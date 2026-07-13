"""エンジン部分（勘定科目推定・消費税区分・弥生CSV出力）の単体テスト。

pytest を入れていない環境でも動くよう、素のスクリプトとして実行できる:
    python tests/test_export.py
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from accounts import (  # noqa: E402
    estimate_expense_account,
    estimate_income_account,
    yayoi_tax,
)
from models import JournalEntry  # noqa: E402
from yayoi_exporter import ENCODING, HEADER, to_yayoi_csv  # noqa: E402


def test_account_estimation():
    # 科目名は会計事務所の科目体系（手数料・交際接待費・給与手当など）に準拠
    assert estimate_expense_account("ETC利用料")[0] == "旅費交通費"
    assert estimate_expense_account("東京電力 電気料金")[0] == "水道光熱費"
    assert estimate_expense_account("NTT 通信料")[0] == "通信費"
    assert estimate_expense_account("振込手数料")[0] == "手数料"
    assert estimate_expense_account("居酒屋〇〇")[0] == "交際接待費"
    assert estimate_expense_account("ENEOS ガソリン")[0] == "車輌費"
    assert estimate_expense_account("ヤマト運輸 宅急便")[0] == "荷造運賃"
    # 推定できない摘要は要確認フラグが立つ
    account, needs_review = estimate_expense_account("謎の支払い")
    assert needs_review is True
    assert estimate_income_account("売掛金 入金")[0] == "売掛金"


def test_yayoi_tax_categories():
    # 会計事務所の指示: 租税公課→不課税、支払利息・保険料→非課税、他はほぼ課税
    assert yayoi_tax("消耗品費") == "課対仕入込10%"
    assert yayoi_tax("旅費交通費") == "課対仕入込10%"
    assert yayoi_tax("保険料") == "非課仕入"
    assert yayoi_tax("支払利息") == "非課仕入"
    assert yayoi_tax("租税公課") == "対象外"  # 不課税は弥生上は対象外
    # BS科目は常に対象外
    assert yayoi_tax("現金") == "対象外"
    assert yayoi_tax("普通預金") == "対象外"
    assert yayoi_tax("売掛金") == "対象外"
    assert yayoi_tax("未払金") == "対象外"
    # 収益科目は売上側の文字列
    assert yayoi_tax("売上高") == "課税売上込10%"
    assert yayoi_tax("受取利息") == "非課売上"
    # 未知の科目は費用・課税10%扱い
    assert yayoi_tax("謎の科目") == "課対仕入込10%"


def test_yayoi_csv_structure():
    entries = [
        JournalEntry(
            date=date(2026, 4, 1),
            debit_account="旅費交通費",
            credit_account="普通預金",
            amount=1500,
            description="ETC利用料",
            debit_tax="課対仕入込10%",
            credit_tax="対象外",
        ),
        JournalEntry(
            date=date(2026, 4, 3),
            debit_account="普通預金",
            credit_account="売掛金",
            amount=715000,
            description="売掛金回収 もくとさい農園",
        ),
    ]
    raw = to_yayoi_csv(entries)

    # Shift-JIS で復号できる（＝弥生が読める文字コード）
    text = raw.decode(ENCODING)
    lines = text.strip().split("\r\n")

    # 既定はヘッダなし（弥生はヘッダ行を読み飛ばさない）で明細2行のみ
    assert len(lines) == 2
    # テンプレートCSVと同じ27列
    assert len(lines[0].split(",")) == len(HEADER) == 27
    # 明細行は識別フラグ "2000" で始まる
    assert lines[0].startswith("2000,")
    # 日付はテンプレートと同じ和暦形式（2026年 = 令和8年）
    assert "R.08/04/01" in lines[0]
    assert "R.08/04/03" in lines[1]
    # 金額と税区分
    assert "1500" in lines[0]
    assert "課対仕入込10%" in lines[0]
    assert "715000" in lines[1]

    # ヘッダ付き（確認用）も選べる
    with_header = to_yayoi_csv(entries, include_header=True).decode(ENCODING)
    assert with_header.startswith("識別フラグ,")


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
