"""補助科目マスタ（PDF読み取り・摘要との突合・保存）のテスト。

    python tests/test_submaster.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage  # noqa: E402
from submaster import (  # noqa: E402
    canonical_romaji,
    kana_to_romaji,
    match_subaccount,
    normalize_name,
    parse_yayoi_subaccount_pdf,
)

REAL_PDF = "/root/.claude/uploads/782d4722-b77f-5333-ba1b-55c013b4712d/11e7027d-______.pdf"

MASTER = [
    {"account": "普通預金", "sub_name": "川崎信用金庫", "search_key": "1"},
    {"account": "完成工事未収入金", "sub_name": "出口住設", "search_key": "deguti"},
    {"account": "完成工事未収入金", "sub_name": "ｴﾐｰﾙ介護センター", "search_key": "emil"},
    {"account": "工事未払金", "sub_name": "矢崎化工㈱", "search_key": "yaza"},
    {"account": "未払金", "sub_name": "オリコ", "search_key": "2"},
    {"account": "長期未払金", "sub_name": "トヨタファイナンス", "search_key": "1"},
]


def test_kana_romaji():
    # 表記ゆれ（ヘボン式/訓令式）は正規化後に同じ形になる
    assert canonical_romaji(kana_to_romaji("デグチジュウセツ")) == "degutizyuusetu"
    assert canonical_romaji("deguchijuusetsu") == "degutizyuusetu"
    assert canonical_romaji("deguti") == "deguti"
    # サーチキーの l はカナのローマ字化に現れないので r に寄せる
    assert canonical_romaji("emil") == "emir"
    # 促音は次の子音を重ねる
    assert kana_to_romaji("サッポロ") == "sapporo"


def test_normalize_name():
    assert normalize_name("㈱ケイズ") == "ケイズ".lower()
    assert normalize_name("カ)オリコプロダクト") == "オリコプロダクト".lower()
    assert normalize_name("ｴﾐｰﾙ介護センター") == "エミール介護センター".lower()


def test_match_by_name_and_key():
    # 名前の直接一致（確度高 → by=name）
    m = match_subaccount("カ)オリコプロダクト", MASTER, side="withdrawal")
    assert (m["account"], m["sub_name"], m["by"]) == ("未払金", "オリコ", "name")

    # サーチキー経由（カタカナ→ローマ字化して照合 → by=key）
    m = match_subaccount("フリコミ デグチジュウセツ", MASTER, side="deposit")
    assert (m["account"], m["sub_name"], m["by"]) == ("完成工事未収入金", "出口住設", "key")

    m = match_subaccount("ヤザキカコウ", MASTER, side="withdrawal")
    assert (m["account"], m["sub_name"]) == ("工事未払金", "矢崎化工㈱")

    # l を含むサーチキー
    m = match_subaccount("エミールカイゴセンター", MASTER, side="deposit")
    assert m and m["sub_name"] == "ｴﾐｰﾙ介護センター"

    # 入出金の向きで候補を絞る（入金にオリコ=未払金は出ない）
    assert match_subaccount("カ)オリコプロダクト", MASTER, side="deposit") is None
    # 一致なし
    assert match_subaccount("ナゾノトリヒキサキ", MASTER, side="withdrawal") is None


def test_parse_real_pdf():
    if not os.path.exists(REAL_PDF):
        print("  (実PDFなし・スキップ)")
        return
    records = parse_yayoi_subaccount_pdf(open(REAL_PDF, "rb").read())
    assert len(records) > 200
    accounts = {r["account"] for r in records}
    assert {"普通預金", "完成工事未収入金", "工事未払金", "預り金", "当期完成工事高"} <= accounts

    banks = [r["sub_name"] for r in records if r["account"] == "普通預金"]
    assert banks == [
        "川崎信用金庫", "川崎信用金庫049", "川崎信用金庫746",
        "湘南信用金庫", "神奈川銀行", "芝信用金庫",
    ]
    azukari = [r["sub_name"] for r in records if r["account"] == "預り金"]
    assert set(azukari) == {"社会保険料", "源泉所得税", "住民税", "労働保険", "積立金"}


def test_subaccounts_storage():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        assert storage.list_subaccounts("A建設", db_path=db) == []
        saved = storage.replace_subaccounts("A建設", MASTER, db_path=db)
        assert saved == len(MASTER)
        # 科目で絞り込める
        banks = storage.list_subaccounts("A建設", "普通預金", db_path=db)
        assert len(banks) == 1 and banks[0]["sub_name"] == "川崎信用金庫"
        # 別クライアントには影響しない
        assert storage.list_subaccounts("B工務店", db_path=db) == []
        # 置き換え（重複・空行は除外）
        saved = storage.replace_subaccounts(
            "A建設",
            [{"account": "普通預金", "sub_name": "川崎信用金庫", "search_key": "1"},
             {"account": "普通預金", "sub_name": "川崎信用金庫", "search_key": "1"},
             {"account": "", "sub_name": "x", "search_key": ""}],
            db_path=db,
        )
        assert saved == 1


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
