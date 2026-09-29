"""部門（事前登録④・取り込み時の部門・仕訳の編集の部門切り替え・CSV出力）と
給与の控除項目ボタンのテスト。

    python tests/test_departments.py
"""

import csv
import io
import os
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage  # noqa: E402
from deptmaster import parse_department_csv, parse_department_lines  # noqa: E402
from models import JournalEntry  # noqa: E402
from yayoi_exporter import to_yayoi_csv  # noqa: E402

_UPLOADS = "/root/.claude/uploads/782d4722-b77f-5333-ba1b-55c013b4712d"
REAL_DEPT_CSV = f"{_UPLOADS}/64549bec-________.CSV"  # 弥生の仕訳CSV（部門付き・Kライフ提供のサンプル）


def _yayoi_row(debit_dept: str, credit_dept: str, desc: str = "") -> list[str]:
    row = ["2000", "1", "", "R.08/09/28", "売上値引高", "", debit_dept, "課税売返込10%",
           "10000", "0", "売掛金", "", credit_dept, "対象外", "10000", "0", desc,
           "", "", "0", "", "", "0", "0", "no"]
    return row


def _csv_bytes(rows, encoding="cp932") -> bytes:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue().encode(encoding)


# --- 部門の読み取り ---


def test_parse_real_yayoi_journal_csv():
    if not os.path.exists(REAL_DEPT_CSV):
        print("  (実CSVなし・スキップ)")
        return
    records = parse_department_csv(open(REAL_DEPT_CSV, "rb").read())
    assert records == [{"name": "ひみつ屋小作店"}]


def test_parse_yayoi_journal_csv_collects_both_sides():
    data = _csv_bytes([
        _yayoi_row("ひみつ屋小作店", "ひみつ屋小作店"),
        _yayoi_row("本社", ""),
        _yayoi_row("", "ひみつ屋羽村店"),
        _yayoi_row("", ""),  # 部門なしの仕訳
        _yayoi_row("本社", "本社"),
    ])
    names = [r["name"] for r in parse_department_csv(data)]
    assert names == ["ひみつ屋小作店", "本社", "ひみつ屋羽村店"]


def test_parse_csv_with_header_and_utf8():
    data = _csv_bytes([
        ["日付", "借方勘定科目", "借方部門", "貸方勘定科目", "貸方部門", "金額"],
        ["2026/09/01", "消耗品費", "営業部", "現金", "", "1000"],
        ["2026/09/02", "旅費交通費", "総務部", "現金", "総務部", "500"],
    ], encoding="utf-8-sig")
    assert [r["name"] for r in parse_department_csv(data)] == ["営業部", "総務部"]


def test_parse_department_list_csv():
    data = _csv_bytes([["部門コード", "部門名"], ["001", "本社"], ["002", "小作店"], ["002", "小作店"]])
    assert [r["name"] for r in parse_department_csv(data)] == ["本社", "小作店"]
    data = "本社\n小作店\n\n羽村店\n".encode("cp932")
    assert [r["name"] for r in parse_department_csv(data)] == ["本社", "小作店", "羽村店"]


def test_parse_department_lines_from_pdf_or_image():
    lines = [
        "部門一覧表", "作成日 2026年9月28日", "コード 部門名",
        "001 本社", "002 ひみつ屋小作店", "003", "営業部", "1/1頁", "本社",
    ]
    assert [r["name"] for r in parse_department_lines(lines)] == ["本社", "ひみつ屋小作店", "営業部"]


# --- 保存 ---


def test_department_master_storage():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        assert storage.list_departments("Kライフ", db_path=db) == []
        saved = storage.replace_departments(
            "Kライフ", [{"name": "本社"}, {"name": " 小作店 "}, {"name": "本社"}, {"name": ""}], db_path=db,
        )
        assert saved == 2
        assert storage.list_departments("Kライフ", db_path=db) == ["本社", "小作店"]
        assert storage.list_departments("別社", db_path=db) == []
        storage.add_client("Kライフ", db_path=db)
        storage.delete_client("Kライフ", db_path=db)
        assert storage.list_departments("Kライフ", db_path=db) == []


def test_entries_keep_departments():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        storage.add_entries("Kライフ", [
            JournalEntry(date=date(2026, 9, 28), debit_account="消耗品費", credit_account="現金",
                         amount=1000, debit_dept="小作店", credit_dept="小作店"),
            JournalEntry(date=date(2026, 9, 29), debit_account="雑費", credit_account="現金", amount=200),
        ], source_file="a.jpg", db_path=db)
        df = storage.load_entries("Kライフ", db_path=db)
        assert list(df["借方部門"]) == ["小作店", ""]
        assert list(df["貸方部門"]) == ["小作店", ""]
        # 表の編集（部門の変更・欠損値）を保存しても部門が残る
        df.loc[1, "借方部門"] = "本社"
        df.loc[1, "貸方部門"] = None
        storage.replace_entries("Kライフ", df, db_path=db)
        df = storage.load_entries("Kライフ", db_path=db)
        assert list(df["借方部門"]) == ["小作店", "本社"]
        assert list(df["貸方部門"]) == ["小作店", ""]


