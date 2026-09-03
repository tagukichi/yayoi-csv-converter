"""画面のテーマ（CSS）と、デザイン用のHTML部品。

デザイン案「サイドバー型ダッシュボード」（紺のサイドバー・白カード・
IBM Plex Sans JP）を Streamlit 上で再現するための CSS と、ヘッダ・
サマリーカード・ステータスピルなどの小さなHTML部品をまとめる。
配色は .streamlit/config.toml の [theme] と揃えている。

サービス名・事務所名は未定のため定数にしてある（決まったらここを変える）。
"""

from __future__ import annotations

import html

import streamlit as st

SERVICE_NAME = "[サービス名]"
OFFICE_LABEL = "[事務所名] ・ 管理者"

# デザイントークン
NAVY = "#202741"
ACCENT = "#34459b"
ACCENT_LIGHT = "#4d5ec4"
AMBER = "#b7791f"
AMBER_BG = "#faf1df"
AMBER_TEXT = "#8a5c14"
GREEN = "#1e7f5c"
GREEN_BG = "#e7f4ee"
GRAY_TEXT = "#6b7183"
MUTED = "#8a8f9e"
BORDER = "#e6e5e0"

_FONT_LINK = (
    '<link rel="stylesheet" '
    'href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+JP:wght@400;500;700&display=swap">'
)

