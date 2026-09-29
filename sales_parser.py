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
# 金額の前後に付く飾り: 通貨記号・円・末尾の「－」「※」など
_AMOUNT_DECOR = re.compile(r"^[¥￥*※\s]+|[\s円\-－―ー*※]+$")
# 文中の金額（「130, 900」のように桁区切りの後に空白が入るOCR結果も拾う）
_EMBEDDED_AMOUNT_RE = re.compile(r"\d{1,3}(?:[,，]\s?\d{3})+|\d+")


def _to_amount(cell: str) -> int | None:
    """セル文字列を金額（円・整数）にする。数値でなければ None。

    「¥ 130,900－」「5,500 円」「130, 900」のような請求書の印字にも対応。
    """
    s = unicodedata.normalize("NFKC", str(cell)).strip()
    s = _AMOUNT_DECOR.sub("", s)
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


def _date_in_text(text: str) -> date | None:
    """テキスト中の最初の日付（令和・西暦）を返す。"""
    m = _REIWA_DATE.search(text)
    if m:
        try:
            return date(2018 + int(m.group(1)), int(m.group(2)), int(m.group(3)))  # 令和1年=2019
        except ValueError:
            pass
    m = _WESTERN_DATE.search(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


# 「請求日」「請求年月日」「発行日」の付いた日付を最優先する（請求対象期間の
# 日付や納品日が先に印字されている請求書があるため）
_DATE_LABELS = ("請求年月日", "請求日", "発行日")


def _find_document_date(rows: list[list[str]]) -> date | None:
    """書類の日付を探す。請求日ラベル付き（同じ行→次の行）を優先し、
    無ければ最初に見つかった日付。"""
    head = rows[:40]
    for i, row in enumerate(head):
        joined = "".join(row)
        if any(lbl in joined for lbl in _DATE_LABELS):
            for cand in (joined, "".join(head[i + 1]) if i + 1 < len(head) else ""):
                d = _date_in_text(cand)
                if d:
                    return d
    for row in head:
        d = _date_in_text("".join(row))
        if d:
            return d
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

_COMPANY_MARKERS = (
    "株式会社", "有限会社", "合同会社", "合資会社", "㈱", "㈲", "(株)", "(有)",
    "医療法人", "社会福祉法人", "一般社団法人", "公益社団法人", "一般財団法人",
    "公益財団法人", "学校法人", "宗教法人", "協同組合", "税理士法人", "弁護士法人",
    "司法書士法人", "行政書士法人", "社会保険労務士法人",
)
# 当月分の税抜額（これに消費税を足すと当月の税込額）
_INVOICE_BASE_LABELS = ("当月合計額", "税抜今回お買上", "今回お買上高(税抜)", "税抜金額", "税抜合計")
# 当月分の税込額（そのまま仕訳の金額になる）
_INVOICE_TAXINCL_LABELS = ("当月合計金額", "税込今回お買上", "今回お買上高(税込)", "税込合計")
_INVOICE_TAX_LABEL = "消費税"
# 請求額（前月繰越や入金額を含むことがあるので、当月分が読めないときの代用）
_INVOICE_BILLED_LABELS = (
    "今回ご請求高", "今回御請求高", "今回ご請求額", "今回御請求額", "今回請求額",
    "御請求金額", "ご請求金額", "請求金額合計", "合計金額", "請求金額", "ご請求額", "御請求額",
)
# 前月分や入金など、請求額に混ざる当月分以外の金額
_INVOICE_CARRY_LABELS = ("前月繰越", "繰越高", "繰越金額", "前回ご請求", "前回御請求", "ご入金高", "入金額", "当月入金")


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


def _amount_tokens(text: str) -> list[tuple[int, int, int]]:
    """テキスト中の金額トークンを [(金額, 開始位置, 終了位置)] で返す。

    「10%」の率、「T9-2300-…」「044-522-…」の番号、「2026年08月31日」の
    日付の数字は金額でないので除く。位置は NFKC 正規化後の文字位置。
    """
    norm = unicodedata.normalize("NFKC", text)
    out: list[tuple[int, int, int]] = []
    for m in _EMBEDDED_AMOUNT_RE.finditer(norm):
        head = norm[m.start() - 1] if m.start() > 0 else ""
        head2 = norm[m.start() - 2] if m.start() > 1 else ""
        tail = norm[m.end()] if m.end() < len(norm) else ""
        tail2 = norm[m.end() + 1] if m.end() + 1 < len(norm) else ""
        # 「T9-2300」「044-522」のように数字と数字を「-」「/」でつないだ番号・日付
        joined_number = (head in "-−/" and head2.isdigit()) or (tail in "-−/" and tail2.isdigit())
        if head.isalpha() or joined_number or (tail and tail in "年月日時分名式枚袋台個%"):
            continue
        a = _to_amount(m.group(0))
        if a is not None:
            out.append((a, m.start(), m.end()))
    return out


def _pos_center(span: tuple[float, float], n: int, s: int, e: int) -> float:
    """セルの左右端 span と文字数 n から、文字位置 s〜e の中心X座標を見積もる。"""
    left, right = span
    return left + (right - left) * ((s + e) / 2) / max(n, 1)


def _collect_label_amounts(
    rows: list[list[str]], label: str, cell_spans: list[list[tuple[float, float]]] | None = None
) -> list[int]:
    """ラベルを含むセルの金額を集める。同セル内でラベルの後ろ → 右のセル →
    直下1〜3行の同じ列、の順で探す。

    cell_spans（OCRの各セルの左端・右端X座標）があれば、直下の行では
    見出しの真下にある金額を選ぶ（「前回ご請求高｜…｜今回ご請求高」の
    見出し行の下に金額行が並ぶ表形式の請求書のため）。OCRが見出し行や
    金額行を1つの行テキストにまとめて返した場合も、文字位置から各見出し・
    各金額のX座標を見積もって対応づける。無ければ列番号の近傍で探す（xlsx）。
    """
    # 「合 計 金 額」のように字間に空白を入れた見出しにも合わせる
    label_pat = re.compile(r"\s*".join(re.escape(ch) for ch in label))
    found: list[int] = []
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            norm_cell = unicodedata.normalize("NFKC", cell)
            for m in label_pat.finditer(norm_cell):
                after = norm_cell[m.end():].lstrip()
                if after.startswith("率"):  # 「消費税率」は税額ではない
                    continue
                # 1) 同じセルでラベルの後ろにある金額
                same = _amount_tokens(after)
                if same:
                    found.append(same[0][0])
                    continue
                # 2) 右のセル（結合セルでラベルと金額が離れていることがある）
                picked = None
                for cc in range(c + 1, len(row)):
                    toks = _amount_tokens(row[cc])
                    if toks:
                        picked = toks[0][0]
                        break
                # 3) 直下の行
                if picked is None:
                    label_x = (
                        _pos_center(cell_spans[r][c], len(norm_cell), m.start(), m.end())
                        if cell_spans else None
                    )
                    for rr in range(r + 1, min(r + 4, len(rows))):
                        below = rows[rr]
                        if label_x is not None:
                            cands = []
                            for cc, cell2 in enumerate(below):
                                norm2 = unicodedata.normalize("NFKC", cell2)
                                for a, s, e in _amount_tokens(norm2):
                                    x = _pos_center(cell_spans[rr][cc], len(norm2), s, e)
                                    cands.append((abs(x - label_x), a))
                            if cands:
                                picked = min(cands)[1]
                        else:
                            for cc in range(max(0, c - 1), min(c + 5, len(below))):
                                toks = _amount_tokens(below[cc])
                                if toks:
                                    picked = toks[0][0]
                                    break
                        if picked is not None:
                            break
                if picked is not None:
                    found.append(picked)
    return found


def _first_label_amount(
    rows: list[list[str]], labels: tuple[str, ...], cell_spans: list[list[tuple[float, float]]] | None = None
) -> int | None:
    """ラベル候補を順に探し、最初に金額が見つかったものを返す。"""
    for label in labels:
        amounts = _collect_label_amounts(rows, label, cell_spans)
        if amounts:
            return amounts[0]
    return None


def parse_invoice(
    rows: list[list],
    doc_type: str,
    client_name: str = "",
    source_name: str = "",
    rule: dict | None = None,
    subaccounts: list[dict] | None = None,
    account_names: list[str] | None = None,
    force_review: bool = False,
    cell_spans: list[list[tuple[float, float]]] | None = None,
) -> tuple[ParseResult, list[str]]:
    """請求書（1枚）を仕訳1本にする。

    金額は当月分の税込額。請求書の書式はまちまちなので、次の順で決める:
      1. 税込の当月額（「当月合計金額」「税込今回お買上げ額」）
      2. 税抜の当月額 + 消費税（「当月合計額」「税抜今回お買上高」）
      3. 請求額（「今回ご請求高」「御請求金額」「請求金額合計」「合計金額」）。
         前月繰越や入金が印字されていて0でなければ、当月分でない可能性が
         あるので要確認を立てる。
    宛名（御中）にクライアント名があれば仕入、発行者にあれば売上と判定し、
    選択された書類タイプと食い違う場合は判定結果を優先して警告する。

    cell_spans: OCR経由のとき、各セルの左端・右端X座標（表の見出しの真下の金額を選ぶため）。

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

    # 金額: 税込の当月額 → 税抜の当月額+消費税 → 請求額 の順
    tax_incl = _first_label_amount(rows, _INVOICE_TAXINCL_LABELS, cell_spans)
    base = _first_label_amount(rows, _INVOICE_BASE_LABELS, cell_spans)
    taxes = _collect_label_amounts(rows, _INVOICE_TAX_LABEL, cell_spans)
    billed = _first_label_amount(rows, _INVOICE_BILLED_LABELS, cell_spans)
    amount = None
    if tax_incl:
        amount = tax_incl
    elif base:
        amount = base + taxes[0] if taxes else base
    elif billed:
        amount = billed
        carry = [
            a for label in _INVOICE_CARRY_LABELS
            for a in _collect_label_amounts(rows, label, cell_spans)
        ]
        if any(carry):
            needs_review = True
            result.warnings.append(
                "当月分の金額が見つからなかったため「請求金額」を使いました。"
                "前月繰越や入金額が印字されているので、当月分の金額か確認してください。"
            )
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
