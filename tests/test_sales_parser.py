"""売上（売掛表）・請求書・買掛表の解析と、科目マスタ・紐付けのテスト。

    python tests/test_sales_parser.py
"""

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage  # noqa: E402
from sales_parser import (  # noqa: E402
    default_doctype_rule,
    parse_invoice,
    parse_partner_ledger,
    tabular_rows_from_bytes,
)
from submaster import _parse_account_rows, parse_yayoi_account_pdf  # noqa: E402

_UPLOADS = "/root/.claude/uploads/782d4722-b77f-5333-ba1b-55c013b4712d"
REAL_ACCOUNT_PDF = f"{_UPLOADS}/a61dc8ad-_____.pdf"  # 弥生 勘定科目一覧表（Kライフ）
REAL_INVOICE_XLSX = f"{_UPLOADS}/30deaad9-_______.xlsx"  # 売上請求書（Kライフ→1社）
REAL_LEDGER_XLSX = f"{_UPLOADS}/79a5e564-_______.xlsx"  # 売掛表（2025年10月）


# --- 書類タイプ→科目の既定値 ---


def test_default_doctype_rule():
    # 建設業の科目マスタがあればそちらを優先する
    names = ["完成工事未収入金", "工事未払金", "当期完成工事高", "外注費", "仕入高"]
    r = default_doctype_rule("売上", names)
    assert (r["debit_account"], r["credit_account"], r["sub_side"]) == (
        "完成工事未収入金", "当期完成工事高", "debit")
    r = default_doctype_rule("売上請求書", names)
    assert r["debit_account"] == "完成工事未収入金"
    r = default_doctype_rule("仕入請求書", names)
    assert (r["debit_account"], r["credit_account"], r["sub_side"]) == (
        "外注費", "工事未払金", "credit")
    # マスタが無ければ一般的な科目
    r = default_doctype_rule("売上", None)
    assert (r["debit_account"], r["credit_account"]) == ("売掛金", "売上高")
    r = default_doctype_rule("買掛表", None)
    assert (r["debit_account"], r["credit_account"]) == ("仕入高", "買掛金")


# --- 売掛表・買掛表 ---

# 実サンプルと同じ形（行番号 | 当月金額。0や空欄の行が混ざる）
_LEDGER_ROWS = [
    ["", "2025年", "", ""],
    ["", "10月", "", ""],
    ["1", "0", "", ""],
    ["2", "", "", ""],
    ["3", "603760", "", ""],
    ["4", "343903", "", ""],
    ["5", "281056", "", ""],
    ["備忘記録", "", "", ""],
]


def test_partner_ledger_numbered_rows():
    result, new_partners = parse_partner_ledger(
        _LEDGER_ROWS, "売上", source_name="売掛表.xlsx",
        rule={"debit_account": "完成工事未収入金", "credit_account": "当期完成工事高",
              "sub_side": "debit"},
        partner_map={3: "出口住設", 4: "矢崎化工㈱"},
        subaccounts=[{"account": "完成工事未収入金", "sub_name": "出口住設", "search_key": ""}],
    )
    # 金額が0・空欄の行は仕訳にしない
    assert len(result.entries) == 3
    e = result.entries[0]
    assert e.date == date(2025, 10, 31)  # 月末日付
    assert (e.debit_account, e.debit_sub) == ("完成工事未収入金", "出口住設")
    assert (e.credit_account, e.credit_tax) == ("当期完成工事高", "課税売上込10%")
    assert e.amount == 603760
    assert e.description == "出口住設 10月分売上"
    assert not e.needs_review
    # 対応表に無い行番号は No.5 で仮置きし要確認
    e5 = result.entries[2]
    assert e5.debit_sub == "No.5" and e5.needs_review
    assert any("行番号 5" in w for w in result.warnings)
    # マスタに無い取引先（矢崎化工㈱）だけが新規登録の対象になる
    assert new_partners == ["矢崎化工㈱"]


