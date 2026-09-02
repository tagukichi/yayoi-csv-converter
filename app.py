import os
from datetime import datetime

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import storage
from accounts import BS_ACCOUNTS, EXPENSE_RULES, INCOME_RULES, yayoi_tax
from models import JournalEntry, ParseResult
from ocr import (
    AzureOCRError,
    compress_image_if_needed,
    credentials_available,
    group_rows,
    is_image_filename,
    run_ocr_lines,
    split_text_clusters,
)
from doc_parser import (
    apply_description_rules,
    detect_document_type,
    parse_document,
    parse_payroll,
    parse_receipt_clusters,
    parse_table_document,
)
from sales_parser import (
    INVOICE_TYPES,
    PARTNER_LEDGER_TYPES,
    default_doctype_rule,
    parse_invoice,
    parse_partner_ledger,
    tabular_rows_from_bytes,
)
from yayoi_exporter import to_yayoi_csv

# 表形式（xlsx/CSV）の自動解析に対応した新しい書類タイプ
TABULAR_DOC_TYPES = PARTNER_LEDGER_TYPES + INVOICE_TYPES

load_dotenv()

# Streamlit Community Cloud 等では認証情報を st.secrets で渡す。ocr.py /
# storage.py は os.getenv で読むため、secrets を環境変数へ橋渡しする
# （ローカルの .env が優先されるよう setdefault を使う）。
try:
    for _k, _v in dict(st.secrets).items():
        if isinstance(_v, str):
            os.environ.setdefault(_k, _v)
except Exception:
    pass  # secrets 未設定（ローカル開発）なら何もしない

st.set_page_config(page_title="PDF → 弥生CSV 変換ツール", page_icon="📄", layout="wide")


def _check_password() -> bool:
    """APP_PASSWORD が設定されている場合のみパスワード認証を要求する。

    未設定（ローカル開発）なら常に通す。公開デプロイ時に secrets へ
    APP_PASSWORD を入れると、URL を知る人全員が使えてしまうのを防げる。
    """
    expected = os.getenv("APP_PASSWORD")
    if not expected:
        return True
    if st.session_state.get("authed"):
        return True
    st.title("PDF → 弥生CSV 変換ツール")
    pw = st.text_input("パスワードを入力してください", type="password")
    if pw:
        if pw == expected:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("パスワードが違います。")
    return False


if not _check_password():
    st.stop()

st.title("PDF → 弥生CSV 変換ツール")

