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
import unicodedata
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

# 書類タイプの自動判定に使うキーワード。書類タイプの選び間違い
# （初期値の「領収書」のままカード明細をアップ等）を検出するため。
_DOC_TYPE_SIGNALS = {
    # 「利用明細」は「ご利用明細」と二重に一致するため入れない
    "カード明細": ("ご利用明細", "ご利用代金明細", "カード名義", "利用店名", "支払方法", "お支払い月", "利用金額", "回払い"),
    "通帳": ("普通預金", "お預り金額", "お支払金額", "差引残高", "繰越", "通帳", "当座預金"),
    "領収書": ("領収書", "領収証", "レシート", "お買上", "お釣り", "上様"),
    "電子請求書": ("請求書", "御請求書", "御見積", "お振込先", "振込期日", "支払期日"),
    "給与台帳": ("給与台帳", "給料台帳", "支給月分", "月例給与計", "差引支給額", "非課税分賃金"),
}


# 利用者が選んだ書類タイプへの加点（明示的な選択も証拠として扱う）
_SELECTED_TYPE_BONUS = 2


def detect_document_type(lines: list[str], selected: str | None = None) -> str | None:
    """OCRテキストから書類タイプを推定する。確信が持てなければ None。

    利用者が選んだ書類タイプを上書きする用途なので、判定は慎重に行う。
    駐車場の領収書に「クレジットカードご利用明細」と印字されている等、
    他タイプの語がわずかに混じるケースで誤って上書きしないよう、
    選択されたタイプに加点したうえで、2位に明確な差（2語以上）を
    付けた場合だけ判定を返す。
    """
    text = " ".join(lines)
    scores = {t: sum(1 for kw in kws if kw in text) for t, kws in _DOC_TYPE_SIGNALS.items()}
    if selected in scores:
        scores[selected] += _SELECTED_TYPE_BONUS
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_type, best = ranked[0]
    second = ranked[1][1]
    if best >= 3 and best - second >= 2:
        return best_type
    return None

# 通帳の預金口座に使う勘定科目
BANK_ACCOUNT = "普通預金"
# カード明細の支払いに使う勘定科目
CARD_CREDIT_ACCOUNT = "未払金"

# 対応する日付表記と年の解釈:
#   西暦4桁 2026年4月1日 / 2026/04/01 / 2026-4-1
#   令和    令和8年4月1日 / R8.4.1
#   西暦2桁 '26年06月20日（タクシー領収書などで使われる。頭の記号が目印）
_DATE_PATTERNS = [
    (re.compile(r"(20\d{2})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})日?"), "western"),
    (re.compile(r"令和\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"), "reiwa"),
    (re.compile(r"[RＲ](\d{1,2})[.．](\d{1,2})[.．](\d{1,2})"), "reiwa"),
    (re.compile(r"['’‘´`](\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"), "yy"),
]

# 金額: カンマ区切り（715,000）または ¥ 付き（¥1500）を金額とみなす。
# 手書き領収証では桁区切りがピリオドで書かれること（¥2.916-）があるため
# ピリオド区切りも許容する。桁区切りなしの裸の数字は年号・番号と
# 区別できないため拾わない。
_AMOUNT_PATTERN = re.compile(r"[¥￥]?\s*(\d{1,3}(?:[,，.．]\s?\d{3})+|\d+)(?:円)?")

_SEPARATORS = re.compile(r"[,，.．\s]")

_TOTAL_KEYWORDS = ("合計", "総額", "請求金額", "御請求額", "ご請求額", "領収金額", "お買上げ計", "お買い上げ計", "納入金額")

# レシートで合計と誤認しやすい行（預り金・釣り銭・ポイント）は金額候補から除外する
_EXCLUDE_KEYWORDS = ("預り", "預かり", "お釣", "おつり", "釣り銭", "釣銭", "ポイント", "残高")

# 店舗名の候補から除外する行（書類の種別名・宛名・連絡先・支払欄など）
_STORE_NAME_SKIP = (
    "領収書", "領収証", "レシート", "明細", "御買上", "お買上", "請求書",
    "invoice", "receipt", "tel", "fax", "電話", "〒", "様", "御中",
    "登録番号", "http", "www", "印紙", "上記", "但し", "として", "支払",
    "対象", "消費税", "軽減", "クレジット", "現金", "小切手", "手形", "入金日",
)

# 但し書き（「但 御菓子代として」）から用途を拾う
_TADASHI_PATTERN = re.compile(r"但し?[、,]?\s*(.{2,25}?)\s*として")