def test_old_database_gets_department_columns():
    """部門の列が無い既存のDBでも、開いたときに列が足される（仕訳は消えない）。"""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "old.db"
        conn = sqlite3.connect(db)
        conn.execute(
            """CREATE TABLE entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, client TEXT NOT NULL, date TEXT NOT NULL,
                debit_account TEXT NOT NULL, debit_sub TEXT NOT NULL DEFAULT '',
                debit_tax TEXT NOT NULL DEFAULT '対象外', credit_account TEXT NOT NULL,
                credit_sub TEXT NOT NULL DEFAULT '', credit_tax TEXT NOT NULL DEFAULT '対象外',
                amount INTEGER NOT NULL, description TEXT NOT NULL DEFAULT '',
                needs_review INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
                source_file TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')))"""
        )
        conn.execute(
            "INSERT INTO entries (client, date, debit_account, credit_account, amount) "
            "VALUES ('Kライフ', '2026/09/01', '雑費', '現金', 300)"
        )
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        conn.close()
        df = storage.load_entries("Kライフ", db_path=db)
        assert len(df) == 1 and df.loc[0, "借方部門"] == "" and df.loc[0, "金額"] == 300


# --- 弥生CSV出力 ---


def test_export_puts_departments_in_yayoi_columns():
    e = JournalEntry(date=date(2026, 9, 28), debit_account="売上値引高", credit_account="売掛金",
                     amount=10000, debit_dept="ひみつ屋小作店", credit_dept="ひみつ屋小作店")
    row = next(csv.reader(io.StringIO(to_yayoi_csv([e]).decode("cp932"))))
    assert row[6] == "ひみつ屋小作店"   # 借方部門（7列目）
    assert row[12] == "ひみつ屋小作店"  # 貸方部門（13列目）


# --- 仕訳の編集: 部門の切り替え ---


def test_ledger_department_filter_and_merge():
    import views

    full = pd.DataFrame({
        "摘要": ["a", "b", "c", "d"],
        "借方部門": ["本社", "", "小作店", ""],
        "貸方部門": ["本社", "小作店", "", ""],
        "要確認": [True, False, True, False],
    })
    assert list(views.filter_ledger(full, False, views.DEPT_ALL)["摘要"]) == ["a", "b", "c", "d"]
    assert list(views.filter_ledger(full, False, "小作店")["摘要"]) == ["b", "c"]  # 借方・貸方どちらか
    assert list(views.filter_ledger(full, False, views.DEPT_NONE)["摘要"]) == ["d"]
    assert list(views.filter_ledger(full, True, "小作店")["摘要"]) == ["c"]

    # 部門で絞り込んだ表示中の編集: 見えていない行は残り、追加行には部門が入る
    shown = views.filter_ledger(full, False, "小作店")
    edited = shown.drop(index=[1]).copy()
    edited.loc[2, "摘要"] = "c2"
    added = pd.DataFrame({"摘要": ["e"], "借方部門": [None], "貸方部門": [""], "要確認": [True]}, index=[99])
    edited = pd.concat([edited, added])
    merged = views._merge_editor_result(full, shown, edited, views._default_dept("小作店"))
    assert list(merged["摘要"]) == ["a", "c2", "d", "e"]
    assert merged.iloc[-1]["借方部門"] == "小作店" and merged.iloc[-1]["貸方部門"] == "小作店"
    assert views._default_dept(views.DEPT_ALL) == "" and views._default_dept(views.DEPT_NONE) == ""


# --- 給与の控除項目ボタン ---


def test_payroll_preset_button():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        assert storage.add_payroll_deduction("Kライフ", "駐車場代", "雑収入", db_path=db) == "added"
        assert storage.add_payroll_deduction("Kライフ", "駐車場代", "雑収入", db_path=db) == "exists"
        # 給与台帳から科目空欄で登録された項目は、ボタンで推奨の科目が入る
        storage.ensure_payroll_deductions("Kライフ", ["社宅"], db_path=db)
        assert storage.add_payroll_deduction("Kライフ", "社宅", "受取家賃", db_path=db) == "filled"
        assert storage.payroll_deduction_map("Kライフ", db_path=db) == {
            "駐車場代": ("雑収入", ""), "社宅": ("受取家賃", ""),
        }
        # 事務所が決めた科目は上書きしない
        storage.replace_payroll_deductions(
            "Kライフ", [{"label": "駐車場代", "account": "地代家賃", "sub_account": ""}], db_path=db
        )
        assert storage.add_payroll_deduction("Kライフ", "駐車場代", "雑収入", db_path=db) == "exists"
        assert storage.payroll_deduction_map("Kライフ", db_path=db)["駐車場代"] == ("地代家賃", "")


def test_presets_are_not_fixed_payroll_items():
    """プリセットに、給与台帳の解析が自動で扱う項目（所得税・社員旅行積立 等）を入れない。"""
    import views
    from doc_parser import _PAYROLL_LABELS

    fixed = [kw for kw, _ex, _key in _PAYROLL_LABELS]
    for label, account, _sub in views.PAYROLL_DEDUCTION_PRESETS:
        assert account
        assert not any(kw in label for kw in fixed), label


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
