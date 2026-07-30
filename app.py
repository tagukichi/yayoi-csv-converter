import os
from datetime import datetime

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import storage
from models import JournalEntry
from ocr import AzureOCRError, credentials_available, group_rows, run_ocr_lines
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
    "ファイルをアップロード",
    type=["pdf", "png", "jpg", "xlsx"],
    accept_multiple_files=True,
)

if st.button("変換を開始", type="primary"):
    if not uploaded_files:
        st.warning("ファイルをアップロードしてください。")
    else:
        progress = st.progress(0.0)
        added_total = 0
        for i, f in enumerate(uploaded_files):
            with st.expander(f"📄 {f.name}", expanded=False):
                try:
                    if f.name.lower().endswith(".xlsx"):
                        df = pd.read_excel(f)
                        st.dataframe(df)
                        st.caption("xlsx の自動解析は未対応です（プレビューのみ）。")
                    else:
                        with st.spinner("OCR処理中..."):
                            ocr_lines = run_ocr_lines(f.getvalue())
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
                            result = parse_table_document(rows, effective_type, source_name=f.name)
                            preview = "\n".join(
                                " | ".join(c.text for c in row) for row in rows
                            )
                        else:
                            result = parse_document(texts, effective_type, source_name=f.name)
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

    # --- 出力プレビュータブ ---
    with tab_output:
        # プレビューとCSVは編集タブの現在の内容（未保存の修正も含む）から生成する
        csv_error = None
        entries: list[JournalEntry] = []
        try:
            for idx, row in edited_df.iterrows():
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
        else:
            review_left = int(edited_df["要確認"].sum())
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

            if accounting_software == "弥生":
                st.download_button(
                    "⬇️ 弥生CSVをダウンロード",
                    data=to_yayoi_csv(entries),
                    file_name=f"yayoi_{client}.csv",
                    mime="text/csv",
                    type="primary",
                    help="弥生会計デスクトップ版の「仕訳データ」インポート形式（Shift-JIS・ヘッダなし25列）",
                )
            else:
                st.button("⬇️ CSVをダウンロード", disabled=True, help="現在は弥生のみ対応しています。")