# 注意: st.markdown は Markdown として解釈するため、<style> ブロックの中に
# 空行があるとそこでHTMLブロックが終わり、残りが本文として表示されてしまう。
# inject_css() で空行を取り除いてから差し込む。
_CSS = f"""
<style>
/* ---------- 全体 ---------- */
.block-container {{
    padding-top: 1.4rem;
    padding-bottom: 2rem;
    max-width: 1240px;
}}
header[data-testid="stHeader"] {{ background: transparent; }}
h1, h2, h3 {{ letter-spacing: 0.01em; }}

/* ---------- サイドバー ---------- */
section[data-testid="stSidebar"] {{
    width: 300px !important;
    border-right: 0;
}}
section[data-testid="stSidebar"] [data-testid="stSelectbox"] [data-baseweb="select"] > div > div {{
    font-size: 15px;
}}
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {{
    padding: 1.2rem 0.9rem 1rem 0.9rem;
}}
.yc-logo {{
    display: flex; align-items: center; gap: 10px;
    padding: 2px 6px 14px 6px; color: #ffffff;
    font-size: 18px; font-weight: 700; letter-spacing: 0.02em;
}}
.yc-logo .mark {{
    width: 32px; height: 32px; border-radius: 8px; background: {ACCENT_LIGHT};
    display: flex; align-items: center; justify-content: center; flex-shrink: 0;
}}
.yc-side-label {{
    font-size: 12px; color: #9aa3c0; margin: 8px 6px 3px 6px;
}}
/* クライアント選択（サイドバー内のセレクトボックス） */
section[data-testid="stSidebar"] [data-testid="stSelectbox"] [data-baseweb="select"] > div {{
    background: rgba(255,255,255,0.07);
    border: 0;
    color: #ffffff;
}}
section[data-testid="stSidebar"] [data-testid="stSelectbox"] svg {{ fill: #9aa3c0; }}
section[data-testid="stSidebar"] [data-testid="stExpander"] details {{
    border: 0; background: transparent;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] summary {{
    color: #9aa3c0; padding: 4px 6px;
}}
section[data-testid="stSidebar"] [data-testid="stExpander"] summary p {{
    font-size: 14px; white-space: nowrap;
}}
/* ナビ（ラジオをナビ風に） */
section[data-testid="stSidebar"] [data-testid="stRadio"] div[role="radiogroup"] {{
    gap: 2px;
}}
section[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"] {{
    padding: 12px 14px; border-radius: 8px; margin: 0; width: 100%;
    color: #c0c6dd; cursor: pointer; align-items: center;
    transition: background 0.12s ease;
}}
section[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"]:hover {{
    background: rgba(255,255,255,0.06);
}}
section[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"] > div:first-of-type {{
    display: none;  /* ラジオの丸を隠す */
}}
section[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"] p {{
    color: inherit; font-size: 15px; margin: 0;
}}
section[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"]:has(input:checked) {{
    background: {ACCENT_LIGHT}; color: #ffffff; font-weight: 700;
}}
/* 事前登録（1つ目）と日々の作業、学習ルール（5つ目）の間に区切り線 */
section[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"]:nth-of-type(2),
section[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"]:nth-of-type(5) {{
    margin-top: 10px; position: relative;
}}
section[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"]:nth-of-type(2)::before,
section[data-testid="stSidebar"] [data-testid="stRadio"] label[data-baseweb="radio"]:nth-of-type(5)::before {{
    content: ""; position: absolute; left: 4px; right: 4px; top: -6px;
    height: 1px; background: rgba(255,255,255,0.08);
}}
.yc-side-footer {{
    margin-top: 20px; padding-top: 14px; border-top: 1px solid rgba(255,255,255,0.08);
    font-size: 12.5px; color: #9aa3c0; display: flex; flex-direction: column; gap: 6px;
}}
.yc-side-footer .dot {{
    display: inline-block; width: 7px; height: 7px; border-radius: 50%;
    background: #4cc38a; margin-right: 6px; vertical-align: middle;
}}

/* ---------- ヘッダーバー ---------- */
.yc-header {{
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    background: #ffffff; border: 1px solid {BORDER}; border-radius: 10px;
    padding: 12px 20px; margin-bottom: 16px;
}}
.yc-header .title {{ font-size: 16px; font-weight: 700; color: #20242e; }}
.yc-header .meta {{ margin-left: auto; font-size: 12px; color: {GRAY_TEXT}; }}

/* ---------- カード ---------- */
[data-testid="stVerticalBlockBorderWrapper"] {{
    background: #ffffff;
    border-color: {BORDER} !important;
    border-radius: 10px !important;
}}
.yc-card-title {{
    font-size: 13px; font-weight: 700; color: #454a59; margin: 2px 0 6px 0;
}}
.yc-hint {{ font-size: 12px; color: {MUTED}; }}

/* ---------- ピル・バッジ ---------- */
.yc-pill {{
    display: inline-block; padding: 3px 10px; border-radius: 999px;
    font-size: 12px; font-weight: 700; white-space: nowrap;
}}
.yc-pill.ok {{ background: {GREEN_BG}; color: {GREEN}; }}
.yc-pill.warn {{ background: {AMBER_BG}; color: {AMBER}; }}
.yc-pill.muted {{ background: #f1f1ee; color: {MUTED}; }}
.yc-pill.info {{ background: #eceef8; color: {ACCENT}; }}

/* ---------- お知らせ（黄色バナー） ---------- */
.yc-notice {{
    padding: 10px 16px; border-radius: 8px; font-size: 13px;
    display: flex; align-items: center; gap: 10px; margin-bottom: 12px;
}}
.yc-notice.warn {{ background: {AMBER_BG}; color: {AMBER_TEXT}; }}
.yc-notice.ok {{ background: {GREEN_BG}; color: {GREEN}; }}

/* ---------- サマリーカード ---------- */
.yc-summary {{
    display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px;
    margin-bottom: 16px;
}}
.yc-summary .card {{
    background: #ffffff; border: 1px solid {BORDER}; border-radius: 10px;
    padding: 14px 18px; display: flex; flex-direction: column; gap: 6px;
}}
.yc-summary .label {{ font-size: 11px; color: {GRAY_TEXT}; }}
.yc-summary .value {{ font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }}
.yc-summary .value small {{ font-size: 13px; font-weight: 400; color: {GRAY_TEXT}; }}
.yc-summary .value.ok {{ font-size: 15px; color: {GREEN}; }}
.yc-summary .value.warn {{ font-size: 15px; color: {AMBER}; }}

/* ---------- 取り込みログ ---------- */
.yc-log {{ background: #ffffff; border: 1px solid {BORDER}; border-radius: 10px; overflow: hidden; }}
.yc-log .head {{
    padding: 12px 18px; border-bottom: 1px solid #eeede9; display: flex; align-items: center;
    font-size: 13px; font-weight: 700; color: #454a59;
}}
.yc-log .head span {{ margin-left: auto; font-size: 12px; font-weight: 400; color: {MUTED}; }}
.yc-log .row {{
    display: flex; align-items: center; gap: 14px; padding: 10px 18px;
    border-bottom: 1px solid #f2f1ed; font-size: 13px;
}}
.yc-log .row:last-child {{ border-bottom: 0; }}
.yc-log .row .name {{ flex-grow: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.yc-log .row .detail {{ font-size: 12px; color: {GRAY_TEXT}; }}
.yc-log .row.skip .name, .yc-log .row.skip .detail {{ color: {MUTED}; }}
.yc-log .empty {{ padding: 18px; font-size: 12px; color: {MUTED}; }}

/* ---------- 出力プレビュー表 ---------- */
.yc-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
.yc-table th {{
    text-align: left; font-size: 11px; font-weight: 700; color: {GRAY_TEXT};
    background: #f6f6f3; padding: 9px 12px; border-bottom: 1px solid {BORDER};
}}
.yc-table td {{ padding: 9px 12px; border-bottom: 1px solid #f2f1ed; vertical-align: top; }}
.yc-table td.num, .yc-table th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.yc-table td.no {{ color: {MUTED}; }}
.yc-table td.desc {{ color: #454a59; }}
.yc-table-wrap {{ background: #ffffff; border: 1px solid {BORDER}; border-radius: 10px; overflow: hidden; }}
.yc-table-wrap .head {{
    padding: 12px 18px; border-bottom: 1px solid #eeede9; display: flex; align-items: center;
    font-size: 13px; font-weight: 700; color: #454a59;
}}
.yc-table-wrap .head span {{ margin-left: auto; font-size: 12px; font-weight: 400; color: {MUTED}; }}
.yc-table-wrap .foot {{
    padding: 10px 18px; border-top: 1px solid {BORDER}; background: #fbfbf9;
    font-size: 12px; color: {GRAY_TEXT};
}}

/* ---------- ボタン ---------- */
/* Streamlit の既定は上下の余白が薄いので、押しやすい高さにする */
button[data-testid="stBaseButton-primary"],
button[data-testid="stBaseButton-secondary"],
[data-testid="stDownloadButton"] button {{
    padding: 0.6rem 1.4rem; min-height: 2.9rem; font-size: 15px;
}}
button[data-testid="stBaseButton-primary"] {{
    background: {ACCENT}; border-color: {ACCENT}; font-weight: 700;
}}
button[data-testid="stBaseButton-primary"]:hover {{
    background: #2a3880; border-color: #2a3880;
}}
button[data-testid="stBaseButton-secondary"] {{
    border-color: #d8d7d2; color: #454a59; background: #ffffff;
}}
[data-testid="stDownloadButton"] button {{
    background: {GREEN} !important; border-color: {GREEN} !important;
    color: #ffffff !important; font-weight: 700;
}}
[data-testid="stDownloadButton"] button:hover {{
    background: #17694b !important; border-color: #17694b !important;
}}

/* ---------- 書類タイプのチップ（pills） ---------- */
button[data-testid="stBaseButton-pills"] {{
    background: #f1f1ee; border: 0; color: #454a59; border-radius: 8px;
    padding: 9px 18px; font-size: 14px;
}}
button[data-testid="stBaseButton-pillsActive"] {{
    background: {ACCENT}; border: 0; color: #ffffff; border-radius: 8px;
    padding: 9px 18px; font-size: 14px; font-weight: 700;
}}
button[data-testid="stBaseButton-pillsActive"] p, button[data-testid="stBaseButton-pillsActive"] span {{
    color: #ffffff;
}}

/* ---------- ファイルアップローダ（ドロップゾーンの日本語化と装飾） ---------- */
section[data-testid="stFileUploaderDropzone"] {{
    background: #ffffff; border: 1.5px dashed #b9bdd4; border-radius: 12px;
    padding: 28px 20px;
}}
[data-testid="stFileUploaderDropzoneInstructions"] > div > span {{
    display: none;
}}
[data-testid="stFileUploaderDropzoneInstructions"] > div::before {{
    content: "ここにファイルをドラッグ＆ドロップ";
    font-weight: 700; font-size: 15px; display: block; color: #20242e;
}}
[data-testid="stFileUploaderDropzoneInstructions"] > div::after {{
    content: var(--yc-upload-note, "1ファイル200MBまで ・ PDF / PNG / JPG / XLSX / CSV に対応");
    font-size: 12px; color: {MUTED}; display: block; margin-top: 0.25rem;
}}
/* 「Browse files」ボタンは文字だけ隠し、::after で日本語を重ねる。
   重ねる文字の分の幅・高さを min-width / min-height で確保する */
[data-testid="stFileUploaderDropzone"] button {{
    visibility: hidden; position: relative;
    background: {ACCENT}; border-color: {ACCENT};
    min-width: 190px; min-height: 3.2rem; padding: 0.7rem 1.8rem;
}}
[data-testid="stFileUploaderDropzone"] button::after {{
    content: "ファイルを選択";
    visibility: visible; position: absolute; inset: 0;
    display: flex; align-items: center; justify-content: center;
    white-space: nowrap; background: {ACCENT}; color: #ffffff;
    border-radius: 8px; font-weight: 700; font-size: 16px;
}}

/* ---------- 仕訳表 ---------- */
[data-testid="stDataFrame"], [data-testid="stDataEditor"] {{
    border: 1px solid {BORDER}; border-radius: 10px; overflow: hidden; background: #ffffff;
}}
</style>
"""