def test_partner_ledger_named_rows_and_purchase():
    rows = [
        ["買掛表", "令和7年", "11月"],
        ["出口住設", "100000"],
        ["㈱タマ建設", "55,000"],
        ["合計", "155000"],
    ]
    result, new_partners = parse_partner_ledger(
        rows, "買掛表",
        rule={"debit_account": "外注費", "credit_account": "工事未払金",
              "sub_side": "credit"},
        subaccounts=[{"account": "工事未払金", "sub_name": "出口住設", "search_key": ""}],
        custom_expense_rules=[("タマ建設", "仕入高")],
    )
    assert len(result.entries) == 2  # 合計行は読まない
    e = result.entries[0]
    assert e.date == date(2025, 11, 30)
    assert (e.debit_account, e.debit_tax) == ("外注費", "課対仕入込10%")
    assert (e.credit_account, e.credit_sub, e.credit_tax) == ("工事未払金", "出口住設", "対象外")
    assert e.description == "出口住設 11月分仕入"
    # 学習済みルールで取引先ごとに借方科目を変えられる（外注費→仕入高）
    e2 = result.entries[1]
    assert e2.debit_account == "仕入高"
    assert e2.amount == 55000
    # マスタに無い取引先だけ新規登録の対象
    assert new_partners == ["㈱タマ建設"]


def test_partner_ledger_no_month():
    result, _ = parse_partner_ledger([["1", "1000"]], "売上", source_name="x.csv")
    assert result.entries == []
    assert any("対象の月" in w for w in result.warnings)


# --- 請求書 ---

# 実サンプルと同じ構成（宛名+御中、発行者、当月合計額/消費税/請求金額）
_INVOICE_ROWS = [
    ["請　　求　　書"],
    ["", "", "", "令和", "7", "年", "10", "月", "31", "日"],
    ["1社", "", "", "御中", "", "株式会社Kライフ"],
    ["", "", "", "", "", "登録番号：T8020001102694"],
    ["前月繰越", "当月入金額", "調整額", "当月合計額", "消費税", "請求金額"],
    ["974205", "423064", "", "548875", "54885", "1154901"],
    ["当月合計金額", "", "603760"],
]


def test_invoice_sales():
    result, new_partners = parse_invoice(
        _INVOICE_ROWS, "売上請求書", client_name="株式会社Kライフ",
        rule={"debit_account": "完成工事未収入金", "credit_account": "当期完成工事高",
              "sub_side": "debit"},
    )
    assert len(result.entries) == 1
    e = result.entries[0]
    assert e.date == date(2025, 10, 31)
    # 金額は当月合計額+消費税（前月繰越や請求金額ではない）
    assert e.amount == 603760
    assert (e.debit_account, e.debit_sub) == ("完成工事未収入金", "1社")
    assert (e.credit_account, e.credit_tax) == ("当期完成工事高", "課税売上込10%")
    assert not e.needs_review
    assert new_partners == ["1社"]


def test_invoice_direction_autocorrect():
    # 宛名がクライアント自身 → 売上請求書を選んでいても仕入として仕訳する
    rows = [
        ["", "", "令和", "7", "年", "9", "月", "30", "日"],
        ["株式会社Kライフ", "", "御中"],
        ["株式会社タマ建材"],
        ["当月合計額", "100000", "消費税", "10000"],
    ]
    result, _ = parse_invoice(
        rows, "売上請求書", client_name="株式会社Kライフ",
        account_names=["外注費", "工事未払金"],
    )
    assert any("仕入請求書" in w for w in result.warnings)
    e = result.entries[0]
    assert e.amount == 110000
    assert (e.debit_account, e.credit_account) == ("外注費", "工事未払金")
    assert e.credit_sub == "株式会社タマ建材"  # 仕入は発行者が取引先