# 主要チェーンのブランド名。ロゴのOCR結果（FamilyMart等）や支店名
# （川崎古川町店）ではなく、誰が見てもわかるブランド名を摘要に使う。
# 学習ルール（セブン-イレブン→飲食代 等）のキーワードとも揃えやすくなる。
_BRAND_CANONICAL = [
    (("セブン-イレブン", "セブンイレブン", "セブン‐イレブン", "7-eleven", "seven-eleven"), "セブン-イレブン"),
    (("familymart", "ファミリーマート", "ファミマ"), "ファミリーマート"),
    (("lawson", "ローソン"), "ローソン"),
    (("ministop", "ミニストップ"), "ミニストップ"),
    (("デイリーヤマザキ",), "デイリーヤマザキ"),
    (("newdays", "ニューデイズ"), "NewDays"),
    (("セイコーマート",), "セイコーマート"),
]


def _find_brand(lines: list[str]) -> str | None:
    text = " ".join(lines).lower()
    for keywords, name in _BRAND_CANONICAL:
        if any(k in text for k in keywords):
            return name
    return None


# 会社名・店名の目印。縦書きレシートではOCRが「カー」「車番」のような
# 断片を先頭に読み出すため、これらを含む行を優先して摘要に使う。
_COMPANY_MARKERS = ("株式会社", "(株)", "（株）", "有限会社", "合同会社", "㈱", "㈲")
_STORE_MARKERS = ("店", "パーク", "パーキング", "駅", "館", "商店", "屋", "自動車", "ホテル", "クラブ", "組合", "協会", "センター", "事務所")


def _store_name_candidate(text: str) -> bool:
    text = text.strip()
    if len(text) < 2:
        return False
    lowered = text.lower()
    if any(kw in lowered for kw in _STORE_NAME_SKIP):
        return False
    if _find_date([text]):
        return False
    if _amounts_in(text):
        return False
    if re.fullmatch(r"[\d\s\-:/.,*¥￥円%()（）]+", text):
        return False  # 数字・記号だけの行（電話番号・時刻など）
    return True


_COMPANY_LEGAL_TOKENS = ("株式会社", "有限会社", "合同会社", "㈱", "㈲", "(株)", "(有)", "（株）", "（有）")


def _normalize_company(text: str) -> str:
    s = unicodedata.normalize("NFKC", text)
    for token in _COMPANY_LEGAL_TOKENS:
        s = s.replace(token, "")
    return re.sub(r"\s", "", s).lower()


def _find_store_name(lines: list[str], exclude_name: str | None = None) -> str | None:
    """OCR結果から店舗名らしき行を探す。

    まず書類全体から会社名・店名の目印を含む行を探す（縦書きレシートでは
    OCRが「カー」「車番」のような断片を先頭に読み出すため、位置より
    名前らしさを優先する）。見つからなければレシート先頭付近、手書きの
    領収証の発行者欄（末尾）、最後に2回以上現れる行（ロゴと領収書欄の
    両方に印字される店名のパターン）の順に探す。
    """
    exclude = _normalize_company(exclude_name) if exclude_name else None

    def _is_addressee(text: str) -> bool:
        # 宛名（クライアント企業名）は発行者ではないので摘要にしない
        return bool(exclude) and exclude in _normalize_company(text)

    brand = _find_brand(lines)
    if brand:
        # 支店名（「東古市場店」のように「店」で終わる行）が読めていれば
        # 「ファミリーマート 東古市場店」のようにブランド名に添える
        for line in lines:
            text = line.strip()
            if (
                text.endswith("店")
                and 3 <= len(text) <= 20
                and _store_name_candidate(text)
            ):
                if any(
                    k in text.lower()
                    for keywords, _n in _BRAND_CANONICAL
                    for k in keywords
                ):
                    return text  # 支店名の行にブランド名も含まれている
                return f"{brand} {text}"
        return brand
    for markers in (_COMPANY_MARKERS, _STORE_MARKERS):
        for line in lines:
            text = line.strip()
            if (
                len(text) >= 4 and any(m in text for m in markers)
                and _store_name_candidate(text) and not _is_addressee(text)
            ):
                return text
    for line in lines[:8]:
        if _store_name_candidate(line) and not _is_addressee(line):
            return line.strip()
    for line in reversed(lines[-12:]):
        if _store_name_candidate(line) and not _is_addressee(line):
            return line.strip()
    seen: dict[str, int] = {}
    for line in lines:
        text = line.strip()
        if _store_name_candidate(text) and not _is_addressee(text):
            seen[text] = seen.get(text, 0) + 1
    for text, count in seen.items():
        if count >= 2:
            return text
    return None


# 「クレジット」と明記された支払いのみ貸方を未払金にする。
# QUICPay・FamiPay等の電子マネーは会計事務所の指示により現金扱い
_CREDIT_PAYMENT_KEYWORDS = (
    "クレジット", "credit", "visa", "mastercard", "jcb", "amex",
)


