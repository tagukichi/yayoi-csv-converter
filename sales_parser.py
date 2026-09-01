"""売上（売掛表）・売上請求書・仕入請求書・買掛表の解析。

会計事務所の指示に基づく仕訳の形:
    売上請求書:   完成工事未収入金（補助科目=取引先）/ 当期完成工事高
    仕入・外注の請求書: 外注費 or 仕入高 / 工事未払金（補助科目=取引先）
    売掛表・買掛表: 取引先ごとの当月合計額を月末日付で1本ずつ（税込10%）

実際の科目名は会社ごとに違う（売掛金/完成工事未収入金 等）ため、
借方・貸方の科目は「事前登録」の勘定科目マスタと書類タイプの紐付けで決める。
ここでは紐付けが未設定でも動くよう、マスタから既定の科目を推定する。

入力はどれも「セル文字列の2次元リスト」(rows)。xlsx は openpyxl の値、
CSV は csv.reader、OCR（PDF・画像）は行テキストを1セルの行に変換して渡す。
"""

from __future__ import annotations

import calendar
import csv
import io
import re
import unicodedata
from datetime import date, datetime

from accounts import yayoi_tax
from models import JournalEntry, ParseResult
from submaster import normalize_name

# 書類タイプの区分
PARTNER_LEDGER_TYPES = ("売上", "買掛表")  # 取引先別の月次金額一覧
INVOICE_TYPES = ("売上請求書", "仕入請求書")  # 1枚の請求書
SALES_DOC_TYPES = ("売上", "売上請求書")  # 売上側（貸方が収益）


def is_sales_type(doc_type: str) -> bool:
    return doc_type in SALES_DOC_TYPES


# --- 書類タイプ→科目の既定値 ---


def _pick_account(names: set[str], exact: list[str], markers: list[str], fallback: str) -> str:
    for c in exact:
        if c in names:
            return c
    for n in names:
        if any(m in n for m in markers):
            return n
    return fallback


def default_doctype_rule(doc_type: str, account_names: list[str] | None = None) -> dict:
    """書類タイプ紐付けが未設定のときの既定の科目を返す。

    クライアントの勘定科目マスタ（account_names）に建設業の科目
    （完成工事未収入金・工事未払金 等）があればそちらを優先する。
    """
    names = set(account_names or [])
    if is_sales_type(doc_type):
        return {
            "debit_account": _pick_account(
                names, ["完成工事未収入金", "売掛金"], ["売掛", "未収入金"], "売掛金"
            ),
            "credit_account": _pick_account(
                names, ["当期完成工事高", "売上高"], ["完成工事高", "売上高"], "売上高"
            ),
            "sub_side": "debit",
        }
    return {
        "debit_account": _pick_account(
            names, ["外注費", "仕入高"], ["外注"], "仕入高"
        ),
        "credit_account": _pick_account(
            names, ["工事未払金", "買掛金"], ["買掛", "工事未払"], "買掛金"
        ),
        "sub_side": "credit",
    }


# --- 共通ヘルパ ---

_AMOUNT_RE = re.compile(r"^[+-]?\d+(?:\.0+)?$")


def _to_amount(cell: str) -> int | None:
    """セル文字列を金額（円・整数）にする。数値でなければ None。"""
    s = unicodedata.normalize("NFKC", str(cell)).strip()
    s = s.replace(",", "").replace("¥", "").replace("円", "").replace(" ", "")
    if not s or not _AMOUNT_RE.fullmatch(s):
        return None
    try:
        value = float(s)
    except ValueError:
        return None
    if value != int(value):
        return None
    return int(value)


def _clean_rows(rows: list[list]) -> list[list[str]]:
    return [["" if c is None else str(c).strip() for c in row] for row in rows]


