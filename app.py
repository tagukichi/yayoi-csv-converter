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
    page_title="PDF → 弥生CSV 変換ツール", layout="wide",
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
    # サービス名はヘッダーに出しているので、サイドバーは企業選択から始める。
    # 顧問先が増えても探せるよう、要確認の残件数つき・並び替えできる形にする
    clients = storage.list_clients()
    client = None
    if clients:
        _prefs = storage.list_client_prefs()
        _review_counts = storage.review_counts_by_client()
        _sort_label = st.session_state.get("client_sort", "最近使った順")
        clients = storage.sort_clients(
            clients, _prefs, _review_counts, storage.CLIENT_SORTS[_sort_label]
        )

        def _client_label(name: str) -> str:
            """ピン留めの印と、要確認の残件数を名前の後ろに付ける。"""
            label = f"{'★ ' if _prefs.get(name, {}).get('pinned') else ''}{name}"
            n = _review_counts.get(name, 0)
            return f"{label} ・要確認 {n}" if n else label

        st.markdown('<div class="yc-side-label">クライアント企業</div>', unsafe_allow_html=True)
        # 並び替えを変えると選択肢の順番が変わり、Streamlit はセレクタを作り直す。
        # そのとき選択が先頭の企業に移らないよう、選んでいた企業を覚えておいて
        # その位置を初期値にする
        _selected = st.session_state.get("current_client")
        client = st.selectbox(
            "クライアント企業", clients, format_func=_client_label,
            index=clients.index(_selected) if _selected in clients else 0,
            label_visibility="collapsed",
            help="名前を入力すると絞り込めます",
        )
        st.session_state["current_client"] = client

        col_sort, col_pin = st.columns([3, 1])
        col_sort.selectbox(
            "並び替え", list(storage.CLIENT_SORTS), key="client_sort",
            label_visibility="collapsed",
        )
        _pinned = _prefs.get(client, {}).get("pinned", False)
        if col_pin.button(
            "★" if _pinned else "☆", key="pin_client", use_container_width=True,
            help="ピン留めするとセレクタの先頭に固定されます",
        ):
            storage.set_client_pinned(client, not _pinned)
            st.rerun()

        # 「最近使った順」のために、企業を切り替えたときだけ時刻を記録する
        if st.session_state.get("last_client") != client:
            st.session_state["last_client"] = client
            storage.touch_client_opened(client)

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
        if item == views.NAV_MASTERS and client and not views.setup_done(client):
            label += " :orange-background[要]"
        return label

    # 事前登録が済んでいない企業では「事前登録」から、済んでいれば
    # 「書類の取り込み」から始める（最初の描画時のみ効く）
    nav = st.radio(
        "メニュー", views.NAV_ITEMS, format_func=_nav_label,
        index=views.NAV_ITEMS.index(
            views.NAV_IMPORT if client and views.setup_done(client) else views.NAV_MASTERS
        ),
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
    st.toast("仕訳表の編集を自動保存しました。")

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
