"""表形式解析（通帳・カード明細）と行復元（group_rows）のテスト。

    python tests/test_table_parser.py
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ocr import OcrLine, group_rows  # noqa: E402
from doc_parser import parse_table_document  # noqa: E402


def _line(text, x, y, page=1, height=10.0):
    return OcrLine(text=text, x=x, y=y, height=height, page=page)


def _row(y, *cells):
    """(x, text) のタプル列から1行分の OcrLine を作る。"""
    return [_line(text, x, y) for x, text in cells]


def test_group_rows_clusters_by_y():
    lines = [
        # 1行目（Yに±2のゆらぎ）
        _line("08-04-01", 10, 100),
        _line("振込 タナカ", 60, 102),
        _line("150,000", 200, 99),
        # 2行目
        _line("08-04-10", 10, 130),
        _line("電気料金", 60, 131),
    ]
    rows = group_rows(lines)
    assert len(rows) == 2
    assert [c.text for c in rows[0]] == ["08-04-01", "振込 タナカ", "150,000"]
    assert [c.text for c in rows[1]] == ["08-04-10", "電気料金"]


def test_bankbook_balance_continuity():
    rows = [
        _row(50, (10, "日付"), (60, "摘要"), (150, "お支払金額"), (220, "お預り金額"), (300, "残高")),
        _row(80, (60, "繰越"), (300, "1,000,000")),
        # 入金: 1,000,000 + 150,000 = 1,150,000
        _row(110, (10, "08-04-01"), (60, "振込 カ)タナカ"), (220, "150,000"), (300, "1,150,000")),
        # 出金: 1,150,000 - 8,000 = 1,142,000
        _row(140, (10, "08-04-10"), (60, "デンキ料金"), (150, "8,000"), (300, "1,142,000")),
    ]
    result = parse_table_document(rows, "通帳")
    assert len(result.entries) == 2

    deposit = result.entries[0]
    assert deposit.date == date(2026, 4, 1)  # 和暦08 → 令和8年 = 2026
    assert deposit.debit_account == "普通預金"
    assert deposit.credit_account == "売掛金"  # 「振込」から推定
    assert deposit.amount == 150000
    assert deposit.needs_review is False  # 残高が合致し科目も推定できた

    withdraw = result.entries[1]
    assert withdraw.debit_account == "水道光熱費"  # 「デンキ」から推定
    assert withdraw.credit_account == "普通預金"
    assert withdraw.amount == 8000
    assert withdraw.needs_review is False


def test_bankbook_balance_mismatch_flagged():
    rows = [
        _row(80, (60, "繰越"), (300, "1,000,000")),
        # 残高が合わない行（OCR誤読を想定）→ 要確認
        _row(110, (10, "08-04-01"), (60, "何かの支払"), (150, "5,000"), (300, "999,000")),
    ]
    result = parse_table_document(rows, "通帳")
    assert len(result.entries) == 1
    assert result.entries[0].needs_review is True
    assert any("残高" in w for w in result.warnings)


def test_bankbook_with_row_numbers():
    """左端の行番号が日付にくっつく通帳（実物のフィードバックを再現）。

    「108.03.25」= 行番号1 + 令和8年3月25日。従来はこれが西暦1008年等に
    誤解釈され、行の欠落や日付異常（1008/04/24）が起きていた。
    """
    rows = [
        _row(50, (5, "年月日"), (60, "摘要"), (150, "お支払金額"), (220, "お預り金額"), (300, "差引残高")),
        _row(80, (5, "108.03.25"), (60, "繰越"), (300, "¥560,856")),
        _row(110, (5, "208.03.26"), (60, "保険"), (150, "17,090"), (220, "トウキヨウカイシ ヨウニ"), (300, "¥543,766")),
        # 入金: 543,766 + 300,000 = 843,766
        _row(140, (5, "708.04.24"), (60, "振込 サカイ リヨウ"), (220, "300,000"), (300, "¥843,766")),
        # 行番号・日付・摘要が1セルに結合したケース: 843,766 - 29,565 = 814,201
        _row(170, (5, "15 08.04.30 保険 シヤカイホケンリヨウ"), (150, "29,565"), (300, "¥814,201")),
    ]
    result = parse_table_document(rows, "通帳")
    assert len(result.entries) == 3

    first = result.entries[0]
    assert first.date == date(2026, 3, 26)  # 行番号2を除いた 08.03.26
    assert first.debit_account == "保険料"
    assert first.amount == 17090
    assert first.needs_review is False  # 残高チェック成立

    deposit = result.entries[1]
    assert deposit.date == date(2026, 4, 24)
    assert deposit.debit_account == "普通預金"
    assert deposit.amount == 300000

    merged = result.entries[2]
    assert merged.date == date(2026, 4, 30)  # 結合セルから日付を抽出
    assert merged.debit_account == "保険料"
    assert merged.amount == 29565
    assert merged.needs_review is False


def test_card_statement():
    rows = [
        _row(50, (10, "利用日"), (60, "利用店名"), (200, "利用金額")),
        _row(80, (10, "2026/04/02"), (60, "ETC利用"), (200, "1,500")),
        _row(110, (10, "2026/04/05"), (60, "謎の店"), (200, "3,000")),
        _row(140, (60, "合計"), (200, "4,500")),  # 日付がないため取引にしない
    ]
    result = parse_table_document(rows, "カード明細")
    assert len(result.entries) == 2
    assert result.entries[0].debit_account == "旅費交通費"
    assert result.entries[0].credit_account == "未払金"
    assert result.entries[0].needs_review is False
    # 科目を推定できなかった行は要確認
    assert result.entries[1].needs_review is True


def test_card_statement_merged_cells():
    """OCRが日付・店名・金額を1つの行テキストに結合しても各行を取引にできる。"""
    rows = [
        _row(30, (10, "ご利用明細 2026年")),
        _row(80, (10, "04/02 セブン-イレブン江東亀戸店 1,500")),
        _row(110, (10, "04/15 ENEOS セルフ亀戸SS 5,800")),
        _row(140, (10, "05/01 ご請求金額 7,300")),  # 合計行は取引にしない
    ]
    result = parse_table_document(rows, "カード明細")
    assert len(result.entries) == 2

    first = result.entries[0]
    assert first.date == date(2026, 4, 2)
    assert first.amount == 1500
    assert "セブン-イレブン" in first.description
    assert first.credit_account == "未払金"

    second = result.entries[1]
    assert second.date == date(2026, 4, 15)
    assert second.amount == 5800
    assert second.debit_account == "車輌費"  # ENEOS → 車輌費


def test_card_statement_fee_column_and_noise():
    """利用金額と手数料(0円)が並ぶ行は利用金額を採用し、摘要のノイズを除去。"""
    rows = [
        _row(80, (10, "2026/06/03"), (60, "ETC"), (100, "首都高速道路"),
             (200, "本人"), (240, "1回払い"), (300, "1,320"), (360, "0")),
        _row(110, (10, "2026/06/20"), (60, "ガスト"), (100, "多摩店"),
             (200, "本人"), (240, "1回払い"), (300, "4,280"), (360, "0")),
    ]
    result = parse_table_document(rows, "カード明細")
    assert len(result.entries) == 2
    first = result.entries[0]
    assert first.amount == 1320  # 手数料の0ではなく利用金額
    assert "本人" not in first.description and "1回払い" not in first.description
    # 「ガスト」は「ガス」(水道光熱費)に誤マッチせず交際接待費になる
    assert result.entries[1].debit_account == "交際接待費"


def test_card_statement_with_learned_rule():
    """学習ルールがあれば「謎の店」も要確認にならず科目が付く。"""
    rows = [
        _row(80, (10, "2026/04/05"), (60, "謎の店"), (200, "3,000")),
    ]
    result = parse_table_document(
        rows, "カード明細", custom_expense_rules=[("謎の店", "会議費")]
    )
    assert result.entries[0].debit_account == "会議費"
    assert result.entries[0].needs_review is False


def test_card_statement_skips_summary_rows():
    """日付付きの合計・請求サマリ行は仕訳にしない。"""
    rows = [
        _row(80, (10, "2026/04/02"), (60, "お支払金額"), (200, "45,000")),
        _row(110, (10, "2026/04/02"), (60, "リボ残高"), (200, "120,000")),
    ]
    result = parse_table_document(rows, "カード明細")
    assert result.entries == []


def test_bankbook_month_day_dates_with_year_hint():
    """年なし日付（6-02）の通帳。見出しに年があれば補完される。"""
    rows = [
        _row(30, (10, "2026年6月分 取引明細")),
        _row(80, (60, "繰越"), (300, "500,000")),
        # 入金: 500,000 + 200,000 = 700,000
        _row(110, (10, "6-02"), (60, "フリコミ タマケンセツ"), (220, "200,000"), (300, "700,000")),
        # 出金: 700,000 - 3,000 = 697,000
        _row(140, (10, "6-15"), (60, "テスウリョウ"), (150, "3,000"), (300, "697,000")),
    ]
    result = parse_table_document(rows, "通帳")
    assert len(result.entries) == 2

    deposit = result.entries[0]
    assert deposit.date == date(2026, 6, 2)  # 見出しの「2026年」から補完
    assert deposit.debit_account == "普通預金"
    assert deposit.credit_account == "売掛金"  # 「フリコミ」から推定
    assert deposit.needs_review is False  # 年が特定でき、残高も一致

    withdraw = result.entries[1]
    assert withdraw.date == date(2026, 6, 15)
    assert withdraw.debit_account == "手数料"  # 「テスウリョウ」から推定（事務所の科目名）
    assert withdraw.debit_tax == "課対仕入込10%"
    assert withdraw.credit_tax == "対象外"  # 普通預金


def test_bankbook_month_day_without_year_hint():
    """年の記載が一切ない場合は本年と仮定し、要確認＋警告になる。"""
    rows = [
        _row(80, (60, "繰越"), (300, "500,000")),
        _row(110, (10, "6-02"), (60, "フリコミ タマケンセツ"), (220, "200,000"), (300, "700,000")),
    ]
    result = parse_table_document(rows, "通帳")
    assert len(result.entries) == 1
    assert result.entries[0].date.month == 6
    assert result.entries[0].needs_review is True  # 年を仮定したため
    assert any("年を特定できなかった" in w for w in result.warnings)


def test_bankbook_year_rollover():
    """12月→1月の年またぎで年が進む。"""
    rows = [
        _row(30, (10, "2026年12月")),
        _row(80, (60, "繰越"), (300, "100,000")),
        _row(110, (10, "12-28"), (60, "フリコミ A"), (220, "50,000"), (300, "150,000")),
        _row(140, (10, "1-05"), (60, "フリコミ B"), (220, "10,000"), (300, "160,000")),
    ]
    result = parse_table_document(rows, "通帳")
    assert result.entries[0].date == date(2026, 12, 28)
    assert result.entries[1].date == date(2027, 1, 5)


def test_payroll_ledger():
    """給与台帳（会計事務所のサンプルPDFの実数値）から諸口仕訳一式を起こす。"""
    from doc_parser import parse_payroll

    def label_row(y, label):
        return _row(y, *[(10 + i * 60, label) for i in range(3)])

    def amount_row(y, *amounts):
        return _row(y, *[(10 + i * 60, a) for i, a in enumerate(amounts)])

    rows = [
        _row(10, (10, "令和3年度給与"), (200, "給与台帳")),
        _row(30, (10, "支給月分")),
        _row(40, (10, "12月"), (70, "12月"), (130, "12月"), (190, "合計")),
        label_row(60, "基本給"),
        amount_row(70, "1,000,000", "300,000", "3,204,000"),
        label_row(90, "①月例給与計"),
        amount_row(100, "1,000,000", "300,000", "3,206,656"),
        label_row(110, "②非課税分賃金額"),
        label_row(120, "③支給額合計（①＋②）"),
        amount_row(130, "1,000,000", "300,000", "3,206,656"),
        label_row(150, "健康保険"),
        amount_row(160, "57,771", "17,685", "188,050"),
        label_row(170, "厚生年金保険及び基金掛金等"),
        amount_row(180, "59,475", "27,450", "261,690"),
        label_row(190, "雇用保険"),
        amount_row(200, "1,224", "1,632", "7,616"),
        label_row(210, "④小計"),
        amount_row(220, "117,246", "45,135", "457,356"),
        label_row(230, "⑤差引控除後の金額①－④"),
        amount_row(240, "882,754", "254,865", "2,749,300"),
        label_row(250, "所得税"),
        amount_row(260, "106,507", "6,750", "166,987"),
        label_row(270, "市民村民税"),
        amount_row(280, "60,900", "12,000", "160,700"),
        label_row(290, "土建組合等"),
        label_row(300, "社員旅行積立"),
        label_row(310, "⑥小計"),
        amount_row(320, "167,407", "18,750", "327,687"),
        label_row(330, "⑦源泉所得税還付金"),
        amount_row(340, "396,884", "12,700", "459,936"),
        label_row(350, "差引支給額③－④－⑥＋⑦"),
        amount_row(360, "1,112,231", "248,815", "2,881,549"),
        _row(380, (10, "単価"), (70, "17000"), (130, "17000")),
    ]
    result = parse_payroll(rows)

    by_desc = {e.description: e for e in result.entries}
    assert len(result.entries) == 7  # 非課税・土建・積立は0のため出ない

    e = by_desc["12月分給与"]
    assert (e.debit_account, e.credit_account, e.amount) == ("給与手当", "諸口", 3206656)
    assert e.date == date(2021, 12, 31)  # 令和3年度の12月 → 月末・発生主義
    assert e.debit_tax == "対象外"  # 給与は不課税

    shakai = by_desc["12月分給与 社会保険料"]
    assert shakai.amount == 188050 + 261690  # 健保＋厚年の合算
    assert (shakai.credit_account, shakai.credit_sub) == ("預り金", "社会保険")

    assert by_desc["12月分給与 雇用保険料"].credit_sub == "労働保険"
    assert by_desc["12月分給与 源泉所得税"].amount == 166987
    assert by_desc["12月分給与 住民税"].amount == 160700

    refund = by_desc["12月分給与 源泉所得税還付"]
    assert (refund.debit_account, refund.debit_sub, refund.credit_account) == ("預り金", "源泉所得税", "諸口")
    assert refund.amount == 459936

    net = by_desc["12月分給与 差引支給額"]
    assert (net.credit_account, net.credit_sub, net.amount) == ("未払費用", "給料", 2881549)

    # 諸口の貸借一致 → 全行チェック不要
    assert all(e.needs_review is False for e in result.entries)
    assert not any("諸口" in w for w in result.warnings)


def test_payroll_ledger_imbalance_flagged():
    """諸口の貸借が合わない（読み取り誤り想定）場合は全行要確認。"""
    from doc_parser import parse_payroll

    rows = [
        _row(10, (10, "2026年 給与台帳"), (100, "支給月分 6月")),
        _row(30, (10, "①月例給与計"), (100, "300,000")),
        _row(50, (10, "差引支給額"), (100, "250,000")),  # 控除なしなのに合わない
    ]
    result = parse_payroll(rows)
    assert len(result.entries) == 2
    assert all(e.needs_review for e in result.entries)
    assert any("諸口の貸借" in w for w in result.warnings)


def _run():
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK  {name}")
            passed += 1
    print(f"\n{passed} 件のテストに合格しました。")


if __name__ == "__main__":
    _run()
