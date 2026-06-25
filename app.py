from datetime import date

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from models import JournalEntry
from ocr import AzureOCRError, credentials_available, run_ocr
from yayoi_exporter import to_yayoi_csv

load_dotenv()

st.set_page_config(page_title="PDF → 弥生CSV 変換ツール", page_icon="📄")

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

# --- メインエリア ---
if not credentials_available():
    st.error(
        "Azure の認証情報が見つかりません。"
        "`.env` に `AZURE_VISION_ENDPOINT` と `AZURE_VISION_KEY` を設定してください"
        "（`.env.example` 参照）。"
    )

document_type = st.selectbox(
    "書類タイプ",
    ["通帳", "カード明細", "電子請求書", "領収書"],
)

uploaded_files = st.file_uploader(
    "ファイルをアップロード",
    type=["pdf", "png", "jpg", "xlsx"],
    accept_multiple_files=True,
)

if uploaded_files:
    st.subheader("アップロード済みファイル")
    for f in uploaded_files:
        st.write(f"- {f.name}")

if st.button("変換を開始"):
    if not uploaded_files:
        st.warning("ファイルをアップロードしてください。")
    else:
        progress = st.progress(0.0)
        for i, f in enumerate(uploaded_files):
            with st.expander(f"📄 {f.name}", expanded=True):
                try:
                    if f.name.lower().endswith(".xlsx"):
                        df = pd.read_excel(f)
                        st.dataframe(df)
                    else:
                        with st.spinner("OCR処理中..."):
                            lines = run_ocr(f.getvalue())
                        st.text("\n".join(lines))
                except AzureOCRError as e:
                    st.error(f"OCRエラー: {e}")
                except Exception as e:
                    st.error(f"処理に失敗しました: {e}")
            progress.progress((i + 1) / len(uploaded_files))

        st.info(
            "OCR結果の表示まで動作しています。"
            "OCR結果 → 仕訳データへの解析（書類タイプ別）は実装中です。"
        )

# --- 弥生CSV出力フォーマットの確認用デモ ---
# 解析層が未完成のため、ここはサンプルの仕訳データから弥生CSVを生成するデモ。
# ダウンロードしたCSVを実際の弥生会計に取り込んで、フォーマットの妥当性を
# 早めに検証するために使う。
with st.expander("🧪 弥生CSV 出力フォーマットの確認（デモ）"):
    st.caption(
        "サンプルの仕訳データから弥生インポート形式CSV（Shift-JIS）を生成します。"
        "実際の弥生会計に取り込めるか、このCSVで先に確認してください。"
    )
    sample_entries = [
        JournalEntry(
            date=date(2026, 4, 1),
            debit_account="旅費交通費",
            credit_account="普通預金",
            amount=1500,
            description="ETC利用料",
        ),
        JournalEntry(
            date=date(2026, 4, 3),
            debit_account="普通預金",
            credit_account="売掛金",
            amount=715000,
            description="売掛金回収 もくとさい農園",
        ),
    ]
    preview = pd.DataFrame(
        [
            {
                "日付": e.date.strftime("%Y/%m/%d"),
                "借方": e.debit_account,
                "貸方": e.credit_account,
                "金額": e.amount,
                "摘要": e.description,
            }
            for e in sample_entries
        ]
    )
    st.dataframe(preview, use_container_width=True)
    st.download_button(
        "弥生CSV（サンプル）をダウンロード",
        data=to_yayoi_csv(sample_entries),
        file_name="yayoi_sample.csv",
        mime="text/csv",
    )
