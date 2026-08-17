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
from doc_parser import parse_document  # noqa: E402

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


def test_receipt_ignores_tendered_and_change():
    """コンビニレシート: お預り・お釣りを合計と誤認しない。"""
    lines = [
        "コンビニ〇〇店",
        "2026年7月1日",
        "おにぎり ¥150",
        "お茶 ¥140",
        "合計",
        "¥1,290",
        "お預り",
        "¥10,000",
        "お釣り",
        "¥8,710",
    ]
    result = parse_document(lines, "領収書")
    assert result.entries[0].amount == 1290


def test_receipt_fallback_excludes_tendered():
    """「合計」が読めなかった場合の最大値フォールバックでも預り金は除外。"""
    lines = ["2026/07/01", "¥1,290", "お預り ¥10,000"]
    result = parse_document(lines, "領収書")
    assert result.entries[0].amount == 1290


def test_receipt_store_name_as_description():
    """摘要にはファイル名ではなくOCRで読んだ店舗名が入る。"""
    lines = [
        "領収書",
        "セブン-イレブン 江東亀戸店",
        "2026年7月1日",
        "合計 ¥1,290",
    ]
    result = parse_document(lines, "領収書", source_name="IMG_1234.jpg")
    assert result.entries[0].description == "セブン-イレブン 江東亀戸店"

    # 店名らしき行が見つからなければファイル名で代用
    result2 = parse_document(["2026/07/01", "合計 ¥500"], "領収書", source_name="IMG_5678.jpg")
    assert result2.entries[0].description == "IMG_5678.jpg"


def test_source_file_management():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"

        def entry(desc):
            return JournalEntry(date=date(2026, 6, 1), debit_account="雑費",
                                credit_account="現金", amount=100, description=desc)

        storage.add_entries("A建設", [entry("a1"), entry("a2")], source_file="通帳6月.pdf", db_path=db)
        storage.add_entries("A建設", [entry("b1")], source_file="レシート.jpg", db_path=db)
        storage.add_entries("B工務店", [entry("c1")], source_file="通帳6月.pdf", db_path=db)

        files = dict(storage.list_source_files("A建設", db_path=db))
        assert files == {"通帳6月.pdf": 2, "レシート.jpg": 1}

        # ファイル単位の削除は対象クライアント・対象ファイルだけに効く
        deleted = storage.delete_entries_by_source("A建設", "通帳6月.pdf", db_path=db)
        assert deleted == 2
        assert dict(storage.list_source_files("A建設", db_path=db)) == {"レシート.jpg": 1}
        assert len(storage.load_entries("B工務店", db_path=db)) == 1


def test_account_rules_storage():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        assert storage.list_account_rules(db_path=db) == []
        assert storage.add_account_rule("タイムズ", "旅費交通費", db_path=db) is True
        assert storage.add_account_rule("", "旅費交通費", db_path=db) is False

        rules = storage.list_account_rules(db_path=db)
        assert len(rules) == 1
        assert rules[0]["keyword"] == "タイムズ"
        assert rules[0]["side"] == "expense"

        # 同じキーワードは上書き（重複しない）
        assert storage.add_account_rule("タイムズ", "車輌費", db_path=db) is True
        rules = storage.list_account_rules(db_path=db)
        assert len(rules) == 1
        assert rules[0]["account"] == "車輌費"

        storage.delete_account_rule(rules[0]["id"], db_path=db)
        assert storage.list_account_rules(db_path=db) == []


def test_custom_rules_priority():
    from accounts import estimate_expense_account, estimate_income_account

    # 学習ルールは組み込みルールより優先される
    account, review = estimate_expense_account("ETC利用料", [("ETC", "車輌費")])
    assert (account, review) == ("車輌費", False)
    # 組み込みで推定できない摘要も学習ルールで確定できる
    account, review = estimate_expense_account("謎の店", [("謎の店", "会議費")])
    assert (account, review) == ("会議費", False)
    account, review = estimate_income_account("フリコミ タマケンセツ", [("タマケンセツ", "売上高")])
    assert (account, review) == ("売上高", False)


