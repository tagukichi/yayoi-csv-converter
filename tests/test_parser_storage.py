"""暫定解析（parser.py）と永続化（storage.py）のテスト。

    python tests/test_parser_storage.py
"""

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage  # noqa: E402
from models import JournalEntry  # noqa: E402
from parser import parse_document  # noqa: E402

# ユーザー提供の見積書PDFのOCR結果（抜粋）
SAMPLE_QUOTE_LINES = [
    "合計",
    "¥715,000",
    "項目", "摘要", "金額",
    "①", "不動産調査アプリ構築一式",
    "要件定義、技術選定、環境構築、 UI/UX設計、コーディング等", "550,000",
    "②", "案件管理アプリ構築一式",
    "要件定義、技術選定、環境構築、 UI/UX設計、コーディング等", "100,000",
    "小計", "650,000",
    "消費税", "65,000",
    "合計", "715,000",
]


def test_parse_receipt_total():
    result = parse_document(SAMPLE_QUOTE_LINES, "領収書", source_name="請求書.pdf")
    assert len(result.entries) == 1
    e = result.entries[0]
    assert e.amount == 715000  # 小計650,000ではなく合計を採用
    assert e.credit_account == "現金"
    assert e.needs_review is True
    # 日付がない書類なので警告が出る
    assert any("日付" in w for w in result.warnings)


def test_parse_with_date():
    lines = ["領収書", "2026年4月15日", "お品代として", "合計 ¥3,300"]
    result = parse_document(lines, "電子請求書")
    e = result.entries[0]
    assert e.date == date(2026, 4, 15)
    assert e.amount == 3300
    assert e.credit_account == "未払金"


def test_parse_reiwa_date():
    lines = ["令和8年4月1日", "¥1,000", "合計 ¥1,000"]
    result = parse_document(lines, "領収書")
    assert result.entries[0].date == date(2026, 4, 1)


def test_unsupported_doc_type():
    result = parse_document(["何か"], "通帳")
    assert result.entries == []
    assert any("未対応" in w for w in result.warnings)


def test_storage_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        entries = [
            JournalEntry(
                date=date(2026, 4, 1),
                debit_account="消耗品費",
                credit_account="現金",
                amount=1500,
                description="文具",
                needs_review=True,
            )
        ]
        assert storage.add_entries("A建設", entries, source_file="r.pdf", db_path=db) == 1
        # 別クライアントには影響しない
        assert storage.load_entries("B工務店", db_path=db).empty

        df = storage.load_entries("A建設", db_path=db)
        assert len(df) == 1
        assert df.iloc[0]["金額"] == 1500
        assert bool(df.iloc[0]["要確認"]) is True

        # 編集して置き換え → 反映される
        df.loc[0, "金額"] = 1800
        df.loc[0, "要確認"] = False
        assert storage.replace_entries("A建設", df, db_path=db) == 1
        df2 = storage.load_entries("A建設", db_path=db)
        assert df2.iloc[0]["金額"] == 1800
        assert bool(df2.iloc[0]["要確認"]) is False

        storage.clear_entries("A建設", db_path=db)
        assert storage.load_entries("A建設", db_path=db).empty


def test_client_management():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        # 初回は既定の企業が登録されている
        assert storage.list_clients(db_path=db) == ["A建設", "B工務店", "C社"]

        assert storage.add_client("D商事", db_path=db) is True
        assert "D商事" in storage.list_clients(db_path=db)
        # 重複・空欄は追加できない
        assert storage.add_client("D商事", db_path=db) is False
        assert storage.add_client("  ", db_path=db) is False

        # 削除すると仕訳も消える
        storage.add_entries(
            "D商事",
            [JournalEntry(date=date(2026, 4, 1), debit_account="雑費",
                          credit_account="現金", amount=100)],
            db_path=db,
        )
        storage.delete_client("D商事", db_path=db)
        assert "D商事" not in storage.list_clients(db_path=db)
        assert storage.load_entries("D商事", db_path=db).empty

        # 既定企業を全部消しても勝手に復活しない
        for name in ["A建設", "B工務店", "C社"]:
            storage.delete_client(name, db_path=db)
        assert storage.list_clients(db_path=db) == []


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
