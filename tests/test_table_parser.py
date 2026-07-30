"""表形式解析（通帳・カード明細）と行復元（group_rows）のテスト。

    python tests/test_table_parser.py
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ocr import OcrLine, group_rows  # noqa: E402
from parser import parse_table_document  # noqa: E402


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