def test_receipt_reduced_tax_full():
    """全額が軽減8%対象のレシート（木村ピーナッツの実物を再現）。"""
    lines = [
        "[領収書]", "有限会社木村ピーナッツ", "千葉県館山市下真倉236-3",
        "TEL:0470-22-3488", "登録番号:T2040002099323",
        "2026/05/05 16:30:22", "レジ:0001 担当:0001",
        "*ピーナッツソフト[コーン]", "¥470 3点 ¥1,410",
        "小計 3点 ¥1,410", "合計 ¥1,410",
        "(内消費税等 ¥104)", "(8%軽減対象 ¥1,410)",
        "現金 ¥2,000", "お預り ¥2,000", "お釣り ¥590",
        "*印は軽減税率(8%)適用商品",
    ]
    result = parse_document(lines, "領収書")
    e = result.entries[0]
    assert e.amount == 1410
    assert e.debit_tax == "課対仕入込軽減8%"  # 全額軽減
    assert e.credit_account == "現金"  # クレジット表記なし
    assert e.description == "有限会社木村ピーナッツ"
    assert e.date == date(2026, 5, 5)


def test_receipt_handwritten_credit_and_bottom_store():
    """手書き領収証（緑壽庵清水の実物を再現）: ピリオド区切り金額・
    クレジット払い・下部の発行者名・但し書き。"""
    lines = [
        "領収証", "上", "様", "¥2.916-", "但 御菓子代として", "クレジット",
        "入金日 2026年5月5日 上記の金額正に領収いたしました",
        "8%対象 ¥2.916-", "内訳 10%対象 ¥0-", "内消費税 ¥216-",
        "現金", "小切手", "手形", "銀座", "株式会社 緑壽庵清水",
        "〒104-0061 東京都中央区銀座6丁目2番地1号",
        "TEL (03)5537-9111 FAX (03)5537-9112",
    ]
    result = parse_document(lines, "領収書")
    e = result.entries[0]
    assert e.amount == 2916  # 「¥2.916-」を桁区切りとして解釈
    assert e.credit_account == "未払金"  # クレジット払い
    assert e.debit_tax == "課対仕入込軽減8%"  # 10%対象が0円 → 全額軽減
    assert "緑壽庵清水" in e.description
    assert "御菓子代" in e.description  # 但し書き


def test_receipt_mixed_tax_split():
    """8%と10%が混在する領収証は2行に分割する（会計事務所の指示）。"""
    lines = [
        "領収証", "上 様", "6,885-", "但 御菓子代として", "クレジット",
        "入金日 2026年5月6日",
        "8%対象 ¥5,940-", "内訳 10%対象 ¥945-",
        "内消費税 ¥440-", "内消費税 ¥85-",
        "株式会社 緑壽庵清水",
    ]
    result = parse_document(lines, "領収書")
    assert len(result.entries) == 2

    reduced, standard = result.entries
    assert reduced.amount == 5940
    assert reduced.debit_tax == "課対仕入込軽減8%"
    assert "軽減8%分" in reduced.description
    assert standard.amount == 945
    assert standard.debit_tax == "課対仕入込10%"
    assert "10%分" in standard.description
    # 合計は元の領収証と一致し、どちらもクレジット払い→未払金
    assert reduced.amount + standard.amount == 6885
    assert reduced.credit_account == standard.credit_account == "未払金"
    assert any("分割" in w for w in result.warnings)


def test_receipt_reduced_tax_with_jumbled_ocr_order():
    """OCRの行順が乱れて8%対象の金額が拾えなくても、10%の記載が
    どこにもなければ全額軽減8%とみなす（もあ小麦館・実OCRの症状を再現）。"""
    lines = [
        "もあ 小麦館",
        "登録番号 T5020001076479",
        "2026年 5月19日 (火) 15:04",
        "塩あんバター",
        "¥237",
        "合計",
        "¥197",
        "(税率 8%対象額",
        "*は軽減税率対象です",  # 金額が近くにない＝内訳が拾えない状態
        "もあ 小麦館",
        "川崎市宮前区有馬5-1-1",
    ]
    result = parse_document(lines, "領収書")
    e = result.entries[0]
    assert e.amount == 197
    assert e.debit_tax == "課対仕入込軽減8%"  # 10%の記載なし → 全額軽減
    assert "もあ 小麦館" in e.description  # 2回出現する行を店名とみなす


def test_receipt_shinkansen_spaced_amount():
    """新幹線領収書（実OCR）: 「¥12. 050円」のスペース入り金額と
    「2026年 6月13日」のスペース入り日付を読める。"""
    lines = [
        "領収書-No", "763",
        "東海旅客鉄道株式会社", "2026年 6月13日",
        "但し、乗車券類(クレジット扱い)として",
        "「消費税等込み ·10%」", "¥12. 050円",
        "名古屋駅", "登録番号:T3180001031569",
    ]
    result = parse_document(lines, "領収書")
    e = result.entries[0]
    assert e.amount == 12050
    assert e.date == date(2026, 6, 13)
    assert e.debit_account == "旅費交通費"  # 鉄道 → 旅費交通費
    assert e.credit_account == "未払金"  # クレジット扱い
    assert e.debit_tax == "課対仕入込10%"  # 10%明記 → 軽減にしない