def _to_date(m: re.Match, mode: str) -> date | None:
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if mode == "reiwa":
            y += 2018  # 令和元年 = 2019
        elif mode == "yy":
            y += 2000
        return date(y, mo, d)
    except ValueError:
        return None


def _find_date(lines: list[str]) -> date | None:
    for line in lines:
        for pat, mode in _DATE_PATTERNS:
            m = pat.search(line)
            if m:
                found = _to_date(m, mode)
                if found:
                    return found
    return None


def _amounts_in(line: str) -> list[int]:
    values = []
    for m in _AMOUNT_PATTERN.finditer(line):
        text = m.group(1)
        has_marker = (
            _SEPARATORS.search(text)
            or "¥" in m.group(0)
            or "￥" in m.group(0)
            or "円" in m.group(0)
        )
        if not has_marker:
            continue  # 裸の数字（年号・電話番号の断片など）は金額とみなさない
        value = int(_SEPARATORS.sub("", text))
        if 1 <= value <= 100_000_000:
            values.append(value)
    return values


def _rate_target_amount(lines: list[str], rate: str) -> int | None:
    """「8%対象 ¥1,410」「10%対象￥0-」のような税率別内訳の金額を探す。

    OCRの行順が乱れることがあるため、対象行の少し先（3行）まで金額を探す。
    """
    pat = re.compile(rf"{rate}\s*[%％]")
    for i, line in enumerate(lines):
        if pat.search(line) and "対象" in line:
            for near in lines[i : i + 4]:
                m = re.search(r"[¥￥]\s*([\d,，.．]+)|(\d{1,3}(?:[,，.．]\d{3})+)", near)
                if m:
                    token = _SEPARATORS.sub("", (m.group(1) or m.group(2))).strip()
                    if token.isdigit():
                        return int(token)
    return None


def _reduced_marked_amounts(lines: list[str]) -> list[int]:
    """品目に付く軽減税率マークから8%対象の金額を集める。

    コンビニ等のレシートは「オールフリー500 ¥243軽」のように金額の直後に
    「軽」を印字する。税率内訳の行がOCRで金額と泣き別れになるレイアウト
    でも、品目マークは金額と同じ行に付くため確実に拾える。
    説明行（「軽」は軽減税率対象商品です）や内訳行（8%軽減対象）は除外する。
    """
    marked: list[int] = []
    for line in lines:
        if "軽" not in line or "軽減" in line or "対象" in line:
            continue
        for m in re.finditer(r"(\d{1,3}(?:[,，.．]\s?\d{3})+|\d+)\s*円?\s*軽", line):
            token = _SEPARATORS.sub("", m.group(1))
            if token.isdigit():
                value = int(token)
                if 1 <= value <= 100_000_000:
                    marked.append(value)
    return marked


def _find_total(lines: list[str]) -> int | None:
    """合計金額を探す。

    「合計」等のキーワード行の近く（同じ行〜2行先）を優先する。
    見つからない場合は最も多く現れる金額を採る。レシートは合計額を
    複数回印字する（小計・合計・支払額・運賃料金計など）のに対し、
    交通系ICの残高のような紛らわしい金額は1回しか出ないため。
    縦書きレシートではOCRが項目名と金額を離れた位置に読み出すため、
    同じ行での除外キーワード判定が効かないケースの保険でもある。
    """
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
        counts: dict[int, int] = {}
        for a in all_amounts:
            counts[a] = counts.get(a, 0) + 1
        # 出現回数が多いもの、同数なら金額が大きいものを採る
        return max(counts, key=lambda a: (counts[a], a))
    return None


