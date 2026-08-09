"""補助科目マスタ: 弥生「補助科目一覧表」PDFの読み取りと、OCR摘要との突合。

- parse_yayoi_subaccount_pdf(): 弥生から出力した補助科目一覧表PDFを解析し、
  (勘定科目, 補助科目, サーチキー) の一覧を返す。クライアント別マスタの
  一括登録に使う。
- match_subaccount(): 通帳のOCR摘要（半角カタカナが多い）をマスタと突合する。
  補助科目名の正規化一致に加え、カタカナをローマ字化して弥生のサーチキー
  英字（利用者が付けた略称ローマ字）と照合する。
"""

from __future__ import annotations

import io
import re
import unicodedata

# --- 弥生 補助科目一覧表PDF の解析 ---

# 税区分の値（この語が現れたセル以降は税設定の列とみなす）
_TAX_WORDS = ("対象外", "課税売上", "課対仕入", "非課仕入", "非課売上", "対外売上", "対外仕入")


def _is_tax_cell(text: str) -> bool:
    return any(text.startswith(w) for w in _TAX_WORDS)


def parse_yayoi_subaccount_pdf(file_bytes: bytes) -> list[dict]:
    """弥生の補助科目一覧表PDFから (勘定科目, 補助科目, サーチキー) を抽出する。"""
    import pdfplumber

    records: list[dict] = []
    current_account: str | None = None
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            line_map: dict[int, list] = {}
            for w in page.extract_words():
                line_map.setdefault(round(w["top"] / 4), []).append(w)
            rows = []
            for key in sorted(line_map):
                cells = sorted(line_map[key], key=lambda w: w["x0"])
                rows.append([w["text"] for w in cells])

            for i, cells in enumerate(rows):
                joined = "".join(cells)
                if "補助科目一覧" in joined.replace(" ", ""):
                    continue  # タイトル行
                if re.fullmatch(r"\d+\s*頁", joined):
                    continue  # ページ番号
                if cells[0] == "補助科目" and any("サーチキー" in c for c in cells):
                    continue  # 表ヘッダ

                tax_idx = next((j for j, c in enumerate(cells) if _is_tax_cell(c)), None)
                if tax_idx is None:
                    # 税区分のない行 = 勘定科目の見出し（ページ末尾の会社名
                    # フッターも一度ここに入るが、データ行が続かないため無害）
                    if len(cells) <= 2:
                        current_account = " ".join(cells).strip()
                    continue
                if not current_account:
                    continue

                before = cells[:tax_idx]
                search_key = ""
                if before and re.fullmatch(r"[0-9a-zA-Z&\-]+", before[-1]):
                    search_key = before[-1].lower()
                    before = before[:-1]
                sub_name = " ".join(before).strip()
                if sub_name:
                    records.append(
                        {
                            "account": current_account,
                            "sub_name": sub_name,
                            "search_key": search_key,
                        }
                    )
    return records


# --- 摘要との突合 ---

# 法人格などの表記ゆれで一致を妨げる断片（正規化時に除去）
_LEGAL_TOKENS = (
    "株式会社", "有限会社", "合同会社", "合資会社", "㈱", "㈲", "(株)", "(有)",
    "カ)", "ユ)", "ド)", "(カ", "(ユ", "カブシキガイシャ", "ユウゲンガイシャ",
)

_KATAKANA_RUN = re.compile(r"[ァ-ヴー]+")

# カタカナ→ローマ字（ヘボン式ベース。後段の正規化で訓令式との差を吸収する）
_KANA_BASE = {
    "ア": "a", "イ": "i", "ウ": "u", "エ": "e", "オ": "o",
    "カ": "ka", "キ": "ki", "ク": "ku", "ケ": "ke", "コ": "ko",
    "ガ": "ga", "ギ": "gi", "グ": "gu", "ゲ": "ge", "ゴ": "go",
    "サ": "sa", "シ": "shi", "ス": "su", "セ": "se", "ソ": "so",
    "ザ": "za", "ジ": "ji", "ズ": "zu", "ゼ": "ze", "ゾ": "zo",
    "タ": "ta", "チ": "chi", "ツ": "tsu", "テ": "te", "ト": "to",
    "ダ": "da", "ヂ": "ji", "ヅ": "zu", "デ": "de", "ド": "do",
    "ナ": "na", "ニ": "ni", "ヌ": "nu", "ネ": "ne", "ノ": "no",
    "ハ": "ha", "ヒ": "hi", "フ": "fu", "ヘ": "he", "ホ": "ho",
    "バ": "ba", "ビ": "bi", "ブ": "bu", "ベ": "be", "ボ": "bo",
    "パ": "pa", "ピ": "pi", "プ": "pu", "ペ": "pe", "ポ": "po",
    "マ": "ma", "ミ": "mi", "ム": "mu", "メ": "me", "モ": "mo",
    "ヤ": "ya", "ユ": "yu", "ヨ": "yo",
    "ラ": "ra", "リ": "ri", "ル": "ru", "レ": "re", "ロ": "ro",
    "ワ": "wa", "ヲ": "o", "ン": "n", "ヴ": "bu",
    "ァ": "a", "ィ": "i", "ゥ": "u", "ェ": "e", "ォ": "o",
}
_SMALL_Y = {"ャ": "ya", "ュ": "yu", "ョ": "yo"}

