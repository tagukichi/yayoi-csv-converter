"""エンジン部分（勘定科目推定・弥生CSV出力）の単体テスト。

pytest を入れていない環境でも動くよう、素のスクリプトとして実行できる:
    python tests/test_export.py
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from accounts import estimate_expense_account, estimate_income_account  # noqa: E402
from models import JournalEntry  # noqa: E402
from yayoi_exporter import ENCODING, HEADER, to_yayoi_csv  # noqa: E402


def test_account_estimation():
    assert estimate_expense_account("ETC利用料")[0] == "旅費交通費"
    assert estimate_expense_account("東京電力 電気料金")[0] == "水道光熱費"
    assert estimate_expense_account("NTT 通信料")[0] == "通信費"
    # 推定できない摘要は要確認フラグが立つ
    account, needs_review = estimate_expense_account("謎の支払い")
    assert needs_review is True
    assert estimate_income_account("売掛金 入金")[0] == "売掛金"


def test_yayoi_csv_structure():
    entries = [
        JournalEntry(
            date=date(2026, 4, 1),
            debit_account="旅費交通費",
            credit_account="普通預金",
            amount=1500,
            description="ETC利用料",
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

    # ヘッダ + 2明細行
    assert len(lines) == 3
    # 25列ある
    assert len(lines[0].split(",")) == len(HEADER) == 25
    # 明細行は識別フラグ "2000" で始まる
    assert lines[1].startswith("2000,")
    # 金額が借方・貸方の両方に入っている
    assert "1500" in lines[1]
    assert "715000" in lines[2]


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