def parse_document(
    lines: list[str],
    document_type: str,
    source_name: str = "",
    custom_expense_rules: list[tuple[str, str]] | None = None,
    client_name: str | None = None,
) -> ParseResult:
    """OCRテキスト行を書類タイプに応じて仕訳データに変換する。"""
    result = ParseResult()

    # コンビニのレシート等は数字・％が全角で印字される（「１０％対象 ￥１，６４２」）
    # ことがあるため、全角英数記号を半角に正規化してから解析する
    lines = [unicodedata.normalize("NFKC", line) for line in lines]

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
    debit_account, _ = estimate_expense_account(text, custom_expense_rules)
    debit_sub = ""
    credit_account = CREDIT_ACCOUNT_BY_DOC_TYPE[document_type]

    # 支払手段がクレジットカードなら、貸方を現金ではなく未払金にする
    if credit_account == "現金" and any(
        kw in text.lower() for kw in _CREDIT_PAYMENT_KEYWORDS
    ):
        credit_account = "未払金"

    # 摘要はOCRから拾った店舗名を優先し、取れなければファイル名で代用。
    # 但し書き（「御菓子代として」「但し 〜 会費」等）が読めれば添える
    description = _find_store_name(lines, exclude_name=client_name) or source_name or document_type
    tadashi = _TADASHI_PATTERN.search(text)
    if tadashi:
        description = f"{description} {tadashi.group(1)}"
    else:
        for line in lines:
            stripped = line.strip()
            m = re.match(r"^但し?[、,]?\s*(.{2,40})$", stripped)
            if m and "として" not in m.group(1):
                description = f"{description} {m.group(1).strip()}"
                break

    # 住民税（特別徴収）の納付書: 給与から預かった住民税の納付なので、
    # 会計事務所のルールどおり 預り金（補助: 住民税）/ 現金 にする
    if ("特別徴収" in text and "領収証書" in text) or ("個人市民税" in text and "個人県民税" in text):
        debit_account, debit_sub = "預り金", "住民税"
        municipality = next(
            (
                m.group(1)
                for line in lines
                if (m := re.search(r"([一-龥]{1,6}[市区町村])会計管理者", line))
            ),
            None,
        )
        description = f"住民税納付 {municipality}" if municipality else "住民税納付"

    # 軽減税率8%の判定。全額が8%対象なら税区分を軽減8%に、
    # 10%との混在は会計事務所の指示により8%分と10%分の2行に分割する
    debit_tax = yayoi_tax(debit_account)
    credit_tax = yayoi_tax(credit_account)
    note = "暫定解析（合計金額ベース）"
    reduced_hint = ("軽減" in text) or re.search(r"8\s*[%％]\s*(?:軽減)?対象", text)

    def _entry(amount: int, tax: str, desc: str, entry_note: str) -> JournalEntry:
        # 暫定解析のため、科目が推定できた場合でも一律で人の確認に回す
        return JournalEntry(
            date=found_date,
            debit_account=debit_account,
            debit_sub=debit_sub,
            credit_account=credit_account,
            amount=amount,
            description=desc,
            debit_tax=tax,
            credit_tax=credit_tax,
            needs_review=True,
            note=entry_note,
        )

    if reduced_hint and debit_tax == "課対仕入込10%":
        amount8 = _rate_target_amount(lines, "8")
        amount10 = _rate_target_amount(lines, "10")
        no_ten_percent = not re.search(r"10\s*[%％]", text)

        def _split(a8: int, a10: int):
            # 8%分と10%分を別仕訳に分割（会計事務所の指示）
            result.entries.append(
                _entry(a8, "課対仕入込軽減8%", f"{description}（軽減8%分）", "混在レシートの分割")
            )
            result.entries.append(
                _entry(a10, "課対仕入込10%", f"{description}（10%分）", "混在レシートの分割")
            )
            result.warnings.append(
                f"軽減税率の混在を検出し、8%分（{a8:,}円）と10%分（{a10:,}円）の"
                "2行に分割しました。"
            )

        marked_sum = sum(_reduced_marked_amounts(lines))

        # 1) 内訳の行がきちんと読めて合計と一致する場合はそれに従う
        if amount8 and amount10 and amount8 + amount10 == total:
            _split(amount8, amount10)
            return result
        # 2) 10%対象が明示的に0円 → 全額軽減
        if amount10 == 0:
            result.entries.append(
                _entry(total, "課対仕入込軽減8%", description, note)
            )
            return result
        # 3) 品目の「軽」マーク（¥243軽）が合計の一部にだけ付いている
        #    → 混在の積極的な証拠なので、マーク合計を8%分として分割。
        #    複数レシート写真の分割で内訳の行が別の断片に分かれても機能する
        if 0 < marked_sum < total:
            _split(marked_sum, total - marked_sum)
            return result
        # 4) 全額軽減の根拠（マークが全品目に付いている、または10%の記載が
        #    どこにもなく8%の内訳が合計と矛盾しない）
        if marked_sum == total or (
            amount10 is None and no_ten_percent and (amount8 is None or amount8 == total)
        ):
            result.entries.append(
                _entry(total, "課対仕入込軽減8%", description, note)
            )
            return result
        result.warnings.append(
            "軽減税率の記載がありますが内訳を特定できませんでした。税区分を確認してください。"
        )

    result.entries.append(_entry(total, debit_tax, description, note))
    return result