def test_receipt_ic_card_balance_excluded():
    """交通系IC領収書（実OCR）: 「交通系残高 ¥8085円」を合計と誤認しない。"""
    lines = [
        "交通系 ICカード売上票",
        "えびす自動車株式会社",
        "御利用 日.2026/06/20",
        "合計金額 ¥3200円",
        "交通系支払額 ¥3200円",
        "交通系残高 ¥8085円",
    ]
    result = parse_document(lines, "領収書")
    e = result.entries[0]
    assert e.amount == 3200  # 残高8085ではなく合計
    assert e.debit_account == "旅費交通費"  # タクシー


def test_receipt_labels_and_amounts_in_separate_blocks():
    """縦書きレシートの実OCR: 項目名と金額が離れたブロックに読み出される。

    同じ行での「残高」除外が効かないため、最も多く現れる金額
    （＝合計。この受領証では¥3200が4回、残高¥8085は1回）を採る。
    """
    lines = [
        "車番", "取引", "取引通番", "カード番号", "加盟店名·", "合計金額",
        "電話番号.", "通行料他", "基本運賃", "運賃料金計", "交通系残高",
        "03-3743-0401", "伝票番号.012255", "交通系支払額",
        "御利用 日.2026/06/20", "えびす自動車(株)",
        "¥3200円", "No.0789", "¥3200円", "¥3200円", "¥3200円",
        "¥8085円", "000", "¥0円",
    ]
    result = parse_document(lines, "領収書")
    e = result.entries[0]
    assert e.amount == 3200, f"残高8085を拾ってしまった: {e.amount}"
    assert e.date == date(2026, 6, 20)
    # 先頭の断片「車番」ではなく会社名を摘要に使う
    assert e.description == "えびす自動車(株)"


def test_store_name_prefers_company_over_fragment():
    """縦書きレシートでOCRが断片を先頭に読み出しても会社名を摘要にする。"""
    from doc_parser import _find_store_name

    # 会社表記を最優先
    assert _find_store_name(
        ["APL", "AID", "NPC24H新宿3丁目パーキング", "日本パーキング株式会社"]
    ) == "日本パーキング株式会社"
    # 会社表記がなければ店名らしい語を含む行
    assert _find_store_name(["カー", "Visa", "エコロパーク 恵比寿第1"]) == "エコロパーク 恵比寿第1"
    # どちらもなければ従来どおり冒頭の行
    assert _find_store_name(["よろずや", "2026/07/01", "合計 ¥500"]) == "よろずや"


def test_receipt_fullwidth_mixed_tax_split():
    """全角数字のレシート（ファミマの実物を再現）: 「１０％対象」「８％対象」
    が全角で印字されていても8%/10%を分割できる。"""
    lines = [
        "FamilyMart",
        "東古市場店",
        "神奈川県川崎市幸区東古市場",
        "登録番号:T8020002096077",
        "２０２６年 ７月２３日（木）１６：４３",
        "領 収 証",
        "スーパードライ６缶３５", "￥１，３６８",
        "オールフリー５００", "￥２４３軽",
        "レジ袋２０号バイオマス", "￥５",
        "スプバレ豊潤ラガー３５", "￥２６９",
        "合 計", "￥１，８８５",
        "（１０％対象", "￥１，６４２）",
        "（内消費税等", "￥１４９）",
        "（ ８％対象", "￥２４３）",
        "（内消費税等", "￥１８）",
        "ＱＵＩＣＰａｙ支払", "￥１，８８５",
        "「軽」は軽減税率対象商品です。",
    ]
    result = parse_document(lines, "領収書")
    assert len(result.entries) == 2, [w for w in result.warnings]

    by_amount = {e.amount: e for e in result.entries}
    assert by_amount[1642].debit_tax == "課対仕入込10%"
    assert by_amount[243].debit_tax == "課対仕入込軽減8%"
    assert sum(e.amount for e in result.entries) == 1885
    # QUICPay（後払い型）→ 未払金、日付は全角でも読める
    assert all(e.credit_account == "未払金" for e in result.entries)
    assert all(e.date == date(2026, 7, 23) for e in result.entries)