_REIWA_DATE = re.compile(r"令和\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_WESTERN_DATE = re.compile(r"(20\d{2})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})\s*日?")
_YEAR_ONLY = re.compile(r"(20\d{2})\s*年")
_REIWA_YEAR_ONLY = re.compile(r"令和\s*(\d{1,2})\s*年")
_MONTH_ONLY = re.compile(r"(?<![\d/.\-])(\d{1,2})\s*月(?!\d*日)")


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _find_document_date(rows: list[list[str]]) -> date | None:
    """行を連結したテキストから書類の日付を探す（令和・西暦）。"""
    for row in rows[:40]:
        joined = "".join(row)
        m = _REIWA_DATE.search(joined)
        if m:
            year = 2018 + int(m.group(1))  # 令和1年=2019
            try:
                return date(year, int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
        m = _WESTERN_DATE.search(joined)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
    return None


def _find_year_month(rows: list[list[str]]) -> tuple[int | None, int | None]:
    """ヘッダ部（先頭数行）から対象の年・月を探す。"""
    year: int | None = None
    month: int | None = None
    for row in rows[:8]:
        joined = "".join(row)
        if year is None:
            m = _YEAR_ONLY.search(joined)
            if m:
                year = int(m.group(1))
            else:
                m = _REIWA_YEAR_ONLY.search(joined)
                if m:
                    year = 2018 + int(m.group(1))
        if month is None:
            m = _MONTH_ONLY.search(joined)
            if m and 1 <= int(m.group(1)) <= 12:
                month = int(m.group(1))
        if year and month:
            break
    return year, month


def _match_custom(description: str, custom_rules: list[tuple[str, str]] | None) -> str | None:
    if not custom_rules:
        return None
    text = description.lower()
    for keyword, account in custom_rules:
        if keyword.lower() in text:
            return account
    return None


def _canonical_partner(name: str, subaccounts: list[dict] | None) -> tuple[str, bool]:
    """取引先名を補助科目マスタと突合して正式名に寄せる。

    戻り値: (取引先名, マスタに存在するか)。
    """
    if not subaccounts:
        return name, False
    target = normalize_name(name)
    if not target:
        return name, False
    best: tuple[int, str] | None = None
    for r in subaccounts:
        sub_norm = normalize_name(r["sub_name"])
        if len(sub_norm) >= 2 and (sub_norm == target or sub_norm in target or target in sub_norm):
            score = len(sub_norm)
            if best is None or score > best[0]:
                best = (score, r["sub_name"])
    if best:
        return best[1], True
    return name, False


# --- 売掛表・買掛表（取引先別の月次金額一覧） ---

_LEDGER_SKIP_WORDS = ("備忘", "合計", "繰越", "小計", "総計")


def parse_partner_ledger(
    rows: list[list],
    doc_type: str,
    source_name: str = "",
    rule: dict | None = None,
    partner_map: dict[int, str] | None = None,
    subaccounts: list[dict] | None = None,
    custom_expense_rules: list[tuple[str, str]] | None = None,
    custom_income_rules: list[tuple[str, str]] | None = None,
) -> tuple[ParseResult, list[str]]:
    """売掛表・買掛表を仕訳にする。

    行の形は「取引先名 | 金額」または「行番号 | 金額」。行番号形式は
    partner_map（事前登録の行番号→取引先対応）で名前に変換する。
    仕訳は月末日付・取引先ごとに1本・税込。金額が空か0の行は読み飛ばす。

    戻り値: (ParseResult, マスタに無かった新しい取引先名のリスト)。
    """
    rows = _clean_rows(rows)
    result = ParseResult()
    sales = is_sales_type(doc_type)
    rule = rule or default_doctype_rule(doc_type)
    partner_map = partner_map or {}

    year, month = _find_year_month(rows)
    if month is None:
        result.warnings.append(
            f"「{source_name}」から対象の月を読み取れませんでした。"
            "表の先頭に「2025年」「10月」のような年月があるか確認してください。"
        )
        return result, []
    if year is None:
        year = datetime.now().year
        result.warnings.append(
            f"対象の年が書かれていないため {year}年 と仮定しました。日付を確認してください。"
        )
    entry_date = _month_end(year, month)

    new_partners: list[str] = []
    unmapped_nos: list[int] = []
    for row in rows:
        cells = [c for c in row if c]
        if not cells or len(cells) < 2:
            continue
        joined = "".join(cells)
        if any(w in joined for w in _LEDGER_SKIP_WORDS):
            continue
        # 年月のヘッダ行はデータ行として扱わない
        if _YEAR_ONLY.search(joined) or _REIWA_YEAR_ONLY.search(joined) or _MONTH_ONLY.search(joined):
            continue
        label = cells[0]
        amount = next((a for c in cells[1:] if (a := _to_amount(c)) is not None), None)
        if amount is None or amount <= 0:
            continue

        needs_review = False
        row_no = _to_amount(label)
        if row_no is not None:  # 行番号形式
            partner = partner_map.get(row_no, "")
            if not partner:
                unmapped_nos.append(row_no)
                partner = f"No.{row_no}"
                needs_review = True
                in_master = True  # 番号は仮名なのでマスタ登録しない
            else:
                partner, in_master = _canonical_partner(partner, subaccounts)
        else:
            partner, in_master = _canonical_partner(label, subaccounts)
        if not in_master and partner not in new_partners:
            new_partners.append(partner)

        debit = rule["debit_account"]
        credit = rule["credit_account"]
        if sales:
            custom = _match_custom(partner, custom_income_rules)
            if custom:
                credit = custom
            description = f"{partner} {month}月分売上"
        else:
            custom = _match_custom(partner, custom_expense_rules)
            if custom:
                debit = custom
            description = f"{partner} {month}月分仕入"

        result.entries.append(
            JournalEntry(
                date=entry_date,
                debit_account=debit,
                credit_account=credit,
                amount=amount,
                description=description,
                debit_sub=partner if rule["sub_side"] == "debit" else "",
                credit_sub=partner if rule["sub_side"] == "credit" else "",
                debit_tax=yayoi_tax(debit),
                credit_tax=yayoi_tax(credit),
                needs_review=needs_review,
            )
        )

    if unmapped_nos:
        nos = "、".join(str(n) for n in unmapped_nos[:10])
        more = f" ほか{len(unmapped_nos) - 10}件" if len(unmapped_nos) > 10 else ""
        result.warnings.append(
            f"行番号 {nos}{more} に対応する取引先が未登録です。"
            "「事前登録」の行番号対応表に登録すると、次回から取引先名が自動で付きます。"
        )
    if not result.entries:
        result.warnings.append(
            f"「{source_name}」から仕訳にできる行が見つかりませんでした"
            "（金額が入っている行がない可能性があります）。"
        )
    return result, new_partners


# --- 請求書（売上・仕入） ---

_COMPANY_MARKERS = ("株式会社", "有限会社", "合同会社", "㈱", "㈲", "(株)", "(有)")
_INVOICE_TOTAL_LABELS = ("当月合計",)
_INVOICE_TAX_LABEL = "消費税"
_INVOICE_BILLED_LABELS = ("請求金額", "ご請求金額", "御請求金額", "今回請求額")


def _find_addressee(rows: list[list[str]]) -> str:
    """「御中」「様」の宛名（請求先）を探す。"""
    for row in rows:
        for i, cell in enumerate(row):
            if "御中" not in cell:
                continue
            before = cell.split("御中")[0].strip()
            if before:
                return before
            left = [c for c in row[:i] if c.strip()]
            if left:
                return " ".join(left).strip()
    return ""


def _find_issuer(rows: list[list[str]], exclude: str) -> str:
    """発行者（請求元）の会社名を探す。宛名（exclude）は除く。"""
    exclude_norm = normalize_name(exclude) if exclude else ""
    for row in rows[:20]:
        for cell in row:
            if "御中" in cell:
                continue
            if not any(m in cell for m in _COMPANY_MARKERS):
                continue
            name = cell.strip()
            norm = normalize_name(name)
            if exclude_norm and (norm in exclude_norm or exclude_norm in norm):
                continue
            return name
    return ""


def _collect_label_amounts(rows: list[list[str]], label: str) -> list[int]:
    """ラベルを含むセルの金額を集める。同セル内の後続数値 → 右のセル →
    直下1〜3行の同じ列近傍、の順で探す。"""
    found: list[int] = []
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            if label not in cell:
                continue
            if "率" in cell and "%" in cell:
                continue
            rest = cell.split(label, 1)[1]
            m = re.search(r"[\d,，]+", unicodedata.normalize("NFKC", rest))
            if m:
                a = _to_amount(m.group(0))
                if a is not None:
                    found.append(a)
                    continue
            picked = None
            # 結合セルでラベルと金額が離れていることがあるため行末まで見る
            for cc in range(c + 1, len(row)):
                a = _to_amount(row[cc])
                if a is not None:
                    picked = a
                    break
            if picked is None:
                for rr in range(r + 1, min(r + 4, len(rows))):
                    below = rows[rr]
                    for cc in range(max(0, c - 1), min(c + 5, len(below))):
                        a = _to_amount(below[cc])
                        if a is not None:
                            picked = a
                            break
                    if picked is not None:
                        break
            if picked is not None:
                found.append(picked)
    return found


def parse_invoice(
    rows: list[list],
    doc_type: str,
    client_name: str = "",
    source_name: str = "",
    rule: dict | None = None,
    subaccounts: list[dict] | None = None,
    account_names: list[str] | None = None,
    force_review: bool = False,
) -> tuple[ParseResult, list[str]]:
    """請求書（1枚）を仕訳1本にする。

    金額は「当月合計額 + 消費税」（前月繰越や入金額を含まない当月分の税込）。
    見つからないときは「請求金額」で代用し要確認を立てる。
    宛名（御中）にクライアント名があれば仕入、発行者にあれば売上と判定し、
    選択された書類タイプと食い違う場合は判定結果を優先して警告する。

    戻り値: (ParseResult, マスタに無かった新しい取引先名のリスト)。
    """
    rows = _clean_rows(rows)
    result = ParseResult()

    addressee = _find_addressee(rows)
    issuer = _find_issuer(rows, exclude=addressee)

    # 売上か仕入かの自動判定（クライアント名がどちら側に出てくるか）
    effective_type = doc_type
    if client_name:
        client_norm = normalize_name(client_name)
        addr_norm = normalize_name(addressee) if addressee else ""
        issuer_norm = normalize_name(issuer) if issuer else ""
        detected = None
        if addr_norm and client_norm and (client_norm in addr_norm or addr_norm in client_norm):
            detected = "仕入請求書"
        elif issuer_norm and client_norm and (client_norm in issuer_norm or issuer_norm in client_norm):
            detected = "売上請求書"
        if detected and detected != doc_type:
            effective_type = detected
            result.warnings.append(
                f"宛名・発行者から「{detected}」と判定して仕訳しました"
                f"（書類タイプの選択は「{doc_type}」でした）。"
            )
    sales = is_sales_type(effective_type)
    if effective_type != doc_type:
        # 判定で向きが変わったら、渡された紐付けルールは逆向きなので使わない
        rule = default_doctype_rule(effective_type, account_names)
    else:
        rule = rule or default_doctype_rule(effective_type, account_names)

    # 取引先: 売上請求書なら宛名（請求先）、仕入請求書なら発行者（請求元）
    partner_raw = addressee if sales else issuer
    needs_review = force_review or not partner_raw
    new_partners: list[str] = []
    if partner_raw:
        partner, in_master = _canonical_partner(partner_raw, subaccounts)
        if not in_master:
            new_partners.append(partner)
    else:
        partner = ""
        result.warnings.append(
            f"「{source_name}」から取引先（{'宛名' if sales else '発行者'}）を読み取れませんでした。"
        )

    # 金額: 当月合計 + 消費税
    totals = []
    for label in _INVOICE_TOTAL_LABELS:
        totals.extend(_collect_label_amounts(rows, label))
    taxes = _collect_label_amounts(rows, _INVOICE_TAX_LABEL)
    amount = None
    if totals:
        base = min(totals)
        if taxes:
            candidate = base + taxes[0]
            # 「当月合計金額」（税込）が別に印字されていれば突合できる
            amount = candidate if candidate in totals or len(totals) == 1 else max(totals)
        else:
            amount = max(totals)
    else:
        for label in _INVOICE_BILLED_LABELS:
            billed = _collect_label_amounts(rows, label)
            if billed:
                amount = billed[0]
                needs_review = True
                result.warnings.append(
                    "当月合計額が見つからなかったため「請求金額」を使いました。"
                    "前月繰越を含む金額の可能性があるので確認してください。"
                )
                break
    if amount is None or amount <= 0:
        result.warnings.append(
            f"「{source_name}」から金額（当月合計額・消費税）を読み取れませんでした。"
        )
        return result, []

    entry_date = _find_document_date(rows)
    if entry_date is None:
        year, month = _find_year_month(rows)
        if month is not None:
            entry_date = _month_end(year or datetime.now().year, month)
            needs_review = True
        else:
            entry_date = datetime.now().date()
            needs_review = True
            result.warnings.append("請求書の日付を読み取れなかったため、本日の日付にしています。")

    debit = rule["debit_account"]
    credit = rule["credit_account"]
    label = "売上" if sales else "仕入"
    description = f"{partner} {entry_date.month}月分{label}".strip()
    result.entries.append(
        JournalEntry(
            date=entry_date,
            debit_account=debit,
            credit_account=credit,
            amount=amount,
            description=description,
            debit_sub=partner if rule["sub_side"] == "debit" else "",
            credit_sub=partner if rule["sub_side"] == "credit" else "",
            debit_tax=yayoi_tax(debit),
            credit_tax=yayoi_tax(credit),
            needs_review=needs_review,
        )
    )
    return result, new_partners


# --- xlsx / CSV の読み込み ---


def tabular_rows_from_bytes(filename: str, data: bytes) -> list[list[str]]:
    """xlsx / CSV をセル文字列の2次元リストにする。

    xlsx は全シートを連結する（請求書は1枚目に本体、売掛表はシートが
    1枚のことが多い。連結してもラベル検索ベースの解析には影響しない）。
    CSV は cp932 → utf-8 の順で試す。
    """
    name = filename.lower()
    if name.endswith(".xlsx"):
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        rows: list[list[str]] = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                rows.append(["" if v is None else str(v).strip() for v in row])
        return rows
    # CSV（Excelからの書き出しは cp932 が多い）
    text = None
    for enc in ("cp932", "utf-8-sig", "utf-8"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("CSVの文字コードを判定できませんでした（Shift-JIS か UTF-8 で保存してください）")
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text))]