def apply_description_rules(entries: list[JournalEntry], rules: list[dict]) -> int:
    """学習済みの摘要書き換えルールを仕訳に適用する。書き換えた件数を返す。

    rules は storage.list_desc_rules() の結果（キーワードの長い順）。
    摘要にキーワードを含む仕訳の摘要を、ルールの文言に置き換える。
    「飲食代」「セブンイレブン 飲食代」など、置き換え後の文言は
    会社ごとの流儀のまま登録されている前提。
    """
    replaced = 0
    for entry in entries:
        for rule in rules:
            keyword = rule.get("keyword", "")
            if keyword and keyword in entry.description:
                if entry.description != rule["description"]:
                    entry.description = rule["description"]
                    replaced += 1
                break
    return replaced


def parse_receipt_clusters(
    clusters: list[list[str]],
    source_name: str = "",
    custom_expense_rules: list[tuple[str, str]] | None = None,
    client_name: str | None = None,
) -> ParseResult:
    """1枚の写真から分割した複数レシートを解析してまとめる。

    1枚のレシートが複数のかたまりに割れることがある（売上票と領収書が
    同じ用紙に印字されている等）ため、同額の仕訳は1件にまとめ、日付を
    読み取れた方を残す。分割結果は判断が難しいため全件を要確認にする。
    """
    result = ParseResult()
    best_by_amount: dict[int, tuple[JournalEntry, bool]] = {}
    duplicates: list[int] = []

    for texts in clusters:
        partial = parse_document(
            texts, "領収書", source_name=source_name,
            custom_expense_rules=custom_expense_rules,
            client_name=client_name,
        )
        date_guessed = any("日付" in w for w in partial.warnings)
        for e in partial.entries:
            e.needs_review = True
            previous = best_by_amount.get(e.amount)
            if previous is None:
                best_by_amount[e.amount] = (e, date_guessed)
            else:
                duplicates.append(e.amount)
                if previous[1] and not date_guessed:
                    best_by_amount[e.amount] = (e, date_guessed)
        # 断片から金額が取れないのは分割の副産物なので通知しない
        result.warnings.extend(
            w for w in partial.warnings if "金額を検出できません" not in w
        )

    result.entries.extend(e for e, _ in best_by_amount.values())
    result.entries.sort(key=lambda e: e.date)
    if duplicates:
        amounts_text = "・".join(f"{a:,}円" for a in sorted(set(duplicates)))
        result.warnings.append(
            f"同じ金額（{amounts_text}）の仕訳が複数検出されたため1件にまとめました。"
            "1枚のレシートが分割された可能性が高いですが、"
            "実際に同額のレシートが複数ある場合は表で行を追加してください。"
        )
    return result


# --- 通帳・カード明細（表形式）の解析 ---

# 通帳の日付セル: 08-04-01（和暦の令和8年）/ 2026-04-01 / 8.4.1 など
_CELL_DATE_PATTERN = re.compile(r"^(\d{1,4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$")
# 通帳の行番号が日付にくっついたセル: 1008.04.24 = 行番号10 + 08.04.24
_CELL_ROWNUM_DATE_PATTERN = re.compile(r"^(\d{1,2})\s*(\d{2})[-/.](\d{1,2})[-/.](\d{1,2})日?$")
# 年なしの日付セル: 6-02 / 6/2 / 6月2日 など（年は文書内の別の記載や前後の行から補う）
_CELL_MONTH_DAY_PATTERN = re.compile(r"^(\d{1,2})[-/.月](\d{1,2})日?$")
# 表以外の場所（見出し等）から年を拾うためのパターン
_YEAR_HINT_PATTERNS = [
    re.compile(r"(20\d{2})\s*年"),
    re.compile(r"令和\s*(\d{1,2})\s*年"),
]