def test_image_compression():
    import io as _io

    import numpy as np
    from PIL import Image

    from ocr import compress_image_if_needed

    # 圧縮が効きにくいノイズ画像で 3.5MB 超のJPEGを作る
    noise = np.random.randint(0, 255, (3500, 2600, 3), dtype=np.uint8)
    buf = _io.BytesIO()
    Image.fromarray(noise).save(buf, "JPEG", quality=95)
    big = buf.getvalue()
    assert len(big) > int(3.5 * 1024 * 1024)

    out, note = compress_image_if_needed(big, "photo.jpg")
    assert len(out) <= int(3.5 * 1024 * 1024)
    assert note is not None
    # 上限内の画像・PDFはそのまま
    small, note2 = compress_image_if_needed(b"x" * 1000, "small.jpg")
    assert small == b"x" * 1000 and note2 is None
    pdf, note3 = compress_image_if_needed(big, "doc.pdf")
    assert pdf == big and note3 is None


def test_receipt_clusters_dedup_split_receipt():
    """1枚のレシートが2つに割れて同額の仕訳が重複するのを防ぐ。

    実機では、えびす自動車のタクシー領収書が「売上票」と「領収書」の
    2つのかたまりに割れ、3,200円が二重に計上されて5件になっていた。
    また端の断片（金額なし）が余分なかたまりになる。
    """
    from doc_parser import parse_receipt_clusters

    clusters = [
        ["領収書", "エコロパーク 恵比寿第1", "2026/06/08 17:49", "合計 1,600円"],
        ["領収書", "日本パーキング株式会社", "2026年06月24日", "請求金額 3,960円"],
        # 同じタクシー領収書の売上票側（日付あり）
        ["交通系 ICカード売上票", "えびす自動車(株)", "御利用 日.2026/06/20", "合計金額 ¥3200円"],
        # 同じ領収書の控え側（日付は '26年06月20日 表記）
        ["収", "えびす自動車株式会社", "日付 ’26年06月20日", "¥3200円"],
        ["東海旅客鉄道株式会社", "2026年 6月13日", "但し、乗車券類(クレジット扱い)として", "¥12. 050円"],
        # 金額のない断片（分割の副産物）
        ["駅-No 51301160", "領収書-No", "窓口-No", "763", "65"],
    ]
    result = parse_receipt_clusters(clusters, source_name="receipts.jpg")

    amounts = sorted(e.amount for e in result.entries)
    assert amounts == [1600, 3200, 3960, 12050], amounts
    assert all(e.needs_review for e in result.entries)
    # 重複をまとめた旨は利用者に伝える
    assert any("同じ金額" in w and "3,200円" in w for w in result.warnings)
    # 金額のない断片についての警告は出さない
    assert not any("金額を検出できません" in w for w in result.warnings)

    # '26年06月20日 表記も日付として読める（重複解消時に残る側の日付）
    taxi = next(e for e in result.entries if e.amount == 3200)
    assert taxi.date == date(2026, 6, 20)


def test_multi_receipt_clustering_adjacent():
    """レシート同士が近接して置かれて1かたまりに融合しても、登録番号
    （T+13桁）が複数見つかれば、しきい値を狭めて再分割する。

    実機では縦置き4枚の写真が融合し、摘要に隣のレシートの会社名が
    入っていた。
    """
    from ocr import OcrLine, split_text_clusters

    def L(text, x, y):
        return OcrLine(text=text, x=x, y=y, height=10.0, page=1, width=90.0)

    # 2枚のレシートが横に並び、隙間は 25px（しきい値 3.0×10=30 より狭い
    # ため初回は融合するが、再分割の 0.55倍しきい値で分かれる）
    receipt_a = [
        L("領収書", 0, 0), L("セブン-イレブン川崎古川町店", 0, 12),
        L("登録番号T2020002085580", 0, 24), L("2026/07/06", 0, 36),
        L("合計 ¥321", 0, 48),
    ]
    receipt_b = [
        L("領収証", 115, 0), L("パークエステート駐車場", 115, 12),
        L("登録番号T3120001177863", 115, 24), L("2026/07/08", 115, 36),
        L("料金計 2,500円", 115, 48),
    ]
    clusters = split_text_clusters(receipt_a + receipt_b)
    assert len(clusters) == 2, f"2枚に分割されるはずが {len(clusters)} 件"

    texts_per_cluster = ["\n".join(l.text for l in c) for c in clusters]
    assert any("セブン-イレブン" in t and "パークエステート" not in t for t in texts_per_cluster)
    assert any("パークエステート" in t and "セブン-イレブン" not in t for t in texts_per_cluster)


