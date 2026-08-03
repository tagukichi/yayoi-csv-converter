import os
from datetime import datetime

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import storage
from accounts import yayoi_tax
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
from parser import detect_document_type, parse_document, parse_table_document
from yayoi_exporter import to_yayoi_csv

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
        content: "1ファイル200MBまで ・ PDF / PNG / JPG / XLSX に対応";
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

with st.expander("📖 使い方", expanded=True):
    st.markdown(
        """
        1. **書類タイプを選択** します（通帳・カード明細・電子請求書・領収書／レシート）
        2. **ファイルをアップロード** します（PDF・PNG・JPG・XLSX。複数まとめて選択できます）
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

document_type = st.selectbox(
    "書類タイプ",
    ["領収書", "電子請求書", "通帳", "カード明細"],
)

uploaded_files = st.file_uploader(
    "ファイルをアップロード（複数選択できます）",
    type=["pdf", "png", "jpg", "jpeg", "xlsx"],
    accept_multiple_files=True,
    help="PDF・PNG・JPG・XLSX に対応しています。スマートフォンで撮影した領収書の写真も使えます。",
)

# 同じファイルを2回アップロードして仕訳が重複する事故を防ぐ。
# 意図的に再取り込みしたい場合のみチェックを入れてもらう
reimport_ok = st.checkbox(
    "取り込み済みと同名のファイルも再度取り込む（仕訳が重複します）",
    value=False,
    key="reimport_ok",
)

if st.button("変換を開始", type="primary"):
    if not uploaded_files:
        st.warning("ファイルをアップロードしてください。")
    else:
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
                    if f.name.lower().endswith(".xlsx"):
                        df = pd.read_excel(f)
                        st.dataframe(df)
                        st.caption("xlsx の自動解析は未対応です（プレビューのみ）。")
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

                        # 書類タイプの選び間違い対策: OCR内容から自動判定し、
                        # 選択と食い違っていれば判定結果の方で解析する
                        effective_type = document_type
                        detected = detect_document_type(texts)
                        if detected and detected != document_type:
                            effective_type = detected
                            st.info(
                                f"書類の内容から「{detected}」と判定して解析しました"
                                f"（書類タイプの選択は「{document_type}」でした）。"
                            )

                        if effective_type in ("通帳", "カード明細"):
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
                        else:
                            # 写真の領収書は、1枚に複数写っている場合に備えて
                            # 座標のかたまりごとに分割してそれぞれ解析する
                            clusters = (
                                split_text_clusters(ocr_lines)
                                if effective_type == "領収書" and is_image_filename(f.name)
                                else [ocr_lines]
                            )
                            if len(clusters) > 1:
                                st.info(
                                    f"1枚の画像から {len(clusters)} 件のレシートを検出し、"
                                    "それぞれ解析しました（分割結果はすべて要確認です）。"
                                )
                                result = ParseResult()
                                for cluster in clusters:
                                    partial = parse_document(
                                        [ln.text for ln in cluster],
                                        effective_type,
                                        source_name=f.name,
                                        custom_expense_rules=learned_expense,
                                    )
                                    for e in partial.entries:
                                        e.needs_review = True
                                    result.entries.extend(partial.entries)
                                    result.warnings.extend(partial.warnings)
                                preview = "\n\n――― レシート区切り ―――\n\n".join(
                                    "\n".join(ln.text for ln in cluster)
                                    for cluster in clusters
                                )
                            else:
                                result = parse_document(
                                    texts, effective_type, source_name=f.name,
                                    custom_expense_rules=learned_expense,
                                )
                                preview = "\n".join(texts)
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
                        st.text_area("OCR結果", preview, height=200, key=f"ocr_{i}")
                except AzureOCRError as e:
                    st.error(f"OCRエラー: {e}")
                except Exception as e:
                    st.error(f"処理に失敗しました: {e}")
            progress.progress((i + 1) / len(uploaded_files))

        if added_total:
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

        edited_df = st.data_editor(
            df,
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "取引日付": st.column_config.TextColumn(help="YYYY/MM/DD 形式"),
                "金額": st.column_config.NumberColumn(min_value=0, step=1, format="localized"),
                "要確認": st.column_config.CheckboxColumn(help="確認が済んだらチェックを外す"),
                "出典ファイル": st.column_config.TextColumn(disabled=True),
            },
            key="ledger_editor",
        )

        col_save, col_review, col_clear = st.columns([1, 1, 1])
        with col_save:
            if st.button("💾 変更を保存"):
                try:
                    saved = storage.replace_entries(client, edited_df)
                    st.success(f"{saved} 件を保存しました。")
                    st.rerun()
                except Exception as e:
                    st.error(f"保存に失敗しました: {e}")
        with col_review:
            if st.button("✅ 要確認を一括解除", disabled=not review_count,
                         help="すべての行の「要確認」チェックを外して保存します"):
                cleared = edited_df.copy()
                cleared["要確認"] = False
                storage.replace_entries(client, cleared)
                st.rerun()
        with col_clear:
            confirm_clear = st.checkbox("全削除を許可", key="confirm_clear")
            if st.button("🗑 台帳を全削除", disabled=not confirm_clear):
                storage.clear_entries(client)
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
                        if bulk_clear_review:
                            updated.loc[mask, "要確認"] = False
                        storage.replace_entries(client, updated)
                        message = f"{count} 件の{target_col}を「{account}」に変更しました。"
                        if bulk_learn:
                            storage.add_account_rule(
                                keyword, account,
                                side="expense" if bulk_side == "借方" else "income",
                            )
                            message += " ルールを学習しました（次回の変換から自動適用）。"
                        st.session_state["flash"] = message
                        st.rerun()

            learned_rules = storage.list_account_rules()
            if learned_rules:
                st.divider()
                st.caption(f"🧠 学習済みルール（{len(learned_rules)}件）— 変換時に自動で科目が付きます:")
                for rule in learned_rules:
                    col_r1, col_r2, col_r3 = st.columns([3, 2, 1])
                    col_r1.write(f"摘要に「{rule['keyword']}」")
                    side_label = "借方" if rule["side"] == "expense" else "貸方"
                    col_r2.write(f"→ {side_label}: {rule['account']}")
                    if col_r3.button("削除", key=f"rule_del_{rule['id']}"):
                        storage.delete_account_rule(rule["id"])
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
                        credit_account=str(row["貸方勘定科目"]).strip(),
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
                        "借方科目": e.debit_account,
                        "貸方科目": e.credit_account,
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
