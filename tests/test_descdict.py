"""摘要辞書（弥生の摘要科目一覧）のPDF読み取り・仕訳への適用・保存のテスト。

    python tests/test_descdict.py
"""

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage  # noqa: E402
from descdict import apply_desc_dictionary, dict_terms_by_account  # noqa: E402
from models import JournalEntry  # noqa: E402
from submaster import _parse_desc_dict_rows, parse_yayoi_desc_dict_pdf  # noqa: E402

REAL_PDF = "/root/.claude/uploads/782d4722-b77f-5333-ba1b-55c013b4712d/22327894-______.pdf"

# 実PDFと同じ形の辞書（抜粋）
DICT = [
    {"description": "駐車料", "account": "旅費交通費", "search_key": "64"},
    {"description": "駐車料", "account": "[製]旅費交通費", "search_key": "73"},
    {"description": "タクシー代", "account": "旅費交通費", "search_key": "65"},
    {"description": "飲食代", "account": "交際費", "search_key": "49"},
    {"description": "飲食代", "account": "会議費", "search_key": "60"},
    {"description": "飲食代", "account": "福利厚生費", "search_key": "35"},
    {"description": "お茶代", "account": "会議費", "search_key": "193"},
    {"description": "川崎幸ロータリークラブ", "account": "研修費", "search_key": "202"},
    {"description": "ATM手数料", "account": "支払手数料", "search_key": "170"},
    {"description": "社会保険", "account": "預り金", "search_key": "148"},
    {"description": "当月請求額 US東京", "account": "当期完成工事高", "search_key": "171"},
]


def test_parse_rows():
    rows = [
        ["摘", "要", "辞", "書", "1", "頁"],
        ["摘", "要", "勘定科目", "サーチキー数字", "非表示"],
        ["預金振替", "複合", "1"],
        ["当月請求額", "US東京", "当期完成工事高", "171"],  # 摘要に空白
        ["古い摘要", "雑費", "999", "○"],  # 非表示は取り込まない
        ["株式会社Ｋライフ"],
    ]
    records = _parse_desc_dict_rows(rows)
    assert records == [
        {"description": "預金振替", "account": "複合", "search_key": "1"},
        {"description": "当月請求額 US東京", "account": "当期完成工事高", "search_key": "171"},
    ]


def test_parse_real_pdf():
    if not os.path.exists(REAL_PDF):
        print("  (実PDFなし・スキップ)")
        return
    records = parse_yayoi_desc_dict_pdf(open(REAL_PDF, "rb").read())
    assert len(records) > 190
    by = {(r["description"], r["account"]) for r in records}
    assert ("駐車料", "旅費交通費") in by
    assert ("川崎幸ロータリークラブ", "研修費") in by
    assert ("当月請求額 US東京", "当期完成工事高") in by
    assert ("ATM手数料", "支払手数料") in by


def _entry(debit, desc, review=False, credit="現金"):
    return JournalEntry(
        date=date(2026, 7, 1), debit_account=debit, credit_account=credit, amount=1000,
        description=desc, debit_tax="課対仕入込10%", needs_review=review,
    )


def test_apply_same_account_sets_description():
    # 科目が一致 → 摘要だけ辞書の語に揃える（要確認はそのまま）
    e = _entry("旅費交通費", "えびす自動車（株）", review=True)
    assert apply_desc_dictionary([e], DICT, context_text="えびす自動車 タクシー 3,200円") == 1
    assert (e.debit_account, e.description, e.needs_review) == ("旅費交通費", "タクシー代", True)


def test_apply_unique_term_overrides_account():
    # 辞書の語がそのまま本文にあり、科目が一意 → 科目も辞書に合わせ要確認を外す
    e = _entry("諸会費", "川崎幸ロータリークラブ 会費")
    assert apply_desc_dictionary([e], DICT) == 1
    assert (e.debit_account, e.debit_tax, e.description, e.needs_review) == (
        "研修費", "課対仕入込10%", "川崎幸ロータリークラブ", False)
    # [製]の対があっても販管費側で一意とみなす（駐車料→旅費交通費）
    e = _entry("雑費", "パークエステートパーキング", review=True)
    assert apply_desc_dictionary([e], DICT, context_text="駐車料金 2,500円") == 1
    assert (e.debit_account, e.description, e.needs_review) == ("旅費交通費", "駐車料", False)


def test_apply_ambiguous_term_keeps_account():
    # 「飲食代」は複数の科目にある → 科目は変えない。同じ科目があれば摘要だけ揃える
    e = _entry("交際費", "居酒屋やまだ")
    assert apply_desc_dictionary([e], DICT, context_text="飲食代 5,000円") == 1
    assert (e.debit_account, e.description) == ("交際費", "飲食代")
    e = _entry("雑費", "居酒屋やまだ", review=True)
    assert apply_desc_dictionary([e], DICT, context_text="飲食代 5,000円") == 0
    assert (e.debit_account, e.needs_review) == ("雑費", True)


def test_apply_skips_bs_accounts_and_no_match():
    # BS科目（預り金）への差し替えはしない
    e = _entry("雑費", "社会保険料 納付", review=True)
    assert apply_desc_dictionary([e], DICT) == 0
    # 一致なし
    e = _entry("消耗品費", "ホームセンター")
    assert apply_desc_dictionary([e], DICT) == 0
    # 辞書なし
    assert apply_desc_dictionary([e], []) == 0


def test_terms_by_account():
    terms = dict_terms_by_account(DICT)
    assert terms["旅費交通費"] == ["駐車料", "タクシー代"]
    assert terms["会議費"] == ["飲食代", "お茶代"]


def test_storage_desc_dict():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        assert storage.list_desc_dict("A建設", db_path=db) == []
        saved = storage.replace_desc_dict(
            "A建設", DICT + [DICT[0], {"description": "", "account": "x"}], db_path=db
        )
        assert saved == len(DICT)  # 重複・空行は除外
        rows = storage.list_desc_dict("A建設", db_path=db)
        assert rows[0]["description"] == "駐車料" and rows[0]["account"] == "旅費交通費"
        assert storage.list_desc_dict("B工務店", db_path=db) == []


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
