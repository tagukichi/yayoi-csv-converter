"""部門マスタ（事前登録④）の読み取り。

部門の一覧は会社から次の形で届く:
  - 弥生の仕訳CSV（エクスポート）: 7列目が借方部門、13列目が貸方部門。
    ヘッダ付きのものは「借方部門」「貸方部門」の見出しで列を決める
  - 1列に部門名を並べたCSV（「部門コード,部門名」の2列でもよい）
  - 会社が独自に作った部門一覧のPDF・画像: 1行1部門を想定し、行頭の
    コード番号や見出し・ページ番号を除いて部門名にする

どの形でも [{"name": 部門名}, ...]（重複なし・出てきた順）を返す。
PDF・画像は書式が決まっていないため、読み取り結果は登録前に画面で
確認・修正してもらう前提。
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata

# 弥生の仕訳CSV（ヘッダなし）の列位置（0始まり）
_YAYOI_DEBIT_DEPT_COL = 6
_YAYOI_CREDIT_DEPT_COL = 12
# 弥生の仕訳CSVの識別フラグ（2000=1行の仕訳、2110/2100/2101 等=複数行の仕訳）
_YAYOI_FLAG = re.compile(r"^2\d{3}$")

# 部門名として採らない見出し・定型文
_HEADER_WORDS = {
    "部門", "部門名", "部門名称", "部門コード", "コード", "no", "番号", "名称", "備考",
    "部門一覧", "部門一覧表", "部門マスタ", "部門マスター", "借方部門", "貸方部門",
}
# 行頭の部門コード（「001 本社」「10: 営業部」「A01 小作店」）
_LEADING_CODE = re.compile(r"^\s*[A-Za-z]?\d{1,6}\s*[:：.．)\-]?\s+")
# 部門名にならない行（ページ番号・日付・金額だけの行など）
_NOISE_LINE = re.compile(
    r"^[\d\s/\-.,:：年月日頁ページ()（）]*$"
    r"|^\d+\s*/\s*\d+\s*(頁|ページ)?$"
    r"|^(作成日|出力日|印刷日|会社名|株式会社|有限会社)"
)


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp932"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("cp932", errors="replace")


def _clean(name: str) -> str:
    return unicodedata.normalize("NFKC", str(name or "")).strip().strip("\"'　 ")


def _is_header(name: str) -> bool:
    """見出しの語だけでできた文字列か（「部門名」「コード 部門名」など）。"""
    words = _clean(name).lower().split()
    return bool(words) and (
        "".join(words) in _HEADER_WORDS or all(w in _HEADER_WORDS for w in words)
    )


def _unique(names) -> list[dict]:
    seen: dict[str, None] = {}
    for n in names:
        n = _clean(n)
        if n and not _is_header(n):
            seen.setdefault(n, None)
    return [{"name": n} for n in seen]


def parse_department_csv(data: bytes) -> list[dict]:
    """部門のCSV（弥生の仕訳CSV、または部門名の一覧）から部門を読み取る。"""
    rows = [r for r in csv.reader(io.StringIO(_decode(data))) if any(c.strip() for c in r)]
    if not rows:
        return []

    # ヘッダ付き（「借方部門」「貸方部門」の見出しがある）
    for i, row in enumerate(rows[:5]):
        cells = [_clean(c) for c in row]
        dept_cols = [j for j, c in enumerate(cells) if c in ("借方部門", "貸方部門", "部門", "部門名")]
        if dept_cols:
            return _unique(
                r[j] for r in rows[i + 1:] for j in dept_cols if j < len(r)
            )

    # 弥生の仕訳CSV（ヘッダなし・識別フラグで始まる25〜27列）
    yayoi_rows = [r for r in rows if len(r) > _YAYOI_CREDIT_DEPT_COL and _YAYOI_FLAG.match(r[0].strip())]
    if yayoi_rows and len(yayoi_rows) >= len(rows) / 2:
        return _unique(
            r[col] for r in yayoi_rows for col in (_YAYOI_DEBIT_DEPT_COL, _YAYOI_CREDIT_DEPT_COL)
        )

    # 部門名の一覧（1列、または「コード,部門名」）: 数字だけのセルはコードとみなす
    names = []
    for r in rows:
        cells = [_clean(c) for c in r if _clean(c)]
        text_cells = [c for c in cells if not re.fullmatch(r"[A-Za-z]?\d+", c)]
        if text_cells:
            names.append(text_cells[0])
    return _unique(names)


def parse_department_lines(lines: list[str]) -> list[dict]:
    """PDF・画像から読んだ行テキストを部門名にする（1行1部門の一覧を想定）。"""
    names = []
    for line in lines:
        text = _clean(line)
        if not text or _NOISE_LINE.search(text):
            continue
        text = _LEADING_CODE.sub("", text).strip()
        # 「本社 001」のように末尾にコードが付く形
        text = re.sub(r"\s+[A-Za-z]?\d{1,6}$", "", text).strip()
        if len(text) < 2 or len(text) > 40:
            continue
        names.append(text)
    return _unique(names)


def pdf_text_lines(data: bytes) -> list[str]:
    """文字が埋め込まれたPDFの行テキストを返す（スキャンPDFなら空）。"""
    import pdfplumber

    lines: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines.extend(ln for ln in text.splitlines() if ln.strip())
    return lines
