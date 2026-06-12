import streamlit as st

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
    st.info("実装予定")