def test_invoice_purchase_from_ocr_lines():
    # OCR経由（1行=1セル）でも解析でき、要確認が立つ
    lines = [
        ["請求書"], ["株式会社Kライフ 御中"], ["有限会社サトウ電気"],
        ["2025年8月31日"], ["当月合計額 200,000"], ["消費税 20,000"],
    ]
    result, _ = parse_invoice(
        lines, "仕入請求書", client_name="株式会社Kライフ", force_review=True,
    )
    e = result.entries[0]
    assert e.amount == 220000
    assert e.date == date(2025, 8, 31)
    assert e.credit_sub == "有限会社サトウ電気"
    assert e.needs_review


def test_invoice_fallback_billed_amount():
    # 当月合計が無い請求書は「請求金額」で代用し、要確認を立てる
    rows = [
        ["令和7年10月31日"], ["1社 御中"], ["株式会社Kライフ"],
        ["ご請求金額", "50000"],
    ]
    result, _ = parse_invoice(rows, "売上請求書", client_name="株式会社Kライフ")
    e = result.entries[0]
    assert e.amount == 50000 and e.needs_review
    assert any("請求金額" in w for w in result.warnings)


# --- 勘定科目一覧表の解析 ---


def test_parse_account_rows():
    rows = [
        ["勘", "定", "科", "目", "一", "覧", "表", "1", "頁"],
        ["勘定科目", "サーチ", "キー数字", "貸借", "区分", "税区分"],
        ["[資産]"],
        ["[現金･預金]", "*100"],
        ["現", "金", "1101", "借方", "対象外", "指定なし", "現金及び預金"],
        # 非表示科目（末尾○）は取り込まない
        ["小", "口", "現", "金", "借方", "対象外", "指定なし", "現金及び預金", "○"],
        # 集計行（税区分もサーチキーも無い）は取り込まない
        ["現", "金", "･", "預", "金", "合", "計", "借方"],
        ["工", "事", "未", "払", "金", "2103", "貸方", "対象外", "指定なし", "工事未払金"],
        ["外", "注", "費", "750", "借方", "課対仕入", "標準自動", "内税", "指定なし", "適格", "外注費"],
        # 複合（諸口）は税区分が無いがサーチキーで拾う
        ["複", "合", "3999", "借方"],
        ["株式会社Ｋライフ"],
    ]
    records = _parse_account_rows(rows)
    names = {r["name"]: r for r in records}
    assert set(names) == {"現金", "工事未払金", "外注費", "複合"}
    assert names["現金"] == {"name": "現金", "search_key": "1101", "side": "借方", "tax_class": "対象外"}
    assert names["工事未払金"]["side"] == "貸方"
    assert names["外注費"]["tax_class"] == "課対仕入"


def test_parse_real_account_pdf():
    if not os.path.exists(REAL_ACCOUNT_PDF):
        print("  (実PDFなし・スキップ)")
        return
    records = parse_yayoi_account_pdf(open(REAL_ACCOUNT_PDF, "rb").read())
    assert len(records) > 150
    names = {r["name"]: r for r in records}
    assert names["完成工事未収入金"]["search_key"] == "1141"
    assert names["工事未払金"]["side"] == "貸方"
    assert names["当期完成工事高"]["tax_class"] == "課税売上"
    assert "外注費" in names and "複合" in names
    # 集計行・非表示科目は入らない
    assert "現金･預金合計" not in names
    assert "小口現金" not in names