def _era_or_western(y: int, mo: int, d: int) -> date | None:
    """年の解釈（18以下=令和、99以下=西暦下2桁）と妥当性チェック付きで日付を作る。

    通帳の行番号が日付にくっつくと「1008.04.24」（行番号10＋令和8年）の
    ような値になるため、解釈後の年が現実的な範囲（1990〜2100年）に
    収まらないものは日付として採用しない。
    """
    if y <= 18:  # 令和（R18=2036年まで対応）
        y += 2018
    elif y <= 99:  # 西暦下2桁
        y += 2000 if y < 80 else 1900
    if not (1990 <= y <= 2100):
        return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _parse_cell_date(text: str) -> date | None:
    """表のセル1つを日付として解釈する。

    通帳の日付は「08-04-01」のように和暦（元号なし）で印字されることが
    多い。また左端の行番号がくっついた「1008.04.24」（行番号10＋08.04.24）
    にも対応する。
    """
    text = text.strip()
    m = _CELL_DATE_PATTERN.match(text)
    if m:
        found = _era_or_western(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if found:
            return found
    # 行番号付き: 1〜2桁の行番号 + 2桁年の日付
    m = _CELL_ROWNUM_DATE_PATTERN.match(text)
    if m:
        return _era_or_western(int(m.group(2)), int(m.group(3)), int(m.group(4)))
    return None


def _parse_cell_amount(text: str) -> int | None:
    """表のセル1つを金額として解釈する。通帳の「*150,000*」のような
    記号付きにも対応。金額でなければ None。"""
    t = text.strip().strip("*＊ ").replace("¥", "").replace("￥", "").replace("円", "")
    t = t.replace(",", "").replace(" ", "")
    # isdigit() は丸数字（⑦）等も True になるため ASCII 数字に限定する
    if not (t.isascii() and t.isdigit()):
        return None
    value = int(t)
    if value > 1_000_000_000:
        return None
    return value


# セル先頭に付いた日付を剥がすためのパターン（OCRが日付と摘要を1行に結合した場合）
_CELL_DATE_PREFIX = re.compile(r"^(\d{1,4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?\s+")
# 行番号付きの先頭日付: 「15 08.04.30 保険 …」（行番号15 + 令和8年4月30日）
_CELL_ROWNUM_DATE_PREFIX = re.compile(r"^(\d{1,2})?\s*(\d{2})[-/.](\d{1,2})[-/.](\d{1,2})日?\s+")
_CELL_MD_PREFIX = re.compile(r"^(\d{1,2})[-/.月](\d{1,2})日?\s+")
# テキスト中に埋め込まれた金額（カンマ区切り・¥・円のいずれかの目印があるもの）
_EMBEDDED_AMOUNT = re.compile(r"[¥￥]\s*\d{1,3}(?:,\d{3})*|\d{1,3}(?:,\d{3})+円?|\d+円")


def _split_row(
    cells: list[OcrLine],
) -> tuple[date | None, tuple[int, int] | None, str, list[tuple[int, float]]]:
    """表の1行を (完全な日付, 年なしの(月,日), 摘要, [(金額, X座標), ...]) に分解する。

    OCRは「04/02 セブン-イレブン 1,500」のように日付・摘要・金額を1つの行
    テキストに結合することがあるため、セル単位の判定に加えて、セル内の
    先頭日付・埋め込み金額も抽出する。
    """
    row_date = None
    month_day = None
    desc_parts: list[str] = []
    amounts: list[tuple[int, float]] = []
    for cell in cells:
        text = cell.text.strip()

        # 1) セル全体が日付
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

        # 2) セル全体が金額
        a = _parse_cell_amount(text)
        if a is not None:
            amounts.append((a, cell.x))
            continue

        # 3) 混在セル: 先頭の日付を剥がす
        if row_date is None and month_day is None:
            m = _CELL_DATE_PREFIX.match(text)
            if m:
                d = _parse_cell_date(m.group(0).strip())
                if d:
                    row_date = d
                    text = text[m.end():]
            if row_date is None:
                # 行番号付き（「15 08.04.30 保険 …」）の先頭日付
                m = _CELL_ROWNUM_DATE_PREFIX.match(text)
                if m:
                    d = _era_or_western(int(m.group(2)), int(m.group(3)), int(m.group(4)))
                    if d:
                        row_date = d
                        text = text[m.end():]
            if row_date is None:
                m = _CELL_MD_PREFIX.match(text)
                if m:
                    mo, d = int(m.group(1)), int(m.group(2))
                    if 1 <= mo <= 12 and 1 <= d <= 31:
                        month_day = (mo, d)
                        text = text[m.end():]

        # 3') 混在セル: 埋め込みの金額を抜き出し、残りを摘要にする
        for am in _EMBEDDED_AMOUNT.finditer(text):
            token = am.group(0).strip("¥￥円 ").replace(",", "")
            if token.isdigit():
                value = int(token)
                if 1 <= value <= 1_000_000_000:
                    amounts.append((value, cell.x))
        text = _EMBEDDED_AMOUNT.sub(" ", text).strip()

        if text:
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


def _parse_bankbook(
    rows: list[list[OcrLine]],
    result: ParseResult,
    custom_expense_rules: list[tuple[str, str]] | None = None,
    custom_income_rules: list[tuple[str, str]] | None = None,
) -> None:
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
            account, unknown = estimate_income_account(desc, custom_income_rules)
            debit_account, credit_account = BANK_ACCOUNT, account
        else:
            account, unknown = estimate_expense_account(desc, custom_expense_rules)
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


# カード明細で取引行と誤認しやすい行（合計・請求サマリ）は仕訳にしない
_CARD_SKIP_KEYWORDS = (
    "合計", "小計", "ご請求", "請求金額", "請求額", "お支払", "支払金額",
    "総額", "リボ", "繰越", "残高",
)

# カード明細の摘要から取り除くノイズ（利用者・支払方法の列）
_CARD_NOISE = re.compile(r"^(本人|家族|\d+回払い|１回払い|分割払い|リボ払い|ボーナス払い)$")


def _clean_card_description(desc: str) -> str:
    tokens = [t for t in desc.split() if not _CARD_NOISE.fullmatch(t)]
    return " ".join(tokens)


def _parse_card(
    rows: list[list[OcrLine]],
    result: ParseResult,
    custom_expense_rules: list[tuple[str, str]] | None = None,
) -> None:
    """カード明細の明細を解析する。日付＋摘要＋利用額の行を1取引とする。

    行には「利用金額」と「手数料」（1回払いなら0円）の複数の金額が並ぶことが
    あるため、0円を除いた最大値を利用額とみなす。
    """
    resolver = _RowDateResolver(rows, result)
    for cells in rows:
        full_date, month_day, desc, amounts = _split_row(cells)
        row_date, year_assumed = resolver.resolve(full_date, month_day)
        if row_date is None or not amounts:
            continue  # 見出し・合計行など
        if any(kw in desc for kw in _CARD_SKIP_KEYWORDS):
            continue  # 合計・請求サマリ行は取引ではない
        non_zero = [a for a, _x in amounts if a > 0]
        if not non_zero:
            continue  # 利用額0円の行（手数料のみ等）は取引にしない
        amount = max(non_zero)
        desc = _clean_card_description(desc)
        account, unknown = estimate_expense_account(desc, custom_expense_rules)
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


# --- 給与台帳の解析 ---
#
# 会計事務所の指示に基づき、給与台帳の「合計」列から諸口勘定を相手にした
# 仕訳一式を起こす（発生主義: 支給月分の月末日付）:
#   給与手当/諸口、旅費交通費(非課税分)/諸口、
#   諸口/預り金(社会保険・労働保険・源泉所得税・住民税・土建組合・社員積立)、
#   預り金(源泉所得税)/諸口（還付）、諸口/未払費用(給料)
# 最後に諸口の貸借一致を検算し、合わなければ全行要確認にする。

_SHOKUCHI = "諸口"

# (正規化テキストに含まれるキーワード, 除外キーワード, 項目キー) の順に判定
_PAYROLL_LABELS = [
    ("月例給与計", (), "salary"),
    ("非課税分賃金", (), "nontaxable"),
    ("健康保険", (), "health"),
    ("厚生年金", (), "pension"),
    ("雇用保険", (), "employment"),
    ("所得税", ("還付",), "income_tax"),
    ("市民村民税", (), "resident_tax"),
    ("市民税", (), "resident_tax"),
    ("住民税", (), "resident_tax"),
    ("土建組合", (), "dokken"),
    ("社員旅行積立", (), "tsumitate"),
    ("源泉所得税還付", (), "refund"),
    ("差引支給額", (), "net_pay"),
]

# 金額行を無視すべき見出し（対象外の集計行など）
_PAYROLL_IGNORE_LABELS = ("支給額合計", "小計", "差引控除後", "単価", "日数", "残業単価", "残業時間", "基本給", "役職手当", "手当")


def _payroll_month_end(rows: list[list[OcrLine]], result: ParseResult) -> date:
    """給与台帳の「支給月分」と年の記載から月末日付を決める（発生主義）。"""
    import calendar

    all_text = " ".join(c.text for cells in rows for c in cells)
    year = None
    is_nendo = "年度" in all_text
    m = re.search(r"令和\s*(\d{1,2})\s*年", all_text)
    if m:
        year = int(m.group(1)) + 2018
    else:
        m = re.search(r"(20\d{2})\s*年", all_text)
        if m:
            year = int(m.group(1))

    month = None
    m = re.search(r"支給月分\s*(\d{1,2})\s*月", all_text)
    if m:
        month = int(m.group(1))
    else:
        # 「12月」が従業員数ぶん並ぶ行（支給月分の行）を探す
        for cells in rows:
            months = [c.text for c in cells if re.fullmatch(r"\d{1,2}月", c.text.strip())]
            if len(months) >= 2:
                month = int(months[0].rstrip("月"))
                break

    if month is None:
        month = date.today().month
        result.warnings.append("支給月分を特定できなかったため今月と仮定しました。日付を確認してください。")
    if year is None:
        year = date.today().year
        result.warnings.append("年を特定できなかったため本年と仮定しました。日付を確認してください。")
    elif is_nendo and month <= 3:
        year += 1  # 年度表記の1〜3月は翌暦年

    return date(year, month, calendar.monthrange(year, month)[1])


def parse_payroll(rows: list[list[OcrLine]], source_name: str = "") -> ParseResult:
    """給与台帳（従業員別の列＋合計列）から給与仕訳一式を起こす。"""
    result = ParseResult()
    entry_date = _payroll_month_end(rows, result)

    # 「ラベル行 → 金額行」の並びを前提に、合計列（行の右端の金額）を拾う
    values: dict[str, int] = {}
    pending: str | None = None
    for cells in rows:
        normalized = "".join(c.text for c in cells).replace(" ", "").replace("　", "")
        amounts = [a for c in cells if (a := _parse_cell_amount(c.text)) is not None]

        label_key = None
        for keyword, excludes, key in _PAYROLL_LABELS:
            if keyword in normalized and not any(ex in normalized for ex in excludes):
                label_key = key
                break

        if label_key is not None:
            if amounts:  # ラベルと金額が同じ行にあるレイアウト
                values.setdefault(label_key, amounts[-1])
                pending = None
            else:
                pending = label_key
            continue

        if any(ig in normalized for ig in _PAYROLL_IGNORE_LABELS):
            pending = None
            continue

        if amounts and pending is not None:
            values.setdefault(pending, amounts[-1])
            pending = None

    if "salary" not in values:
        result.warnings.append(
            "給与台帳から「①月例給与計」を読み取れませんでした。レイアウトの調整が必要です"
            "（OCR結果を共有してください）。"
        )
        return result

    month_label = f"{entry_date.month}月分給与"

    def add(debit, debit_sub, credit, credit_sub, amount, desc):
        if not amount:
            return
        result.entries.append(
            JournalEntry(
                date=entry_date,
                debit_account=debit,
                debit_sub=debit_sub,
                credit_account=credit,
                credit_sub=credit_sub,
                amount=amount,
                description=desc,
                debit_tax=yayoi_tax(debit),
                credit_tax=yayoi_tax(credit),
            )
        )

    add("給与手当", "", _SHOKUCHI, "", values.get("salary", 0), month_label)
    add("旅費交通費", "", _SHOKUCHI, "", values.get("nontaxable", 0), f"{month_label} 非課税通勤費")
    add(_SHOKUCHI, "", "預り金", "社会保険",
        values.get("health", 0) + values.get("pension", 0), f"{month_label} 社会保険料")
    add(_SHOKUCHI, "", "預り金", "労働保険", values.get("employment", 0), f"{month_label} 雇用保険料")
    add(_SHOKUCHI, "", "預り金", "源泉所得税", values.get("income_tax", 0), f"{month_label} 源泉所得税")
    add(_SHOKUCHI, "", "預り金", "住民税", values.get("resident_tax", 0), f"{month_label} 住民税")
    add(_SHOKUCHI, "", "預り金", "土建組合", values.get("dokken", 0), f"{month_label} 土建組合費")
    add(_SHOKUCHI, "", "預り金", "社員積立", values.get("tsumitate", 0), f"{month_label} 社員旅行積立")
    add("預り金", "源泉所得税", _SHOKUCHI, "", values.get("refund", 0), f"{month_label} 源泉所得税還付")
    add(_SHOKUCHI, "", "未払費用", "給料", values.get("net_pay", 0), f"{month_label} 差引支給額")

    # 諸口の貸借一致を検算（通帳の残高チェックと同じ思想の品質担保）
    shokuchi_debit = sum(e.amount for e in result.entries if e.debit_account == _SHOKUCHI)
    shokuchi_credit = sum(e.amount for e in result.entries if e.credit_account == _SHOKUCHI)
    if shokuchi_debit != shokuchi_credit:
        for e in result.entries:
            e.needs_review = True
            e.note = "諸口の貸借不一致"
        result.warnings.append(
            f"諸口の貸借が一致しません（借方 {shokuchi_debit:,} 円 / 貸方 {shokuchi_credit:,} 円）。"
            "読み取り誤りの可能性があるため全行を要確認にしました。"
        )
    return result


def parse_table_document(
    rows: list[list[OcrLine]],
    document_type: str,
    source_name: str = "",
    custom_expense_rules: list[tuple[str, str]] | None = None,
    custom_income_rules: list[tuple[str, str]] | None = None,
) -> ParseResult:
    """座標で復元した表の行から、通帳・カード明細の仕訳を起こす。"""
    result = ParseResult()
    if document_type == "通帳":
        _parse_bankbook(rows, result, custom_expense_rules, custom_income_rules)
    elif document_type == "カード明細":
        _parse_card(rows, result, custom_expense_rules)
    else:
        result.warnings.append(f"書類タイプ「{document_type}」は表形式解析の対象外です。")
        return result

    if not result.entries:
        result.warnings.append(
            "明細行を検出できませんでした。OCR結果を確認してください"
            "（レイアウトによっては調整が必要です。サンプルを共有してください）。"
        )
    return result