def inject_css() -> None:
    """テーマCSS（フォント読み込み含む）をページに差し込む。毎回の描画で呼ぶ。"""
    st.markdown(_FONT_LINK, unsafe_allow_html=True)
    compact = "\n".join(line for line in _CSS.splitlines() if line.strip())
    st.markdown(compact, unsafe_allow_html=True)


def set_upload_note(text: str) -> None:
    """ドロップゾーンの案内文（対応形式）を差し替える。"""
    st.markdown(
        f"<style>:root {{ --yc-upload-note: \"{html.escape(text)}\"; }}</style>",
        unsafe_allow_html=True,
    )


def page_header(title: str, meta: str = "", extra_html: str = "") -> None:
    """画面上部のヘッダーバー。"""
    meta_html = f'<div class="meta">{html.escape(meta)}</div>' if meta else ""
    st.markdown(
        f'<div class="yc-header"><div class="title">{html.escape(title)}</div>'
        f"{extra_html}{meta_html}</div>",
        unsafe_allow_html=True,
    )


def pill(text: str, kind: str = "info") -> str:
    """ステータスピルのHTML（kind: ok / warn / muted / info）。"""
    return f'<span class="yc-pill {kind}">{html.escape(text)}</span>'


def notice(text: str, kind: str = "warn") -> None:
    """黄色（warn）／緑（ok）のお知らせバナー。"""
    icon = "⚠️" if kind == "warn" else "✅"
    st.markdown(
        f'<div class="yc-notice {kind}"><span>{icon}</span><span>{html.escape(text)}</span></div>',
        unsafe_allow_html=True,
    )