def test_parse_real_samples():
    if not (os.path.exists(REAL_INVOICE_XLSX) and os.path.exists(REAL_LEDGER_XLSX)):
        print("  (実xlsxなし・スキップ)")
        return
    rule = default_doctype_rule("売上", ["完成工事未収入金", "当期完成工事高"])
    # 請求書: 当月合計548,875+消費税54,885=603,760
    rows = tabular_rows_from_bytes("invoice.xlsx", open(REAL_INVOICE_XLSX, "rb").read())
    result, _ = parse_invoice(rows, "売上請求書", client_name="株式会社Kライフ", rule=rule)
    e = result.entries[0]
    assert (e.amount, e.date, e.debit_sub) == (603760, date(2025, 10, 31), "1社")
    # 売掛表: 請求書と同じ603,760が3行目に入っている
    rows = tabular_rows_from_bytes("ledger.xlsx", open(REAL_LEDGER_XLSX, "rb").read())
    result, _ = parse_partner_ledger(
        rows, "売上", rule=rule, partner_map={3: "1社"},
    )
    assert result.entries[0].amount == 603760
    assert result.entries[0].debit_sub == "1社"
    assert all(e.date == date(2025, 10, 31) for e in result.entries)


# --- CSV読み込み ---


def test_tabular_rows_from_csv():
    data = "売掛表,2025年,10月\n出口住設,100000\n".encode("cp932")
    rows = tabular_rows_from_bytes("uriage.csv", data)
    assert rows[1] == ["出口住設", "100000"]
    result, _ = parse_partner_ledger(rows, "売上")
    assert result.entries[0].amount == 100000


# --- 保存（勘定科目マスタ・紐付け・行番号対応・補助科目の追記） ---


def test_storage_masters():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        # 勘定科目マスタ
        assert storage.list_account_master("A建設", db_path=db) == []
        saved = storage.replace_account_master(
            "A建設",
            [{"name": "完成工事未収入金", "search_key": "1141", "side": "借方", "tax_class": "対象外"},
             {"name": "完成工事未収入金", "search_key": "1141", "side": "借方", "tax_class": "対象外"},
             {"name": "", "search_key": "", "side": "", "tax_class": ""}],
            db_path=db,
        )
        assert saved == 1  # 重複・空行は除外
        master = storage.list_account_master("A建設", db_path=db)
        assert master[0]["name"] == "完成工事未収入金"
        assert storage.list_account_master("B工務店", db_path=db) == []

        # 書類タイプ→科目の紐付け
        assert storage.get_doctype_rule("A建設", "売上", db_path=db) is None
        storage.set_doctype_rule("A建設", "売上", "完成工事未収入金", "当期完成工事高",
                                 "debit", db_path=db)
        rule = storage.get_doctype_rule("A建設", "売上", db_path=db)
        assert rule == {"debit_account": "完成工事未収入金",
                        "credit_account": "当期完成工事高", "sub_side": "debit"}
        # 上書き
        storage.set_doctype_rule("A建設", "売上", "売掛金", "売上高", "debit", db_path=db)
        assert storage.get_doctype_rule("A建設", "売上", db_path=db)["debit_account"] == "売掛金"

        # 行番号→取引先の対応
        saved = storage.replace_partner_rows(
            "A建設", "sales",
            [{"row_no": 3, "partner_name": "出口住設"},
             {"row_no": 3, "partner_name": "重複"},
             {"row_no": "x", "partner_name": "不正"}],
            db_path=db,
        )
        assert saved == 1
        rows = storage.list_partner_rows("A建設", "sales", db_path=db)
        assert rows[0]["row_no"] == 3 and rows[0]["partner_name"] == "出口住設"
        assert storage.list_partner_rows("A建設", "purchase", db_path=db) == []

        # 補助科目の追記（既存マスタを消さない・重複は False）
        storage.replace_subaccounts(
            "A建設", [{"account": "普通預金", "sub_name": "川崎信用金庫", "search_key": ""}],
            db_path=db,
        )
        assert storage.add_subaccount("A建設", "完成工事未収入金", "1社", db_path=db)
        assert not storage.add_subaccount("A建設", "完成工事未収入金", "1社", db_path=db)
        subs = storage.list_subaccounts("A建設", db_path=db)
        assert {s["sub_name"] for s in subs} == {"川崎信用金庫", "1社"}


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
