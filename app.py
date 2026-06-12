import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from ocr import AzureOCRError, credentials_available, run_ocr

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

        st.info("弥生CSVへの変換ロジックは実装予定です（現在はOCR結果の表示まで）。")