# Streamlit 組み込みのファイルアップローダは表示文言が英語で、翻訳する仕組みが
# 提供されていないため、CSS の ::after で日本語ラベルを重ねて置き換える。
st.markdown(
    """
    <style>
    /* 案内文（"Drag and drop files here" と "Limit 200MB per file • ..."）は
       アイコン span の隣の div の中に span 2つで入っている。両方隠して
       ::before / ::after で日本語を出す。 */
    [data-testid="stFileUploaderDropzoneInstructions"] > div > span {
        display: none;
    }
    [data-testid="stFileUploaderDropzoneInstructions"] > div::before {
        content: "ここにファイルをドラッグ＆ドロップ";
        font-weight: 600;
        display: block;
    }
    [data-testid="stFileUploaderDropzoneInstructions"] > div::after {
        content: "1ファイル200MBまで ・ PDF / PNG / JPG / XLSX / CSV に対応";
        font-size: 0.8rem;
        opacity: 0.6;
        display: block;
        margin-top: 0.25rem;
    }
    /* 「Browse files」ボタン。文字だけ隠して日本語を重ねる
       （visibility なら配色・枠線はテーマのまま保てる） */
    [data-testid="stFileUploaderDropzone"] button {
        visibility: hidden;
        position: relative;
    }
    [data-testid="stFileUploaderDropzone"] button::after {
        content: "ファイルを選択";
        visibility: visible;
        position: absolute;
        inset: 0;
        display: flex;
        align-items: center;
        justify-content: center;
        white-space: nowrap;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

with st.expander("📖 使い方", expanded=False):
    st.markdown(
        """
        1. **書類タイプを選択** します（領収書／レシート・通帳・カード明細・給与台帳・
           売上（売掛表）・売上請求書・仕入請求書・買掛表 など）
        2. **ファイルをアップロード** します（PDF・PNG・JPG・XLSX・CSV。複数まとめて選択できます）
           - 買掛表は **ExcelやCSVのままアップロード** します（OCR不要でそのまま解析）。他の書類は写真・PDFでOKです
        3. **「変換を開始」** をクリックします（読み取りに数十秒かかることがあります）
        4. **「✏️ 仕訳の編集」** タブで内容を確認します
           - **「要確認」にチェックが付いた行**は、勘定科目を自動で判断できなかった行です。
             摘要を見て科目を修正し、確認できたらチェックを外してください
           - 通帳は残高の計算が合わない行にもチェックが付きます（読み取り誤りの可能性）
           - 修正したら **「💾 変更を保存」** をクリックします
        5. **「🖨 出力プレビュー」** タブで件数・合計金額を確認し、
           **「⬇️ 弥生CSVをダウンロード」** をクリックします
           - **期間（月）で絞って**出力できます
           - このタブはブラウザの印刷機能（Mac: ⌘P / Windows: Ctrl+P）でそのまま印刷できます

        ダウンロードしたCSVを弥生会計の「仕訳データのインポート」から取り込んでください。
        仕訳はクライアント企業ごとに蓄積されるので、書類を数回に分けてアップロードし、
        最後にまとめてCSVを出力することもできます。
        **同じ名前のファイルを2回アップロードしても重複しないよう自動でスキップ**されます。
        間違えて取り込んだ場合は、編集タブの「🗂 ファイル単位で取り込みを取り消す」から戻せます。
        """
    )

# マネーフォワード / freee は対応実装後に選択肢へ戻す（コードは温存して非表示）
ACCOUNTING_SOFTWARE_OPTIONS = ["弥生"]  # + ["マネーフォワード", "freee"]

from submaster import (  # noqa: E402
    match_subaccount,
    parse_yayoi_account_pdf,
    parse_yayoi_subaccount_pdf,
)

# --- サイドバー ---
with st.sidebar:
    st.header("設定")

    clients = storage.list_clients()
    client = st.selectbox("クライアント企業", clients) if clients else None

    with st.expander("➕ 企業の追加・削除"):
        new_client = st.text_input("追加する企業名", key="new_client_name")
        if st.button("追加", key="add_client"):
            if storage.add_client(new_client):
                st.rerun()
            else:
                st.error("空欄か、すでに登録済みの企業名です。")
        if client:
            st.divider()
            confirm_delete = st.checkbox(
                f"「{client}」を削除する（蓄積した仕訳も削除されます）",
                key="confirm_delete_client",
            )
            if st.button("削除", key="delete_client", disabled=not confirm_delete):
                storage.delete_client(client)
                st.rerun()

    accounting_software = st.radio(
        "出力する会計ソフト",
        ACCOUNTING_SOFTWARE_OPTIONS,
    )
    if accounting_software != "弥生":
        st.caption("⚠️ 現在は弥生のみ対応しています（他は対応予定）。")

    st.divider()
    st.caption(f"💾 データ保存先: {storage.backend_name()}")

if client is None:
    st.info("サイドバーの「企業の追加・削除」からクライアント企業を登録してください。")
    st.stop()

# --- メインエリア ---
if not credentials_available():
    st.error(
        "Azure の認証情報が見つかりません。"
        "`.env` に `AZURE_VISION_ENDPOINT` と `AZURE_VISION_KEY` を設定してください"
        "（`.env.example` 参照）。"
    )

# --- 事前登録: クライアント別のマスタ（補助科目／勘定科目／売掛・買掛の設定） ---
_master = storage.list_subaccounts(client)
_acct_master = storage.list_account_master(client)

if sub_flash := st.session_state.pop("sub_flash", None):
    st.success(sub_flash)

# 3つの折りたたみに分ける。各タブの中身は下の with ブロックで描画する
# （タブは作った折りたたみの中に表示される）
_sub_label = f"補助科目 {len(_master)} 件登録済み" if _master else "補助科目 未登録"
_acct_label = f"勘定科目 {len(_acct_master)} 件登録済み" if _acct_master else "勘定科目 未登録"

with st.expander(f"🗂 事前登録①：補助科目マスタ — {client}（{_sub_label}）"):
    st.markdown(
        "通帳の摘要（「フリコミ タマケンセツ」など）や売掛表・請求書の取引先から、"
        "勘定科目・補助科目を**自動で振り分ける**ための登録です。"
    )
    tab_pdf_import, tab_master_list = st.tabs(["📄 PDFから一括登録", "📝 登録内容の確認・編集"])

with st.expander(f"📒 事前登録②：勘定科目マスタ — {client}（{_acct_label}）"):
    st.markdown(
        "クライアント企業の弥生の勘定科目一覧を登録しておくと、仕訳表の科目を"
        "**プルダウンで選べる**ようになり、売上・買掛表の既定の科目もここから決まります。"
    )
    tab_acct_pdf, tab_acct_list = st.tabs(["📄 PDFから一括登録", "📝 登録内容の確認・編集"])

with st.expander("⚙️ 売掛・買掛の設定：書類タイプの紐付け／行番号と取引先の対応"):
    st.markdown(
        "売上（売掛表）・請求書・買掛表を仕訳にするときの設定です。"
        "上の2つのマスタとは独立して設定できます。"
    )
    tab_doctype, tab_rowmap = st.tabs(["🔗 書類タイプの紐付け", "🔢 売掛・買掛の行番号"])

with st.container():
    # --- PDFから一括登録 ---
    with tab_pdf_import:
        st.markdown(
            """
            **手順**
            1. 弥生会計で［集計表］→［補助科目一覧表］を **PDF出力** します
            2. そのPDFを下にアップロードします
            3. 読み取り結果を確認して「登録する」を押します
            """
        )
        sub_pdf = st.file_uploader(
            "補助科目一覧表のPDF", type=["pdf"], key="sub_pdf",
            label_visibility="collapsed",
        )
        if sub_pdf is not None:
            try:
                pdf_records = parse_yayoi_subaccount_pdf(sub_pdf.getvalue())
            except Exception as e:
                pdf_records = []
                st.error(f"PDFの読み取りに失敗しました: {e}")
            if not pdf_records:
                st.error(
                    "補助科目を読み取れませんでした。"
                    "弥生の「補助科目一覧表」のPDFかどうか確認してください。"
                )
            else:
                # 登録前に読み取り結果を見せて、確認してから登録してもらう
                st.success(f"✅ {len(pdf_records)} 件の補助科目を読み取りました。内容を確認してください:")
                summary = (
                    pd.DataFrame(pdf_records)
                    .groupby("account", sort=False)
                    .agg(件数=("sub_name", "count"), 補助科目の例=("sub_name", lambda s: "、".join(s.head(3)) + ("…" if len(s) > 3 else "")))
                    .rename_axis("勘定科目")
                )
                st.dataframe(summary, use_container_width=True)
                overwrite_note = (
                    f"※ 登録すると「{client}」の既存のマスタ（{len(_master)}件）は置き換えられます。"
                    if _master else ""
                )
                if overwrite_note:
                    st.caption(overwrite_note)
                if st.button(f"この {len(pdf_records)} 件を登録する", type="primary", key="sub_import"):
                    saved = storage.replace_subaccounts(client, pdf_records)
                    st.session_state["sub_flash"] = f"✅ {saved} 件の補助科目を登録しました。"
                    st.rerun()

    # --- 登録内容の確認・編集 ---
    with tab_master_list:
        if not _master:
            st.info(
                "まだ登録されていません。「📄 PDFから一括登録」タブで弥生のPDFを"
                "アップするか、下の表に直接入力して「保存」を押してください。"
            )
            master_view = pd.DataFrame(columns=["勘定科目", "補助科目", "サーチキー"])
            account_filter = "すべて"
        else:
            accounts_in_master = list(dict.fromkeys(r["account"] for r in _master))
            account_filter = st.selectbox(
                "表示する勘定科目で絞り込み",
                ["すべて"] + [f"{a}（{sum(1 for r in _master if r['account'] == a)}件）" for a in accounts_in_master],
                key="sub_filter",
            )
            selected_account = None
            if account_filter != "すべて":
                selected_account = account_filter.rsplit("（", 1)[0]
            shown = [r for r in _master if selected_account is None or r["account"] == selected_account]
            master_view = pd.DataFrame(shown)[["account", "sub_name", "search_key"]].rename(
                columns={"account": "勘定科目", "sub_name": "補助科目", "search_key": "サーチキー"}
            )

        edited_master = st.data_editor(
            master_view, num_rows="dynamic", use_container_width=True, key="sub_editor",
            column_config={
                "勘定科目": st.column_config.TextColumn(help="例: 普通預金、完成工事未収入金、工事未払金"),
                "補助科目": st.column_config.TextColumn(help="例: 川崎信用金庫、㈱ケイズ"),
                "サーチキー": st.column_config.TextColumn(help="弥生のサーチキー英字。通帳のカタカナ摘要との照合に使います"),
            },
        )
        if st.button("💾 変更を保存", key="sub_save"):
            edited_records = [
                {"account": r["勘定科目"], "sub_name": r["補助科目"], "search_key": r["サーチキー"]}
                for _, r in edited_master.iterrows()
            ]
            if _master and account_filter != "すべて":
                # 絞り込み表示中は、表示していない科目の登録内容を保持したまま
                # 表示分だけを差し替える
                selected_account = account_filter.rsplit("（", 1)[0]
                kept = [r for r in _master if r["account"] != selected_account]
                edited_records = kept + edited_records
            saved = storage.replace_subaccounts(client, edited_records)
            st.session_state["sub_flash"] = f"✅ {saved} 件を保存しました。"
            st.rerun()

    # --- 勘定科目: PDFから一括登録 ---
    with tab_acct_pdf:
        st.markdown(
            """
            **手順**
            1. 弥生会計で「勘定科目一覧表」を **PDF出力** します（科目設定の印刷）
            2. そのPDFを下にアップロードします
            3. 読み取り結果を確認して「登録する」を押します

            登録した科目は「書類タイプの紐付け」と仕訳の科目候補に使われます。
            """
        )
        acct_pdf = st.file_uploader(
            "勘定科目一覧表のPDF", type=["pdf"], key="acct_pdf",
            label_visibility="collapsed",
        )
        if acct_pdf is not None:
            try:
                acct_records = parse_yayoi_account_pdf(acct_pdf.getvalue())
            except Exception as e:
                acct_records = []
                st.error(f"PDFの読み取りに失敗しました: {e}")
            if not acct_records:
                st.error(
                    "勘定科目を読み取れませんでした。"
                    "弥生の「勘定科目一覧表」のPDFかどうか確認してください。"
                )
            else:
                st.success(f"✅ {len(acct_records)} 件の勘定科目を読み取りました。内容を確認してください:")
                st.dataframe(
                    pd.DataFrame(acct_records).rename(
                        columns={"name": "勘定科目", "search_key": "サーチキー",
                                 "side": "貸借", "tax_class": "税区分"}
                    ),
                    use_container_width=True,
                    height=300,
                )
                if _acct_master:
                    st.caption(f"※ 登録すると「{client}」の既存の勘定科目マスタ（{len(_acct_master)}件）は置き換えられます。")
                if st.button(f"この {len(acct_records)} 件を登録する", type="primary", key="acct_import"):
                    saved = storage.replace_account_master(client, acct_records)
                    st.session_state["sub_flash"] = f"✅ {saved} 件の勘定科目を登録しました。"
                    st.rerun()

    # --- 勘定科目: 確認・編集 ---
    with tab_acct_list:
        if not _acct_master:
            st.info(
                "まだ登録されていません。「📒 勘定科目：PDF登録」タブで弥生のPDFを"
                "アップするか、下の表に直接入力して「保存」を押してください。"
            )
            acct_view = pd.DataFrame(columns=["勘定科目", "サーチキー", "貸借", "税区分"])
        else:
            acct_view = pd.DataFrame(_acct_master)[["name", "search_key", "side", "tax_class"]].rename(
                columns={"name": "勘定科目", "search_key": "サーチキー", "side": "貸借", "tax_class": "税区分"}
            )
        edited_accts = st.data_editor(
            acct_view, num_rows="dynamic", use_container_width=True, key="acct_editor",
            column_config={
                "勘定科目": st.column_config.TextColumn(help="弥生の科目名と1文字違わず同じにしてください"),
                "サーチキー": st.column_config.TextColumn(help="弥生のサーチキー数字（例: 1101）"),
                "貸借": st.column_config.SelectboxColumn(options=["借方", "貸方"]),
                "税区分": st.column_config.TextColumn(help="例: 対象外、課対仕入、課税売上"),
            },
        )
        if st.button("💾 変更を保存", key="acct_save"):
            records = [
                {"name": r["勘定科目"], "search_key": r["サーチキー"],
                 "side": r["貸借"], "tax_class": r["税区分"]}
                for _, r in edited_accts.iterrows()
            ]
            saved = storage.replace_account_master(client, records)
            st.session_state["sub_flash"] = f"✅ {saved} 件の勘定科目を保存しました。"
            st.rerun()

    # --- 書類タイプ→科目の紐付け ---
    with tab_doctype:
        st.markdown(
            "売上（売掛表）・請求書・買掛表を仕訳にするときの**借方・貸方の勘定科目**を、"
            "書類タイプごとに設定します。取引先名は補助科目に入ります。"
            "未設定でも、勘定科目マスタから既定の科目（完成工事未収入金・工事未払金 等）を自動で選びます。"
        )
        _acct_names = [r["name"] for r in _acct_master]
        _doctype_inputs: dict[str, tuple[str, str, str]] = {}
        for dt in ("売上", "売上請求書", "仕入請求書", "買掛表"):
            current = storage.get_doctype_rule(client, dt) or default_doctype_rule(dt, _acct_names)
            col_dt, col_debit, col_credit = st.columns([1, 2, 2])
            col_dt.markdown(f"**{dt}**")
            _options = list(dict.fromkeys(
                [current["debit_account"], current["credit_account"]] + _acct_names
            ))
            debit = col_debit.selectbox(
                "借方科目", _options,
                index=_options.index(current["debit_account"]),
                key=f"doctype_debit_{dt}",
            )
            credit = col_credit.selectbox(
                "貸方科目", _options,
                index=_options.index(current["credit_account"]),
                key=f"doctype_credit_{dt}",
            )
            _doctype_inputs[dt] = (debit, credit, current["sub_side"])
        st.caption(
            "補助科目（取引先名）は、売上側は借方（売掛・未収系）、"
            "仕入側は貸方（買掛・未払系）に自動で入ります。"
        )
        if st.button("💾 紐付けを保存", key="doctype_save"):
            for dt, (debit, credit, sub_side) in _doctype_inputs.items():
                storage.set_doctype_rule(client, dt, debit, credit, sub_side)
            st.session_state["sub_flash"] = "✅ 書類タイプの紐付けを保存しました。"
            st.rerun()

    # --- 売掛表・買掛表の行番号→取引先 ---
    with tab_rowmap:
        st.markdown(
            "売掛表・買掛表が**「行番号と金額だけ」**の形式のとき、"
            "行番号を取引先名（補助科目）に変換するための対応表です。"
            "取引先名の列がある表では登録不要です。"
        )
        rowmap_choice = st.radio(
            "対象の表", ["売掛表（売上）", "買掛表"], horizontal=True, key="rowmap_side"
        )
        rowmap_side = "sales" if rowmap_choice.startswith("売掛") else "purchase"
        _rowmap = storage.list_partner_rows(client, rowmap_side)
        rowmap_view = (
            pd.DataFrame(_rowmap)[["row_no", "partner_name"]].rename(
                columns={"row_no": "行番号", "partner_name": "取引先名"}
            )
            if _rowmap
            else pd.DataFrame(columns=["行番号", "取引先名"])
        )
        edited_rowmap = st.data_editor(
            rowmap_view, num_rows="dynamic", use_container_width=True,
            key=f"rowmap_editor_{rowmap_side}",
            column_config={
                "行番号": st.column_config.NumberColumn(min_value=1, step=1),
                "取引先名": st.column_config.TextColumn(help="補助科目に入る取引先名。弥生の補助科目名と合わせてください"),
            },
        )
        if st.button("💾 対応表を保存", key="rowmap_save"):
            records = [
                {"row_no": r["行番号"], "partner_name": r["取引先名"]}
                for _, r in edited_rowmap.iterrows()
                if pd.notna(r["行番号"])
            ]
            saved = storage.replace_partner_rows(client, rowmap_side, records)
            st.session_state["sub_flash"] = f"✅ {rowmap_choice}の対応表 {saved} 件を保存しました。"
            st.rerun()

document_type = st.selectbox(
    "書類タイプ",
    [
        "領収書", "電子請求書", "通帳", "カード明細", "給与台帳",
        "売上", "売上請求書", "仕入請求書", "買掛表",
    ],
    help=(
        "売上＝取引先別の月次売上一覧（売掛表）。買掛表のみ Excel（xlsx/CSV）で"
        "アップロードします。他は写真・PDFで読み取ります。"
    ),
)

if document_type in PARTNER_LEDGER_TYPES:
    _how = (
        "Excel（xlsx・CSV）のままアップロードしてください（OCR不要でそのまま解析）。"
        if document_type == "買掛表"
        else "写真・PDFでアップロードしてください。"
    )
    st.caption(
        f"💡 取引先名（または行番号）と当月金額が並んだ表を読み取り、"
        f"月末日付・取引先ごとに1本の仕訳（税込10%）を作ります。{_how}"
        "行番号だけの表は、上の「⚙️ 売掛・買掛の設定 → 🔢 売掛・買掛の行番号」で"
        "取引先名を登録しておくと自動で名前が付きます。"
    )
elif document_type in INVOICE_TYPES:
    st.caption(
        "💡 請求書の写真・PDFから「当月合計額＋消費税」を1本の仕訳にします。"
        "取引先（宛名・発行者）は補助科目に入り、初めての取引先は"
        "自動で補助科目マスタに登録されます。"
    )

# 買掛表はExcelでもらう運用のため、アップロードもExcel（xlsx/CSV）に限定する。
# 他の書類タイプは従来どおり写真・PDF中心（xlsx/CSVも受け付ける）
if document_type == "買掛表":
    _upload_types = ["xlsx", "csv"]
    _upload_help = "買掛表は Excel（XLSX）または CSV でアップロードしてください。"
    # ドロップゾーンの案内文（CSSで日本語化している）も差し替える
    st.markdown(
        """
        <style>
        [data-testid="stFileUploaderDropzoneInstructions"] > div::after {
            content: "1ファイル200MBまで ・ XLSX / CSV に対応（買掛表はExcelのまま）";
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
else:
    _upload_types = ["pdf", "png", "jpg", "jpeg", "xlsx", "csv"]
    _upload_help = "PDF・PNG・JPG・XLSX・CSV に対応しています。スマートフォンで撮影した領収書の写真も使えます。"

# 通帳のときは、補助科目マスタ（普通預金）に登録された銀行から口座を選べる
bank_sub = None
if document_type == "通帳":
    _banks = [r["sub_name"] for r in storage.list_subaccounts(client, "普通預金")]
    if _banks:
        _bank_choice = st.selectbox(
            "銀行（通帳の口座）",
            ["（指定なし）"] + _banks,
            help="選んだ銀行が、仕訳の普通預金側の補助科目に入ります。",
        )
        if _bank_choice != "（指定なし）":
            bank_sub = _bank_choice
    else:
        st.caption(
            "💡 上の「🗂 事前登録①：補助科目マスタ」で普通預金の補助科目（銀行名）を登録すると、"
            "ここで口座を選べるようになります。"
        )

uploaded_files = st.file_uploader(
    "ファイルをアップロード（複数選択できます）",
    type=_upload_types,
    accept_multiple_files=True,
    help=_upload_help,
)

# 同じファイルを2回アップロードして仕訳が重複する事故を防ぐ。
# 意図的に再取り込みしたい場合のみチェックを入れてもらう
reimport_ok = st.checkbox(
    "取り込み済みと同名のファイルも再度取り込む（仕訳が重複します）",
    value=False,
    key="reimport_ok",
)

# 仕訳表（data_editor）の状態管理。DBを書き換える操作の後は key を変えて
# エディタを作り直す（古い編集状態が新しい行に誤って適用されるのを防ぐ）
st.session_state.setdefault("ledger_rev", 0)


def _ledger_editor_key() -> str:
    return f"ledger_editor_{st.session_state['ledger_rev']}"


def _bump_ledger() -> None:
    st.session_state["ledger_rev"] += 1


# 組み込みルールが使う勘定科目（マスタ未登録のクライアントでもプルダウンに出す）
_BUILTIN_ACCOUNTS = sorted(
    set(BS_ACCOUNTS)
    | {account for _kws, account in EXPENSE_RULES}
    | {account for _kws, account in INCOME_RULES}
    | {"雑費", "売上高", "雑収入", "現金", "普通預金", "未払金", "預り金", "諸口"}
)


def _account_options(target_client: str, df: pd.DataFrame) -> list[str]:
    """仕訳表の勘定科目プルダウンの選択肢。

    クライアントの勘定科目マスタ（事前登録）を優先し、組み込みの科目と
    今の仕訳に入っている科目も含める（選択肢に無い値があると表示が
    崩れるため）。
    """
    master = [r["name"] for r in storage.list_account_master(target_client)]
    in_use = [
        str(v).strip()
        for col in ("借方勘定科目", "貸方勘定科目")
        for v in df[col].tolist()
        if str(v).strip()
    ]
    options = list(dict.fromkeys(master + _BUILTIN_ACCOUNTS + in_use))
    return options


def _tax_sync(changes: dict) -> dict:
    """科目を変えたのに税区分は触っていない行について、税区分を科目に合わせる。"""
    synced = {}
    for acct_col, tax_col in (("借方勘定科目", "借方税区分"), ("貸方勘定科目", "貸方税区分")):
        if acct_col in changes and tax_col not in changes:
            account = str(changes[acct_col] or "").strip()
            if account:
                synced[tax_col] = yayoi_tax(account)
    return synced


def _learn_from_row_edit(target_client: str, original_desc, changes: dict) -> int:
    """仕訳表の直接編集から摘要・科目のルールを学習する。学習件数を返す。

    - 摘要が書き換えられた → 「元の摘要 → 新しい摘要」を摘要ルールに
      （例: セブン-イレブン川崎店 → 飲食代。次回同じ店の摘要は自動で置き換わる）
    - 勘定科目が書き換えられた → 「元の摘要 → 新しい科目」を科目ルールに
    """
    keyword = str(original_desc or "").strip()
    if len(keyword) < 2:
        return 0
    learned = 0
    if "摘要" in changes:
        new_desc = str(changes["摘要"]).strip()
        if new_desc and new_desc != keyword and storage.add_desc_rule(target_client, keyword, new_desc):
            learned += 1
    if "借方勘定科目" in changes:
        account = str(changes["借方勘定科目"]).strip()
        if account and yayoi_tax(account) != "対象外":
            if storage.add_account_rule(keyword, account, side="expense"):
                learned += 1
    if "貸方勘定科目" in changes:
        account = str(changes["貸方勘定科目"]).strip()
        if account and yayoi_tax(account) != "対象外":
            if storage.add_account_rule(keyword, account, side="income"):
                learned += 1
    return learned


def _persist_pending_edits(target_client: str) -> bool:
    """仕訳表の未保存の編集（「変更を保存」前のもの）をDBに書き込む。

    表を編集したまま次のファイルを取り込むと編集が消える、という事故を
    防ぐため、取り込みの直前に呼ぶ。保存した場合 True を返す。
    """
    state = st.session_state.get(_ledger_editor_key())
    if not state:
        return False
    edited = state.get("edited_rows") or {}
    added = state.get("added_rows") or []
    deleted = state.get("deleted_rows") or []
    if not (edited or added or deleted):
        return False

    df = storage.load_entries(target_client)
    for row_idx, changes in edited.items():
        row_idx = int(row_idx)
        if row_idx < len(df):
            _learn_from_row_edit(target_client, df.iloc[row_idx]["摘要"], changes)
            for col, value in {**changes, **_tax_sync(changes)}.items():
                if col in df.columns:
                    df.iloc[row_idx, df.columns.get_loc(col)] = value
    if deleted:
        removed = {int(d) for d in deleted}
        df = df.iloc[[i for i in range(len(df)) if i not in removed]]
    for row in added:
        base = {
            "取引日付": datetime.now().strftime("%Y/%m/%d"),
            "借方勘定科目": "", "借方補助科目": "", "借方税区分": "対象外",
            "貸方勘定科目": "", "貸方補助科目": "", "貸方税区分": "対象外",
            "金額": 0, "摘要": "", "要確認": True, "出典ファイル": "",
        }
        base.update({k: v for k, v in row.items() if k in base})
        df = pd.concat([df, pd.DataFrame([base])], ignore_index=True)
    try:
        storage.replace_entries(target_client, df)
        return True
    except Exception:
        return False


if st.button("変換を開始", type="primary"):
    if not uploaded_files:
        st.warning("ファイルをアップロードしてください。")
    else:
        # 表の編集が「変更を保存」前でも消えないよう、先に自動保存する
        if _persist_pending_edits(client):
            st.info("💾 仕訳表の未保存の編集を自動保存してから取り込みます。")
        progress = st.progress(0.0)
        added_total = 0
        # 一括置換から学習したルール（組み込みルールより優先して科目を決める）
        _rules = storage.list_account_rules()
        learned_expense = [(r["keyword"], r["account"]) for r in _rules if r["side"] == "expense"]
        learned_income = [(r["keyword"], r["account"]) for r in _rules if r["side"] == "income"]
        # 取り込み済みファイル名（二重取り込みチェック用。OCRを呼ぶ前に判定する）
        imported_names = {name for name, _cnt in storage.list_source_files(client)}
        for i, f in enumerate(uploaded_files):
            if f.name in imported_names and not reimport_ok:
                st.warning(
                    f"⏭ 「{f.name}」は取り込み済みのためスキップしました。"
                    "再度取り込む場合は、アップロード欄の下のチェックを入れてから実行してください。"
                )
                progress.progress((i + 1) / len(uploaded_files))
                continue
            with st.expander(f"📄 {f.name}", expanded=False):
                try:
                    result = None
                    preview = ""
                    new_partners: list[str] = []
                    is_tabular = f.name.lower().endswith((".xlsx", ".csv"))

                    if is_tabular and document_type not in TABULAR_DOC_TYPES:
                        if f.name.lower().endswith(".xlsx"):
                            st.dataframe(pd.read_excel(f))
                        else:
                            st.dataframe(pd.DataFrame(tabular_rows_from_bytes(f.name, f.getvalue())))
                        st.caption(
                            "この書類タイプでは xlsx / CSV はプレビューのみです。自動解析は"
                            "書類タイプ「売上」「売上請求書」「仕入請求書」「買掛表」で対応しています。"
                        )
                    elif is_tabular:
                        # 売上（売掛表）・請求書・買掛表: xlsx / CSV を直接解析（OCR不要）
                        rows = tabular_rows_from_bytes(f.name, f.getvalue())
                        subs_master = storage.list_subaccounts(client)
                        acct_names = [r["name"] for r in storage.list_account_master(client)]
                        rule = storage.get_doctype_rule(client, document_type) or default_doctype_rule(
                            document_type, acct_names
                        )
                        if document_type in PARTNER_LEDGER_TYPES:
                            _side = "sales" if document_type == "売上" else "purchase"
                            pmap = {
                                r["row_no"]: r["partner_name"]
                                for r in storage.list_partner_rows(client, _side)
                            }
                            result, new_partners = parse_partner_ledger(
                                rows, document_type, source_name=f.name, rule=rule,
                                partner_map=pmap, subaccounts=subs_master,
                                custom_expense_rules=learned_expense,
                                custom_income_rules=learned_income,
                            )
                        else:
                            result, new_partners = parse_invoice(
                                rows, document_type, client_name=client, source_name=f.name,
                                rule=rule, subaccounts=subs_master, account_names=acct_names,
                            )
                        preview = "\n".join(
                            " | ".join(c for c in row if c) for row in rows if any(row)
                        )
                    else:
                        # スマホ写真などの大きな画像はOCRの上限(4MB)内に自動圧縮
                        file_bytes, compress_note = compress_image_if_needed(
                            f.getvalue(), f.name
                        )
                        if compress_note:
                            st.caption(f"🗜 {compress_note}")
                        with st.spinner("OCR処理中..."):
                            ocr_lines = run_ocr_lines(file_bytes)
                        texts = [ln.text for ln in ocr_lines]

                        if document_type in TABULAR_DOC_TYPES:
                            # 売上・請求書・買掛表をPDF・画像でもらった場合はOCR経由で
                            # 解析する。読み取り誤りがあり得るため要確認を立て、
                            # 取引先の自動登録もしない
                            subs_master = storage.list_subaccounts(client)
                            acct_names = [r["name"] for r in storage.list_account_master(client)]
                            rule = storage.get_doctype_rule(client, document_type) or default_doctype_rule(
                                document_type, acct_names
                            )
                            if document_type in INVOICE_TYPES:
                                result, _ = parse_invoice(
                                    [[t] for t in texts], document_type,
                                    client_name=client, source_name=f.name,
                                    rule=rule, subaccounts=subs_master,
                                    account_names=acct_names, force_review=True,
                                )
                            else:
                                _side = "sales" if document_type == "売上" else "purchase"
                                pmap = {
                                    r["row_no"]: r["partner_name"]
                                    for r in storage.list_partner_rows(client, _side)
                                }
                                result, _ = parse_partner_ledger(
                                    [[c.text for c in row] for row in group_rows(ocr_lines)],
                                    document_type, source_name=f.name, rule=rule,
                                    partner_map=pmap, subaccounts=subs_master,
                                    custom_expense_rules=learned_expense,
                                    custom_income_rules=learned_income,
                                )
                                for e in result.entries:
                                    e.needs_review = True
                            preview = "\n".join(texts)
                        else:
                            # 領収書×写真は、複数レシートの可能性を最優先で確認する。
                            # 駐車場領収書等は「カード利用明細」等の印字を含み、書類
                            # タイプの自動判定がカード明細に誤反応するため、複数の
                            # かたまりを検出したら自動判定より分割解析を優先する
                            receipt_clusters = None
                            if document_type == "領収書" and is_image_filename(f.name):
                                _clusters = split_text_clusters(ocr_lines)
                                if len(_clusters) > 1:
                                    receipt_clusters = _clusters

                            # 書類タイプの選び間違い対策: OCR内容から自動判定し、
                            # 選択と食い違っていれば判定結果の方で解析する
                            effective_type = document_type
                            if receipt_clusters is None:
                                detected = detect_document_type(texts, selected=document_type)
                                if detected and detected != document_type:
                                    effective_type = detected
                                    st.info(
                                        f"書類の内容から「{detected}」と判定して解析しました"
                                        f"（書類タイプの選択は「{document_type}」でした）。"
                                    )

                            if receipt_clusters is not None:
                                result = parse_receipt_clusters(
                                    [[ln.text for ln in cluster] for cluster in receipt_clusters],
                                    source_name=f.name,
                                    custom_expense_rules=learned_expense,
                                    client_name=client,
                                )
                                st.info(
                                    f"1枚の画像から {len(result.entries)} 件のレシートを検出し、"
                                    "それぞれ解析しました（結果はすべて要確認です）。"
                                )
                                preview = "\n\n――― レシート区切り ―――\n\n".join(
                                    "\n".join(ln.text for ln in cluster)
                                    for cluster in receipt_clusters
                                )
                            elif effective_type == "給与台帳":
                                rows = group_rows(ocr_lines)
                                result = parse_payroll(rows, source_name=f.name)
                                preview = "\n".join(
                                    " | ".join(c.text for c in row) for row in rows
                                )
                            elif effective_type in ("通帳", "カード明細"):
                                # 座標で表の行を復元してから解析する
                                rows = group_rows(ocr_lines)
                                result = parse_table_document(
                                    rows, effective_type, source_name=f.name,
                                    custom_expense_rules=learned_expense,
                                    custom_income_rules=learned_income,
                                )
                                preview = "\n".join(
                                    " | ".join(c.text for c in row) for row in rows
                                )
                                if effective_type == "通帳":
                                    # 銀行（口座）の補助科目と、摘要↔補助科目マスタの突合
                                    subs_master = storage.list_subaccounts(client)
                                    matched_count = 0
                                    for e in result.entries:
                                        is_deposit = e.debit_account == "普通預金"
                                        if bank_sub:
                                            if is_deposit:
                                                e.debit_sub = bank_sub
                                            elif e.credit_account == "普通預金":
                                                e.credit_sub = bank_sub
                                        if not subs_master:
                                            continue
                                        matched = match_subaccount(
                                            e.description, subs_master,
                                            side="deposit" if is_deposit else "withdrawal",
                                        )
                                        if matched:
                                            matched_count += 1
                                            if is_deposit:
                                                e.credit_account = matched["account"]
                                                e.credit_sub = matched["sub_name"]
                                                e.credit_tax = yayoi_tax(matched["account"])
                                            else:
                                                e.debit_account = matched["account"]
                                                e.debit_sub = matched["sub_name"]
                                                e.debit_tax = yayoi_tax(matched["account"])
                                            # 名前の直接一致は確定扱い。サーチキー経由は
                                            # 略称ゆえ誤マッチがあり得るため要確認を残す。
                                            # 残高不一致・年仮定（note あり）の行も残す
                                            if matched["by"] == "name" and not e.note:
                                                e.needs_review = False
                                    if matched_count:
                                        st.caption(
                                            f"🔎 補助科目マスタと {matched_count} 件の摘要が一致しました。"
                                        )
                            else:
                                result = parse_document(
                                    texts, effective_type, source_name=f.name,
                                    custom_expense_rules=learned_expense,
                                    client_name=client,
                                )
                                preview = "\n".join(texts)

                    if result is not None:
                        # 売掛表・請求書に出てきた新しい取引先を補助科目マスタへ
                        # 自動登録する（次回から突合・振り分けが効く）
                        registered: list[str] = []
                        for e in result.entries:
                            sub = e.debit_sub or e.credit_sub
                            if sub and sub in new_partners:
                                account = e.debit_account if e.debit_sub else e.credit_account
                                if storage.add_subaccount(client, account, sub):
                                    registered.append(sub)
                        if registered:
                            st.caption(
                                "🆕 新しい取引先を補助科目マスタに登録しました: "
                                + "、".join(registered)
                            )
                        # 学習済みの摘要ルール（セブンイレブン→飲食代 等）を適用
                        _desc_rules = storage.list_desc_rules(client)
                        if _desc_rules:
                            _replaced = apply_description_rules(result.entries, _desc_rules)
                            if _replaced:
                                st.caption(f"📝 学習済みの摘要ルールを {_replaced} 件に適用しました。")
                        for w in result.warnings:
                            st.warning(w)
                        added = storage.add_entries(client, result.entries, source_file=f.name)
                        added_total += added
                        if added:
                            review = result.needs_review_count
                            st.success(
                                f"{added} 件の仕訳を追加しました"
                                f"（うち要確認 {review} 件）。"
                            )
                        st.text_area("読み取り結果", preview, height=200, key=f"ocr_{i}")
                except AzureOCRError as e:
                    st.error(f"OCRエラー: {e}")
                except Exception as e:
                    st.error(f"処理に失敗しました: {e}")
            progress.progress((i + 1) / len(uploaded_files))

        if added_total:
            _bump_ledger()  # 新しい台帳内容でエディタを作り直す
            st.success(f"合計 {added_total} 件の仕訳を「{client}」の台帳に追加しました。下の表で確認・修正してください。")

# --- 蓄積された仕訳データ ---
st.divider()
st.subheader(f"蓄積された仕訳データ — {client}")

df = storage.load_entries(client)

if df.empty:
    st.caption("まだ仕訳がありません。ファイルをアップロードして「変換を開始」を押してください。")
else:
    tab_edit, tab_output = st.tabs(["✏️ 仕訳の編集", "🖨 出力プレビュー"])

    # --- 編集タブ ---
    with tab_edit:
        if flash := st.session_state.pop("flash", None):
            st.success(flash)
        review_count = int(df["要確認"].sum())
        if review_count:
            st.warning(f"⚠️ 要確認の仕訳が {review_count} 件あります。内容を確認し、修正したらチェックを外してください。")

        _acct_options = _account_options(client, df)
        edited_df = st.data_editor(
            df,
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "取引日付": st.column_config.TextColumn(help="YYYY/MM/DD 形式"),
                "借方勘定科目": st.column_config.SelectboxColumn(
                    options=_acct_options,
                    help="プルダウンから選べます。科目を変えると税区分も自動で合わせます",
                ),
                "貸方勘定科目": st.column_config.SelectboxColumn(
                    options=_acct_options,
                    help="プルダウンから選べます。科目を変えると税区分も自動で合わせます",
                ),
                "金額": st.column_config.NumberColumn(min_value=0, step=1, format="localized"),
                "要確認": st.column_config.CheckboxColumn(help="確認が済んだらチェックを外す"),
                "出典ファイル": st.column_config.TextColumn(disabled=True),
            },
            key=_ledger_editor_key(),
        )

        col_save, col_review, col_clear = st.columns([1, 1, 1])
        with col_save:
            if st.button("💾 変更を保存"):
                try:
                    # 直接編集の差分から摘要・科目のルールを学習する
                    learned_total = 0
                    for idx in df.index.intersection(edited_df.index):
                        changes = {}
                        for col in ("摘要", "借方勘定科目", "貸方勘定科目", "借方税区分", "貸方税区分"):
                            if str(df.loc[idx, col]) != str(edited_df.loc[idx, col]):
                                changes[col] = edited_df.loc[idx, col]
                        if changes:
                            learned_total += _learn_from_row_edit(client, df.loc[idx, "摘要"], changes)
                            # 科目だけ変えた行は税区分も新しい科目に合わせる
                            for tax_col, value in _tax_sync(changes).items():
                                edited_df.loc[idx, tax_col] = value
                    saved = storage.replace_entries(client, edited_df)
                    message = f"{saved} 件を保存しました。"
                    if learned_total:
                        message += f" 編集内容から {learned_total} 件のルールを学習しました（次回から自動適用）。"
                    st.success(message)
                    _bump_ledger()
                    st.rerun()
                except Exception as e:
                    st.error(f"保存に失敗しました: {e}")
        with col_review:
            if st.button("✅ 要確認を一括解除", disabled=not review_count,
                         help="すべての行の「要確認」チェックを外して保存します"):
                cleared = edited_df.copy()
                cleared["要確認"] = False
                storage.replace_entries(client, cleared)
                _bump_ledger()
                st.rerun()
        with col_clear:
            confirm_clear = st.checkbox("全削除を許可", key="confirm_clear")
            if st.button("🗑 台帳を全削除", disabled=not confirm_clear):
                storage.clear_entries(client)
                _bump_ledger()
                st.rerun()

        # --- 科目の一括置換（学習機能付き） ---
        with st.expander("🔁 科目の一括置換（次回からの自動適用も学習できます）"):
            st.caption("摘要にキーワードを含む行の勘定科目をまとめて変更します。税区分も新しい科目に合わせて更新されます。")
            col_kw, col_side, col_acct = st.columns([2, 1, 2])
            bulk_keyword = col_kw.text_input("摘要に含まれるキーワード", key="bulk_keyword",
                                             placeholder="例: タイムズ")
            bulk_side = col_side.radio("変更する列", ["借方", "貸方"], key="bulk_side",
                                       help="経費の科目は借方、通帳の入金の科目は貸方です")
            bulk_account = col_acct.text_input("変更後の勘定科目", key="bulk_account",
                                               placeholder="例: 旅費交通費")
            bulk_new_desc = st.text_input(
                "摘要も書き換える（空欄なら変更しない）", key="bulk_new_desc",
                placeholder="例: 飲食代 ／ セブンイレブン 飲食代（会社の流儀に合わせて自由に）",
            )
            bulk_clear_review = st.checkbox("変更した行の「要確認」を解除する", value=True, key="bulk_clear_review")
            bulk_learn = st.checkbox("このルールを学習し、次回の変換から自動で適用する", value=True, key="bulk_learn")

            if st.button("一括置換を実行", key="bulk_apply"):
                keyword, account = bulk_keyword.strip(), bulk_account.strip()
                if not keyword or not account:
                    st.error("キーワードと変更後の勘定科目を入力してください。")
                else:
                    target_col = "借方勘定科目" if bulk_side == "借方" else "貸方勘定科目"
                    tax_col = "借方税区分" if bulk_side == "借方" else "貸方税区分"
                    updated = edited_df.copy()
                    mask = updated["摘要"].astype(str).str.contains(keyword, case=False, regex=False)
                    count = int(mask.sum())
                    if count == 0:
                        st.warning(f"摘要に「{keyword}」を含む行はありません。")
                    else:
                        updated.loc[mask, target_col] = account
                        updated.loc[mask, tax_col] = yayoi_tax(account)
                        new_desc = bulk_new_desc.strip()
                        if new_desc:
                            updated.loc[mask, "摘要"] = new_desc
                        if bulk_clear_review:
                            updated.loc[mask, "要確認"] = False
                        storage.replace_entries(client, updated)
                        message = f"{count} 件の{target_col}を「{account}」に変更しました。"
                        if new_desc:
                            message += f" 摘要も「{new_desc}」に書き換えました。"
                        if bulk_learn:
                            storage.add_account_rule(
                                keyword, account,
                                side="expense" if bulk_side == "借方" else "income",
                            )
                            if new_desc:
                                storage.add_desc_rule(client, keyword, new_desc)
                            message += " ルールを学習しました（次回の変換から自動適用）。"
                        st.session_state["flash"] = message
                        _bump_ledger()
                        st.rerun()

            learned_rules = storage.list_account_rules()
            if learned_rules:
                st.divider()
                st.caption(f"🧠 学習済みの科目ルール（{len(learned_rules)}件）— 変換時に自動で科目が付きます:")
                for rule in learned_rules:
                    col_r1, col_r2, col_r3 = st.columns([3, 2, 1])
                    col_r1.write(f"摘要に「{rule['keyword']}」")
                    side_label = "借方" if rule["side"] == "expense" else "貸方"
                    col_r2.write(f"→ {side_label}: {rule['account']}")
                    if col_r3.button("削除", key=f"rule_del_{rule['id']}"):
                        storage.delete_account_rule(rule["id"])
                        st.rerun()
            learned_descs = storage.list_desc_rules(client)
            if learned_descs:
                st.divider()
                st.caption(f"🧠 学習済みの摘要ルール（{client}・{len(learned_descs)}件）— 変換時に摘要を書き換えます:")
                for rule in learned_descs:
                    col_d1, col_d2, col_d3 = st.columns([3, 2, 1])
                    col_d1.write(f"摘要に「{rule['keyword']}」")
                    col_d2.write(f"→ 「{rule['description']}」")
                    if col_d3.button("削除", key=f"desc_del_{rule['id']}"):
                        storage.delete_desc_rule(rule["id"])
                        st.rerun()

        # --- ファイル単位の取り消し ---
        with st.expander("🗂 ファイル単位で取り込みを取り消す"):
            st.caption("書類タイプの選び間違いなどで取り込んだ仕訳を、ファイルごとまとめて削除します。")
            source_files = storage.list_source_files(client)
            if not source_files:
                st.caption("ファイル由来の仕訳はありません。")
            else:
                options = [f"{name}（{count}件）" for name, count in source_files]
                selected = st.selectbox("取り消すファイル", options, key="undo_file_select")
                selected_name = source_files[options.index(selected)][0]
                confirm_undo = st.checkbox(
                    f"「{selected_name}」由来の仕訳をすべて削除する",
                    key="undo_file_confirm",
                )
                if st.button("取り込みを取り消す", disabled=not confirm_undo, key="undo_file_btn"):
                    deleted = storage.delete_entries_by_source(client, selected_name)
                    st.session_state["flash"] = f"「{selected_name}」の仕訳 {deleted} 件を削除しました。"
                    _bump_ledger()
                    st.rerun()

    # --- 出力プレビュータブ ---
    with tab_output:
        # 期間（年月）で絞り込んで出力できるようにする（経理の月次業務向け）
        months = sorted(
            {str(d)[:7] for d in edited_df["取引日付"] if len(str(d)) >= 7},
            reverse=True,
        )
        period = st.selectbox(
            "出力する期間",
            ["すべて"] + months,
            key="output_period",
            help="月を選ぶと、その月の仕訳だけをプレビュー・CSV出力します。",
        )
        if period == "すべて":
            target_df = edited_df
        else:
            target_df = edited_df[edited_df["取引日付"].astype(str).str.startswith(period)]

        # プレビューとCSVは編集タブの現在の内容（未保存の修正も含む）から生成する
        csv_error = None
        entries: list[JournalEntry] = []
        try:
            for idx, row in target_df.iterrows():
                entries.append(
                    JournalEntry(
                        date=datetime.strptime(str(row["取引日付"]).strip(), "%Y/%m/%d").date(),
                        debit_account=str(row["借方勘定科目"]).strip(),
                        debit_sub=str(row.get("借方補助科目", "") or "").strip(),
                        credit_account=str(row["貸方勘定科目"]).strip(),
                        credit_sub=str(row.get("貸方補助科目", "") or "").strip(),
                        amount=int(row["金額"]),
                        description=str(row["摘要"]).strip(),
                        debit_tax=str(row["借方税区分"]).strip() or "対象外",
                        credit_tax=str(row["貸方税区分"]).strip() or "対象外",
                    )
                )
        except Exception as e:
            csv_error = f"{int(idx) + 1} 行目の内容が不正です（日付は YYYY/MM/DD、金額は数値）: {e}"

        if csv_error:
            st.error(csv_error + " — 編集タブで修正してください。")
        elif not entries:
            st.info("選択した期間に仕訳がありません。")
        else:
            review_left = int(target_df["要確認"].sum())
            if review_left:
                st.warning(f"⚠️ 要確認が {review_left} 件残っています。出力前に編集タブで確認してください。")

            dates = sorted(e.date for e in entries)
            col1, col2, col3 = st.columns(3)
            col1.metric("仕訳件数", f"{len(entries)} 件")
            col2.metric("合計金額", f"¥{sum(e.amount for e in entries):,}")
            col3.metric("期間", f"{dates[0]:%Y/%m/%d} 〜 {dates[-1]:%Y/%m/%d}")

            preview_df = pd.DataFrame(
                [
                    {
                        "No": i,
                        "取引日付": e.date.strftime("%Y/%m/%d"),
                        "借方科目": e.debit_account + (f"（{e.debit_sub}）" if e.debit_sub else ""),
                        "貸方科目": e.credit_account + (f"（{e.credit_sub}）" if e.credit_sub else ""),
                        "金額": f"{e.amount:,}",
                        "摘要": e.description,
                    }
                    for i, e in enumerate(entries, start=1)
                ]
            ).set_index("No")
            # st.table は全行を描画するので、ブラウザの印刷（⌘P / Ctrl+P)でそのまま印刷できる
            st.table(preview_df)
            st.caption("このプレビューはブラウザの印刷機能（Mac: ⌘P / Windows: Ctrl+P）でそのまま印刷できます。")

            csv_suffix = "" if period == "すべて" else "_" + period.replace("/", "")
            if accounting_software == "弥生":
                st.download_button(
                    "⬇️ 弥生CSVをダウンロード",
                    data=to_yayoi_csv(entries),
                    file_name=f"yayoi_{client}{csv_suffix}.csv",
                    mime="text/csv",
                    type="primary",
                    help="弥生会計デスクトップ版の「仕訳データ」インポート形式（Shift-JIS・ヘッダなし25列）",
                )
            else:
                st.button("⬇️ CSVをダウンロード", disabled=True, help="現在は弥生のみ対応しています。")
