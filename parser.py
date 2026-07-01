"""OCR結果（テキスト行）→ 仕訳データへの暫定解析。

現状は領収書・電子請求書向けの汎用ヒューリスティック（日付と合計金額を拾って
1仕訳を起こす）のみ。精度は限定的なので、生成した仕訳はすべて「要確認」扱いに
して人の修正を前提とする。

通帳・カード明細は明細行が多数並ぶ表形式で、フォーマット（銀行・カード会社）
ごとの専用解析が必要なため未対応。実物サンプル入手後に実装する。
"""

from __future__ import annotations

import re
from datetime import date

from accounts import estimate_expense_account
from models import JournalEntry, ParseResult

# 書類タイプ → 貸方勘定科目（支払手段）の既定値
CREDIT_ACCOUNT_BY_DOC_TYPE = {
    "領収書": "現金",
    "電子請求書": "未払金",
}

# 対応する日付表記: 2026年4月1日 / 2026/04/01 / 2026-4-1 / 令和8年4月1日 / R8.4.1
_DATE_PATTERNS = [
    re.compile(r"(20\d{2})[年/.\-](\d{1,2})[月/.\-](\d{1,2})日?"),
    re.compile(r"令和\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
    re.compile(r"[RＲ](\d{1,2})[.．](\d{1,2})[.．](\d{1,2})"),
]

# 金額: カンマ区切り（715,000）または ¥ 付き（¥1500）を金額とみなす。
# 桁区切りなしの裸の数字は年号・番号と区別できないため拾わない。
_AMOUNT_PATTERN = re.compile(r"[¥￥]?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:円)?")

_TOTAL_KEYWORDS = ("合計", "総額", "請求金額", "御請求額", "ご請求額", "領収金額")


def _to_date(m: re.Match, era: bool) -> date | None:
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if era:
            y += 2018  # 令和元年 = 2019
        return date(y, mo, d)
    except ValueError:
        return None


def _find_date(lines: list[str]) -> date | None:
    for line in lines:
        for i, pat in enumerate(_DATE_PATTERNS):
            m = pat.search(line)
            if m:
                found = _to_date(m, era=(i > 0))
                if found:
                    return found
    return None


def _amounts_in(line: str) -> list[int]:
    values = []
    for m in _AMOUNT_PATTERN.finditer(line):
        text = m.group(1)
        has_marker = "," in text or "¥" in m.group(0) or "￥" in m.group(0) or "円" in m.group(0)
        if not has_marker:
            continue  # 裸の数字（年号・電話番号の断片など）は金額とみなさない
        value = int(text.replace(",", ""))
        if 1 <= value <= 100_000_000:
            values.append(value)
    return values


def _find_total(lines: list[str]) -> int | None:
    """合計金額を探す。「合計」等のキーワード行の近く（同じ行〜2行先）を優先し、
    見つからなければ全金額の最大値を使う。"""
    all_amounts: list[int] = []
    keyword_hits: list[int] = []
    for i, line in enumerate(lines):
        amounts = _amounts_in(line)
        all_amounts.extend(amounts)
        if any(kw in line for kw in _TOTAL_KEYWORDS):
            for near in lines[i : i + 3]:
                near_amounts = _amounts_in(near)
                if near_amounts:
                    keyword_hits.append(near_amounts[0])
                    break
    if keyword_hits:
        return max(keyword_hits)  # 「小計」より「合計」が大きい前提で最大を採る
    if all_amounts:
        return max(all_amounts)
    return None


def parse_document(lines: list[str], document_type: str, source_name: str = "") -> ParseResult:
    """OCRテキスト行を書類タイプに応じて仕訳データに変換する。"""
    result = ParseResult()

    if document_type not in CREDIT_ACCOUNT_BY_DOC_TYPE:
        result.warnings.append(
            f"書類タイプ「{document_type}」の自動解析は未対応です"
            "（銀行・カード会社ごとの実物サンプルを元に実装予定）。"
        )
        return result

    total = _find_total(lines)
    if total is None:
        result.warnings.append(
            "金額を検出できませんでした。OCR結果を確認し、手動で行を追加してください。"
        )
        return result

    found_date = _find_date(lines)
    if found_date is None:
        found_date = date.today()
        result.warnings.append(
            "日付を検出できなかったため本日日付を仮置きしました。表で修正してください。"
        )

    text = " ".join(lines)
    debit_account, _ = estimate_expense_account(text)

    result.entries.append(
        JournalEntry(
            date=found_date,
            debit_account=debit_account,
            credit_account=CREDIT_ACCOUNT_BY_DOC_TYPE[document_type],
            amount=total,
            description=source_name or document_type,
            # 暫定解析のため、科目が推定できた場合でも一律で人の確認に回す
            needs_review=True,
            note="暫定解析（合計金額ベース）",
        )
    )
    return result
