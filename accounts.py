"""勘定科目の推定ルール（暫定）。

摘要テキストのキーワードから勘定科目を推定する。ここのマッピングは
「インターネット等で一般的とされる対応」を元にした暫定版であり、
最終的には会計事務所の確認・修正を前提とする。推定できなかった摘要は
既定科目（不明）を返し、needs_review を立てて人の確認に回す。
"""

from __future__ import annotations

# 推定できなかったときに使う仮の科目。弥生取込後に人が振り替える前提。
UNKNOWN_EXPENSE = "雑費"
UNKNOWN_INCOME = "売上高"

# (キーワードのリスト, 勘定科目) の対応表。上から順に最初に一致したものを採用する。
# キーワードは摘要に部分一致するかどうかで判定する（大文字小文字は無視）。
# 通帳の摘要はカタカナで印字されることが多いため、カタカナ表記も併記する。
EXPENSE_RULES: list[tuple[list[str], str]] = [
    (["etc", "高速", "駐車", "パーキング", "タクシー", "jr", "新幹線", "鉄道", "バス", "航空", "ana", "jal"], "旅費交通費"),
    (["電気", "ガス", "水道", "電力", "でんき", "デンキ", "スイドウ", "デンリョク"], "水道光熱費"),
    (["ntt", "携帯", "電話", "通信", "インターネット", "プロバイダ", "softbank", "docomo", "au", "切手", "郵便", "デンワ", "ツウシン"], "通信費"),
    (["amazon", "アスクル", "askul", "文具", "事務用品", "コピー用紙", "トナー"], "消耗品費"),
    (["家賃", "賃料", "テナント", "駐車場代", "ヤチン"], "地代家賃"),
    (["保険", "ホケン"], "保険料"),
    (["振込手数料", "手数料", "atm", "テスウリョウ"], "支払手数料"),
    (["接待", "会食", "懇親", "飲食", "レストラン", "居酒屋"], "接待交際費"),
    (["会議", "打合せ", "打ち合わせ", "カフェ", "喫茶"], "会議費"),
    (["新聞", "書籍", "図書", "セミナー", "研修"], "新聞図書費"),
    (["広告", "宣伝", "チラシ", "印刷"], "広告宣伝費"),
    (["給与", "給料", "賃金"], "給料手当"),
    (["外注", "業務委託"], "外注費"),
    (["税", "印紙"], "租税公課"),
]

INCOME_RULES: list[tuple[list[str], str]] = [
    (["売掛", "入金", "振込", "売上", "請求", "フリコミ", "ニュウキン", "ウリアゲ"], "売掛金"),
    (["利息", "利子", "リソク"], "受取利息"),
]


def _match(description: str, rules: list[tuple[list[str], str]]) -> str | None:
    text = description.lower()
    for keywords, account in rules:
        if any(kw.lower() in text for kw in keywords):
            return account
    return None


def estimate_expense_account(description: str) -> tuple[str, bool]:
    """出金（費用）側の勘定科目を推定する。

    戻り値: (勘定科目, needs_review)。推定できなければ既定科目と True を返す。
    """
    account = _match(description, EXPENSE_RULES)
    if account is None:
        return UNKNOWN_EXPENSE, True
    return account, False


def estimate_income_account(description: str) -> tuple[str, bool]:
    """入金（収益・債権回収）側の勘定科目を推定する。

    戻り値: (勘定科目, needs_review)。推定できなければ既定科目と True を返す。
    """
    account = _match(description, INCOME_RULES)
    if account is None:
        return UNKNOWN_INCOME, True
    return account, False
