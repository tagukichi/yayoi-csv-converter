from datetime import datetime

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import storage
from models import JournalEntry
from ocr import AzureOCRError, credentials_available, run_ocr
from parser import parse_document
from yayoi_exporter import to_yayoi_csv

load_dotenv()

st.set_page_config(page_title="PDF → 弥生CSV 変換ツール", page_icon="📄", layout="wide")

st.title("PDF → 弥生CSV 変換ツール")

# --- サイドバー ---
with st.sidebar:
    st.header("設定")

    client = st.selectbox(
        "クライアント企業",
        ["A建設", "B工務店", "C社"],
    )

    accounting_software = st.radio(
        "出力する会計ソフト",
        ["弥生", "マネーフォワード", "freee"],
    )
    if accounting_software != "弥生":
        st.caption("⚠️ 現在は弥生のみ対応しています（他は対応予定）。")

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
                            lines = run_ocr(f.getvalue())
                        result = parse_document(lines, document_type, source_name=f.name)
                        for w in result.warnings:
                            st.warning(w)
                        added = storage.add_entries(client, result.entries, source_file=f.name)
                        added_total += added
                        if added:
                            st.success(f"{added} 件の仕訳を追加しました（要確認として登録）。")
                        st.text_area("OCR結果", "\n".join(lines), height=200, key=f"ocr_{i}")
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
    review_count = int(df["要確認"].sum())
    if review_count:
        st.warning(f"⚠️ 要確認の仕訳が {review_count} 件あります。内容を確認し、修正したらチェックを外してください。")

    edited_df = st.data_editor(
        df,
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "取引日付": st.column_config.TextColumn(help="YYYY/MM/DD 形式"),
            "金額": st.column_config.NumberColumn(min_value=0, step=1, format="%d"),
            "要確認": st.column_config.CheckboxColumn(help="確認が済んだらチェックを外す"),
            "出典ファイル": st.column_config.TextColumn(disabled=True),
        },
        key="ledger_editor",
    )

    col_save, col_csv, col_clear = st.columns([1, 1, 1])

    with col_save:
        if st.button("💾 変更を保存"):
            try:
                saved = storage.replace_entries(client, edited_df)
                st.success(f"{saved} 件を保存しました。")
                st.rerun()
            except Exception as e:
                st.error(f"保存に失敗しました: {e}")

    with col_csv:
        # ダウンロードは保存済みではなく表の現在の内容から生成する
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
            st.error(csv_error)
        else:
            if accounting_software == "弥生":
                st.download_button(
                    "⬇️ 弥生CSVをダウンロード",
                    data=to_yayoi_csv(entries),
                    file_name=f"yayoi_{client}.csv",
                    mime="text/csv",
                    help="弥生会計デスクトップ版の「仕訳データ」インポート形式（Shift-JIS・ヘッダなし25列）",
                )
            else:
                st.button("⬇️ CSVをダウンロード", disabled=True, help="現在は弥生のみ対応しています。")

    with col_clear:
        confirm_clear = st.checkbox("全削除を許可", key="confirm_clear")
        if st.button("🗑 台帳を全削除", disabled=not confirm_clear):
            storage.clear_entries(client)
            st.rerun()