def test_multi_receipt_clustering_rotated():
    """横向きに撮影されたレシート（テキストが90度回転）でも分割できる。

    実機では4枚のレシートを横向きに並べて撮影した写真が1かたまりに
    融合し、カード明細として誤解析されていた。回転すると外接矩形の
    高さが「行の長さ」になるため、短い辺を文字サイズとして扱う。
    """
    from ocr import OcrLine, split_text_clusters

    def V(text, band_top, x):
        """回転したテキスト行: 幅=文字サイズ、高さ=行の長さ。"""
        length = 300.0
        return OcrLine(
            text=text, x=x, y=band_top + length / 2,
            height=length, page=1, width=12.0,
        )

    # 4枚のレシートが縦に並び、各レシートの中でテキストは横方向に並ぶ
    lines = []
    for k, (band, texts) in enumerate(
        [
            (0, ["領収書", "エコロパーク恵比寿第1", "2026/06/08 17:49", "合計 1,600円"]),
            (400, ["領収書", "日本パーキング株式会社", "2026年06月24日", "請求金額 3,960円"]),
            (800, ["交通系ICカード売上票", "えびす自動車株式会社", "御利用 日.2026/06/20", "合計金額 ¥3200円"]),
            (1200, ["領収書", "東海旅客鉄道株式会社", "2026年 6月13日", "¥12. 050円"]),
        ]
    ):
        for i, t in enumerate(texts):
            lines.append(V(t, band, 20.0 + i * 25))

    clusters = split_text_clusters(lines)
    assert len(clusters) == 4, f"4枚に分割されるはずが {len(clusters)} 件"

    amounts = []
    for cluster in clusters:
        r = parse_document([ln.text for ln in cluster], "領収書")
        amounts.append(r.entries[0].amount)
    assert sorted(amounts) == [1600, 3200, 3960, 12050]


def test_detect_does_not_override_receipt_on_weak_signal():
    """駐車場の領収書に「クレジットカードご利用明細」等が印字されていても、
    わずかな一致でカード明細と誤判定しない（実機の誤解析を再現）。"""
    from doc_parser import detect_document_type

    parking = [
        "領収書", "NPC24H新宿3丁目パーキング", "日本パーキング株式会社",
        "登録番号 T7010001068319", "消費税率 内税10%",
        "◆クレジットカードご利用明細◆", "カード番号 IC ************ 0590",
        "請求金額 3,960円", "出庫時間 06月24日 14:09",
    ]
    assert detect_document_type(parking, selected="領収書") != "カード明細"

    # 本物のカード明細は「領収書」を選んでいても正しく上書き判定できる
    card = [
        "楽天カード ご利用明細", "カード名義: ○○建設株式会社",
        "カード番号: **** 1234", "お支払い月: 2026年7月",
        "利用日", "利用店名・商品名", "支払方法", "利用金額",
    ]
    assert detect_document_type(card, selected="領収書") == "カード明細"


def test_multi_receipt_clustering():
    from ocr import OcrLine, split_text_clusters

    def L(text, y, x=10.0):
        return OcrLine(text=text, x=x, y=y, height=10.0, page=1, width=200.0)

    receipt_a = [
        L("領収書", 0), L("A商店", 20), L("2026/05/01", 40), L("合計 ¥1,000", 60),
    ]
    receipt_b = [
        L("領収書", 400), L("B食堂", 420), L("2026/05/02", 440), L("合計 ¥2,000", 460),
    ]
    clusters = split_text_clusters(receipt_a + receipt_b)
    assert len(clusters) == 2
    r1 = parse_document([ln.text for ln in clusters[0]], "領収書")
    r2 = parse_document([ln.text for ln in clusters[1]], "領収書")
    assert r1.entries[0].amount == 1000
    assert r2.entries[0].amount == 2000

    # 1枚のレシートだけなら分割されない
    assert len(split_text_clusters(receipt_a)) == 1


def test_detect_document_type():
    from doc_parser import detect_document_type

    card_lines = [
        "楽天カード ご利用明細", "カード名義: ○○建設株式会社",
        "カード番号: **** 1234", "お支払い月: 2026年7月",
        "利用日", "利用店名・商品名", "支払方法", "利用金額",
    ]
    assert detect_document_type(card_lines) == "カード明細"

    bank_lines = ["普通預金", "お預り金額", "差引残高", "繰越"]
    assert detect_document_type(bank_lines) == "通帳"

    receipt_lines = ["領収書", "お買上ありがとうございます", "お釣り"]
    assert detect_document_type(receipt_lines) == "領収書"

    # 手がかりが足りなければ None（選択された書類タイプを尊重）
    assert detect_document_type(["こんにちは", "12345"]) is None


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