def card_title(text: str, hint: str = "") -> None:
    hint_html = f'<div class="yc-hint">{html.escape(hint)}</div>' if hint else ""
    st.markdown(f'<div class="yc-card-title">{html.escape(text)}</div>{hint_html}', unsafe_allow_html=True)


def summary_cards(cards: list[tuple[str, str, str]]) -> None:
    """サマリーカード（4枚）。cards: [(ラベル, 値のHTML, クラス)]。"""
    inner = "".join(
        f'<div class="card"><div class="label">{html.escape(label)}</div>'
        f'<div class="value {cls}">{value_html}</div></div>'
        for label, value_html, cls in cards
    )
    st.markdown(f'<div class="yc-summary">{inner}</div>', unsafe_allow_html=True)


def sidebar_logo() -> None:
    st.markdown(
        f"""
        <div class="yc-logo">
          <div class="mark"><svg width="16" height="16" viewBox="0 0 16 16" fill="none">
            <path d="M3 12.5 L8 3.5 L13 12.5" stroke="#ffffff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"></path>
            <path d="M5 9.5 H11" stroke="#ffffff" stroke-width="1.8" stroke-linecap="round"></path></svg></div>
          <div>{html.escape(SERVICE_NAME)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def sidebar_footer(backend: str) -> None:
    st.markdown(
        f"""
        <div class="yc-side-footer">
          <div><span class="dot"></span>保存先: {html.escape(backend)}</div>
          <div>{html.escape(OFFICE_LABEL)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