# 訓令式・ヘボン式などの表記ゆれを1つの形に寄せる置換（順序に意味がある）
_ROMAJI_CANON = [
    ("shi", "si"), ("sha", "sya"), ("shu", "syu"), ("sho", "syo"), ("shy", "sy"),
    ("chi", "ti"), ("cha", "tya"), ("chu", "tyu"), ("cho", "tyo"), ("chy", "ty"),
    ("tsu", "tu"), ("ji", "zi"), ("ja", "zya"), ("ju", "zyu"), ("jo", "zyo"),
    ("jy", "zy"), ("fu", "hu"), ("l", "r"),  # カナのローマ字化に l は現れない
]


def kana_to_romaji(text: str) -> str:
    """カタカナをローマ字にする（カタカナ以外の文字は無視）。"""
    out: list[str] = []
    chars = list(text)
    i = 0
    while i < len(chars):
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if ch == "ッ":
            # 次の音の子音を重ねる
            if nxt in _KANA_BASE:
                out.append(_KANA_BASE[nxt][0])
            i += 1
            continue
        if ch == "ー":
            i += 1
            continue
        if nxt in _SMALL_Y and ch in _KANA_BASE and _KANA_BASE[ch].endswith("i"):
            head = _KANA_BASE[ch][:-1]  # ki→k, shi→sh, ji→j
            out.append(head + _SMALL_Y[nxt])
            i += 2
            continue
        if ch in _KANA_BASE:
            out.append(_KANA_BASE[ch])
        i += 1
    return "".join(out)


def canonical_romaji(text: str) -> str:
    """ローマ字表記のゆれ（ヘボン式/訓令式など）を1つの形に正規化する。"""
    s = re.sub(r"[^0-9a-z]", "", text.lower())
    for old, new in _ROMAJI_CANON:
        s = s.replace(old, new)
    return s


def normalize_name(text: str) -> str:
    """補助科目名・摘要の突合用正規化: 全半角統一、ひらがな→カタカナ、
    法人格・空白・記号の除去。"""
    s = unicodedata.normalize("NFKC", text)
    for token in _LEGAL_TOKENS:
        s = s.replace(token, "")
    s = "".join(
        chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s  # ひらがな→カタカナ
    )
    s = re.sub(r"[\s・.,、。()（）\-‐−/]", "", s)
    return s.lower()


# 入金（預金の増加）の相手になりうる勘定科目の目印
_RECEIVABLE_MARKERS = ("売掛", "未収", "売上", "完成工事高", "前受", "受取")


def _is_receivable_account(account: str) -> bool:
    return any(m in account for m in _RECEIVABLE_MARKERS)


def match_subaccount(
    description: str, subaccounts: list[dict], side: str | None = None
) -> dict | None:
    """摘要をマスタと突合し、最も確からしい (勘定科目, 補助科目) を返す。

    side: "deposit"（入金→売掛・未収・売上系のみ）/ "withdrawal"（出金→
    それ以外）/ None（絞り込みなし）。
    1) 正規化した補助科目名の部分一致（3文字以上）
    2) 摘要のカタカナをローマ字化し、サーチキー英字（3文字以上）を含むか
    の順で照合し、より長く一致したものを採用する。

    戻り値は {"account", "sub_name", "by"}。by は "name"（名前の直接一致・
    確度高）か "key"（サーチキー経由・略称のため誤マッチがあり得る）。
    """
    desc_norm = normalize_name(description)
    desc_romaji = canonical_romaji(
        kana_to_romaji("".join(_KATAKANA_RUN.findall(desc_norm.upper())))
    )

    best: tuple[int, str, dict] | None = None
    for record in subaccounts:
        account = record["account"]
        if side == "deposit" and not _is_receivable_account(account):
            continue
        if side == "withdrawal" and _is_receivable_account(account):
            continue

        name_norm = normalize_name(record["sub_name"])
        score, by = 0, ""
        if len(name_norm) >= 3 and (name_norm in desc_norm or desc_norm in name_norm):
            score, by = 100 + len(name_norm), "name"
        else:
            key = record.get("search_key", "")
            if (
                len(key) >= 3
                and key.isascii()
                and not key.isdigit()
                and desc_romaji
                and canonical_romaji(key) in desc_romaji
            ):
                score, by = len(key), "key"
        if score and (best is None or score > best[0]):
            best = (score, by, record)
    if best is None:
        return None
    return {"account": best[2]["account"], "sub_name": best[2]["sub_name"], "by": best[1]}
