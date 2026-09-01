"""PDF → 弥生CSV 変換ツール（画面の入口）。

サイドバー型ダッシュボード: 左に紺のサイドバー（サービス名・クライアント
切替・ナビ）、右に選んだ画面を表示する。画面ごとの中身は views.py。
"""

import os

import streamlit as st
from dotenv import load_dotenv

import storage
import ui_theme as T
import views

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

st.set_page_config(
    page_title="PDF → 弥生CSV 変換ツール", page_icon="📄", layout="wide",
    initial_sidebar_state="expanded",
)
T.inject_css()


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
    st.title(T.SERVICE_NAME)
    st.caption("PDF → 弥生CSV 変換ツール")
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


# --- サイドバー: ロゴ・クライアント切替・ナビ ---
with st.sidebar:
    T.sidebar_logo()

    clients = storage.list_clients()
    st.markdown('<div class="yc-side-label">クライアント企業</div>', unsafe_allow_html=True)
    client = st.selectbox("クライアント企業", clients, label_visibility="collapsed") if clients else None

    with st.expander("企業の追加・削除"):
        new_client = st.text_input("追加する企業名", key="new_client_name")
        if st.button("追加", key="add_client"):
            if storage.add_client(new_client):
                st.rerun()
            else:
                st.error("空欄か、すでに登録済みの企業名です。")
        if client:
            confirm_delete = st.checkbox(
                f"「{client}」を削除する（蓄積した仕訳も削除されます）",
                key="confirm_delete_client",
            )
            if st.button("削除", key="delete_client", disabled=not confirm_delete):
                storage.delete_client(client)
                st.rerun()

    review_n = views.review_count(client) if client else 0

    def _nav_label(item: str) -> str:
        icons = {
            views.NAV_IMPORT: ":material/upload:",
            views.NAV_LEDGER: ":material/table_rows:",
            views.NAV_EXPORT: ":material/download:",
            views.NAV_MASTERS: ":material/menu_book:",
            views.NAV_RULES: ":material/psychology:",
        }
        label = f"{icons[item]} {item}"
        if item == views.NAV_LEDGER and review_n:
            label += f" :orange-background[{review_n}]"
        return label

    nav = st.radio(
        "メニュー", views.NAV_ITEMS, format_func=_nav_label,
        key="nav", label_visibility="collapsed",
    )

    T.sidebar_footer(storage.backend_name())

if client is None:
    T.page_header("はじめに")
    st.info("サイドバーの「企業の追加・削除」からクライアント企業を登録してください。")
    st.stop()

# 仕訳表の未保存の編集は、画面を離れると widget の状態が消えるので
# 切り替え時に自動保存する（表を編集したまま他の画面へ行っても消えない）
if nav != views.NAV_LEDGER and views.persist_pending_edits(client):
    st.toast("💾 仕訳表の編集を自動保存しました。")

if nav == views.NAV_IMPORT:
    views.render_import(client)
elif nav == views.NAV_LEDGER:
    views.render_ledger(client)
elif nav == views.NAV_EXPORT:
    views.render_export(client)
elif nav == views.NAV_MASTERS:
    views.render_masters(client)
else:
    views.render_rules(client)
