"""OCR結果 → 仕訳データへの解析。

- 領収書・電子請求書: テキスト行から日付と合計金額を拾って1仕訳を起こす
  （parse_document）。暫定解析のため全件「要確認」。
- 通帳・カード明細: 座標で復元した表の行（ocr.group_rows の結果）を
  1行=1取引として解析する（parse_table_document）。通帳は残高の連続性
  （前残高 ± 入出金 = 残高）で入金/出金を判定し、合わない行だけ要確認に
  落とす。銀行・カード会社ごとの細部はサンプルを見ながら調整する。
"""

from __future__ import annotations

import re
from datetime import date

from accounts import (
    estimate_expense_account,
    estimate_income_account,
    yayoi_tax,
)
from models import JournalEntry, ParseResult
from ocr import OcrLine

# 書類タイプ → 貸方勘定科目（支払手段）の既定値
CREDIT_ACCOUNT_BY_DOC_TYPE = {
    "領収書": "現金",
    "電子請求書": "未払金",
}

# 通帳の預金口座に使う勘定科目
BANK_ACCOUNT = "普通預金"
# カード明細の支払いに使う勘定科目
CARD_CREDIT_ACCOUNT = "未払金"

# 対応する日付表記: 2026年4月1日 / 2026/04/01 / 2026-4-1 / 令和8年4月1日 / R8.4.1
_DATE_PATTERNS = [
    re.compile(r"(20\d{2})[年/.\-](\d{1,2})[月/.\-](\d{1,2})日?"),
    re.compile(r"令和\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
    re.compile(r"[RＲ](\d{1,2})[.．](\d{1,2})[.．](\d{1,2})"),
]

# 金額: カンマ区切り（715,000）または ¥ 付き（¥1500）を金額とみなす。
# 桁区切りなしの裸の数字は年号・番号と区別できないため拾わない。
_AMOUNT_PATTERN = re.compile(r"[¥￥]?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:円)?")

_TOTAL_KEYWORDS = ("合計", "総額", "請求金額", "御請求額", "ご請求額", "領収金額", "お買上げ計", "お買い上げ計")

# レシートで合計と誤認しやすい行（預り金・釣り銭・ポイント）は金額候補から除外する
_EXCLUDE_KEYWORDS = ("預り", "預かり", "お釣", "おつり", "釣り銭", "釣銭", "ポイント")


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
        if any(kw in line for kw in _EXCLUDE_KEYWORDS):
            continue
        amounts = _amounts_in(line)
        all_amounts.extend(amounts)
        if any(kw in line for kw in _TOTAL_KEYWORDS):
            for near in lines[i : i + 3]:
                if any(kw in near for kw in _EXCLUDE_KEYWORDS):
                    continue
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
    credit_account = CREDIT_ACCOUNT_BY_DOC_TYPE[document_type]

    result.entries.append(
        JournalEntry(
            date=found_date,
            debit_account=debit_account,
            credit_account=credit_account,
            amount=total,
            description=source_name or document_type,
            debit_tax=yayoi_tax(debit_account),
            credit_tax=yayoi_tax(credit_account),
            # 暫定解析のため、科目が推定できた場合でも一律で人の確認に回す
            needs_review=True,
            note="暫定解析（合計金額ベース）",
        )
    )
    return result


# --- 通帳・カード明細（表形式）の解析 ---

# 通帳の日付セル: 08-04-01（和暦の令和8年）/ 2026-04-01 / 8.4.1 など
_CELL_DATE_PATTERN = re.compile(r"^(\d{1,4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$")
# 年なしの日付セル: 6-02 / 6/2 / 6月2日 など（年は文書内の別の記載や前後の行から補う）
_CELL_MONTH_DAY_PATTERN = re.compile(r"^(\d{1,2})[-/.月](\d{1,2})日?$")
# 表以外の場所（見出し等）から年を拾うためのパターン
_YEAR_HINT_PATTERNS = [
    re.compile(r"(20\d{2})\s*年"),
    re.compile(r"令和\s*(\d{1,2})\s*年"),
]


def _parse_cell_date(text: str) -> date | None:
    """表のセル1つを日付として解釈する。

    通帳の日付は「08-04-01」のように和暦（元号なし）で印字されることが
    多い。年が18以下なら令和の年（+2018）、19〜99なら西暦下2桁とみなす。
    """
    m = _CELL_DATE_PATTERN.match(text.strip())
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y <= 18:  # 令和（R18=2036年まで対応）
        y += 2018
    elif y <= 99:  # 西暦下2桁
        y += 2000 if y < 80 else 1900
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _parse_cell_amount(text: str) -> int | None:
    """表のセル1つを金額として解釈する。通帳の「*150,000*」のような
    記号付きにも対応。金額でなければ None。"""
    t = text.strip().strip("*＊ ").replace("¥", "").replace("￥", "").replace("円", "")
    t = t.replace(",", "").replace(" ", "")
    if not t.isdigit():
        return None
    value = int(t)
    if value > 1_000_000_000:
        return None
    return value


def _split_row(
    cells: list[OcrLine],
) -> tuple[date | None, tuple[int, int] | None, str, list[tuple[int, float]]]:
    """表の1行を (完全な日付, 年なしの(月,日), 摘要, [(金額, X座標), ...]) に分解する。"""
    row_date = None
    month_day = None
    desc_parts: list[str] = []
    amounts: list[tuple[int, float]] = []
    for cell in cells:
        text = cell.text.strip()
        if row_date is None:
            d = _parse_cell_date(text)
            if d:
                row_date = d
                continue
        if row_date is None and month_day is None:
            m = _CELL_MONTH_DAY_PATTERN.match(text)
            if m:
                mo, d = int(m.group(1)), int(m.group(2))
                if 1 <= mo <= 12 and 1 <= d <= 31:
                    month_day = (mo, d)
                    continue
        a = _parse_cell_amount(text)
        if a is not None:
            amounts.append((a, cell.x))
        else:
            desc_parts.append(text)
    return row_date, month_day, " ".join(p for p in desc_parts if p), amounts


def _find_year_hint(rows: list[list[OcrLine]]) -> int | None:
    """書類のどこか（見出し・完全な日付セルなど）から年を拾う。"""
    for cells in rows:
        for cell in cells:
            d = _parse_cell_date(cell.text.strip())
            if d:
                return d.year
            for i, pat in enumerate(_YEAR_HINT_PATTERNS):
                m = pat.search(cell.text)
                if m:
                    year = int(m.group(1))
                    return year + 2018 if i == 1 else year  # 令和 → 西暦
    return None


class _RowDateResolver:
    """年なし日付（6-02 等）に年を補い、年またぎ（12月→1月）も追跡する。"""

    def __init__(self, rows: list[list[OcrLine]], result: ParseResult):
        self.year = _find_year_hint(rows)
        self.assumed = self.year is None
        if self.assumed:
            self.year = date.today().year
            result.warnings.append(
                "書類から年を特定できなかったため、年なしの日付は本年"
                f"（{self.year}年）と仮定しました。取引日付を確認してください。"
            )
        self.last_month: int | None = None

    def resolve(self, full_date: date | None, month_day: tuple[int, int] | None) -> tuple[date | None, bool]:
        """行の日付を確定する。戻り値: (日付, 年を仮定したか)。"""
        if full_date is not None:
            self.year = full_date.year
            self.last_month = full_date.month
            return full_date, False
        if month_day is None:
            return None, False
        mo, d = month_day
        # 月が大きく戻ったら年またぎ（12月→1月など）とみなす
        if self.last_month is not None and self.last_month - mo >= 6:
            self.year += 1
        self.last_month = mo
        try:
            return date(self.year, mo, d), self.assumed
        except ValueError:
            return None, False


def _parse_bankbook(rows: list[list[OcrLine]], result: ParseResult) -> None:
    """通帳の明細を解析する。

    各行の一番右の金額を「残高」、その左を「入出金額」とみなし、
    前行残高との差で入金/出金を判定する。判定できない行は要確認。
    """
    prev_balance: int | None = None
    last_date: date | None = None
    resolver = _RowDateResolver(rows, result)
    # 残高チェックで確定できた列位置を覚えて、チェック不能行の判定に使う
    withdraw_xs: list[float] = []
    deposit_xs: list[float] = []

    for cells in rows:
        full_date, month_day, desc, amounts = _split_row(cells)
        row_date, year_assumed = resolver.resolve(full_date, month_day)
        if not amounts:
            continue  # 見出し行・摘要のみの行

        if row_date is None and last_date is None and len(amounts) == 1:
            # 日付のない金額1つだけの行は繰越残高とみなす
            prev_balance = amounts[0][0]
            continue

        if len(amounts) == 1:
            # 入出金額か残高か判別できない。残高のみ更新行として扱う
            prev_balance = amounts[0][0]
            continue

        movement, movement_x = amounts[-2]
        balance = amounts[-1][0]
        entry_date = row_date or last_date
        if entry_date is None:
            result.warnings.append(f"日付を特定できない行をスキップしました: {desc}")
            continue
        last_date = entry_date

        needs_review = year_assumed  # 年を仮定した行は日付の確認が必要
        note = "年を仮定（書類に年の記載なし）" if year_assumed else ""
        if prev_balance is not None and prev_balance + movement == balance:
            is_deposit = True
            deposit_xs.append(movement_x)
        elif prev_balance is not None and prev_balance - movement == balance:
            is_deposit = False
            withdraw_xs.append(movement_x)
        else:
            # 残高チェック不成立。既知の列位置から推定し、要確認にする
            needs_review = True
            note = "残高チェック不一致（入出金の向き・金額を確認）"
            if prev_balance is not None:
                result.warnings.append(
                    f"残高が合いません: {entry_date} {desc}（要確認にしました）"
                )
            if deposit_xs and withdraw_xs:
                is_deposit = abs(movement_x - _mean(deposit_xs)) < abs(
                    movement_x - _mean(withdraw_xs)
                )
            else:
                is_deposit = False
        prev_balance = balance

        if is_deposit:
            account, unknown = estimate_income_account(desc)
            debit_account, credit_account = BANK_ACCOUNT, account
        else:
            account, unknown = estimate_expense_account(desc)
            debit_account, credit_account = account, BANK_ACCOUNT
        result.entries.append(
            JournalEntry(
                date=entry_date,
                debit_account=debit_account,
                credit_account=credit_account,
                amount=movement,
                description=desc,
                debit_tax=yayoi_tax(debit_account),
                credit_tax=yayoi_tax(credit_account),
                needs_review=needs_review or unknown,
                note=note,
            )
        )


def _parse_card(rows: list[list[OcrLine]], result: ParseResult) -> None:
    """カード明細の明細を解析する。日付＋摘要＋利用額の行を1取引とする。"""
    resolver = _RowDateResolver(rows, result)
    for cells in rows:
        full_date, month_day, desc, amounts = _split_row(cells)
        row_date, year_assumed = resolver.resolve(full_date, month_day)
        if row_date is None or not amounts:
            continue  # 見出し・合計行など
        amount = amounts[-1][0]
        account, unknown = estimate_expense_account(desc)
        result.entries.append(
            JournalEntry(
                date=row_date,
                debit_account=account,
                credit_account=CARD_CREDIT_ACCOUNT,
                amount=amount,
                description=desc,
                debit_tax=yayoi_tax(account),
                credit_tax=yayoi_tax(CARD_CREDIT_ACCOUNT),
                needs_review=unknown or year_assumed,
                note="年を仮定（書類に年の記載なし）" if year_assumed else "",
            )
        )


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def parse_table_document(
    rows: list[list[OcrLine]], document_type: str, source_name: str = ""
) -> ParseResult:
    """座標で復元した表の行から、通帳・カード明細の仕訳を起こす。"""
    result = ParseResult()
    if document_type == "通帳":
        _parse_bankbook(rows, result)
    elif document_type == "カード明細":
        _parse_card(rows, result)
    else:
        result.warnings.append(f"書類タイプ「{document_type}」は表形式解析の対象外です。")
        return result

    if not result.entries:
        result.warnings.append(
            "明細行を検出できませんでした。OCR結果を確認してください"
            "（レイアウトによっては調整が必要です。サンプルを共有してください）。"
        )
    return result
