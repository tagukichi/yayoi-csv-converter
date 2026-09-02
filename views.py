"""各画面の描画（サイドバー型ダッシュボードの5画面）。

- render_import:  書類の取り込み（書類タイプ → アップロード → 変換）
- render_ledger:  仕訳の編集（要確認の絞り込み・保存・一括置換・取り消し）
- render_export:  弥生CSV出力（期間・サマリー・プレビュー・ダウンロード）
- render_masters: 事前登録（勘定科目・補助科目マスタ、書類タイプの紐付け、行番号対応）
- render_rules:   学習ルール（科目ルール・摘要ルールの一覧と削除）

画面の切り替えはサイドバーのナビ（app.py）で行い、仕訳表の未保存の編集は
画面を離れるときに自動保存する（persist_pending_edits）。
"""

from __future__ import annotations

import html
from datetime import datetime

import pandas as pd
import streamlit as st

import storage
import ui_theme as T
from accounts import BS_ACCOUNTS, EXPENSE_RULES, INCOME_RULES, yayoi_tax
from doc_parser import (
    apply_description_rules,
    detect_document_type,
    parse_document,
    parse_payroll,
    parse_receipt_clusters,
    parse_table_document,
)
from models import JournalEntry
from ocr import (
    AzureOCRError,
    compress_image_if_needed,
    credentials_available,
    group_rows,
    is_image_filename,
    run_ocr_lines,
    split_text_clusters,
)
from sales_parser import (
    INVOICE_TYPES,
    PARTNER_LEDGER_TYPES,
    default_doctype_rule,
    parse_invoice,
    parse_partner_ledger,
    tabular_rows_from_bytes,
)
from descdict import apply_desc_dictionary, dict_terms_by_account
from submaster import (
    match_subaccount,
    parse_yayoi_account_pdf,
    parse_yayoi_desc_dict_pdf,
    parse_yayoi_subaccount_pdf,
)
from yayoi_exporter import to_yayoi_csv

# 表形式（xlsx/CSV）の自動解析に対応した書類タイプ
TABULAR_DOC_TYPES = PARTNER_LEDGER_TYPES + INVOICE_TYPES

DOC_TYPES = [
    "領収書", "電子請求書", "通帳", "カード明細", "給与台帳",
    "売上", "売上請求書", "仕入請求書", "買掛表",
]

# サイドバーのナビ項目（app.py と共有）
NAV_IMPORT = "書類の取り込み"
NAV_LEDGER = "仕訳の編集"
NAV_EXPORT = "弥生CSV出力"
NAV_MASTERS = "事前登録"
NAV_RULES = "学習ルール"
NAV_ITEMS = [NAV_IMPORT, NAV_LEDGER, NAV_EXPORT, NAV_MASTERS, NAV_RULES]


# =====================================================================
# 仕訳表の状態管理
# =====================================================================


def ledger_editor_key() -> str:
    st.session_state.setdefault("ledger_rev", 0)
    return f"ledger_editor_{st.session_state['ledger_rev']}"


def bump_ledger() -> None:
    """DBを書き換えたあとに呼び、仕訳表（data_editor）を作り直す。"""
    st.session_state.setdefault("ledger_rev", 0)
    st.session_state["ledger_rev"] += 1


def review_count(client: str) -> int:
    df = storage.load_entries(client)
    return int(df["要確認"].sum()) if not df.empty else 0


def _shown_df(full: pd.DataFrame) -> pd.DataFrame:
    """絞り込み（要確認のみ）を適用した表示用DataFrame（元のindexを保持）。"""
    if st.session_state.get("ledger_filter") == "review":
        return full[full["要確認"]]
    return full


def _merge_editor_result(
    full: pd.DataFrame, shown: pd.DataFrame, edited: pd.DataFrame
) -> pd.DataFrame:
    """絞り込み表示中の編集結果を、表示していない行を保ったまま全体に反映する。

    data_editor は編集行は元のindexのまま、削除行は除き、追加行は新しい
    indexで返すので、それを手掛かりに合成する。
    """
    kept = [i for i in edited.index if i in shown.index]
    deleted = shown.index.difference(edited.index)
    added = edited.loc[[i for i in edited.index if i not in shown.index]]
    result = full.drop(index=deleted)
    if kept:
        result.loc[kept, edited.columns] = edited.loc[kept, edited.columns]
    if len(added):
        result = pd.concat([result, added], ignore_index=False)
    return result.reset_index(drop=True)


# 組み込みルールが使う勘定科目（マスタ未登録のクライアントでもプルダウンに出す）
_BUILTIN_ACCOUNTS = sorted(
    set(BS_ACCOUNTS)
    | {account for _kws, account in EXPENSE_RULES}
    | {account for _kws, account in INCOME_RULES}
    | {"雑費", "売上高", "雑収入", "現金", "普通預金", "未払金", "預り金", "諸口"}
)


def account_options(target_client: str, df: pd.DataFrame) -> list[str]:
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
    return list(dict.fromkeys(master + _BUILTIN_ACCOUNTS + in_use))


def desc_sync(changes: dict, terms_by_account: dict[str, list[str]]) -> dict:
    """科目を変えたのに摘要は触っていない行について、新しい科目の辞書の摘要が
    1つだけならそれを入れる（複数あるときは「摘要辞書から入れる」で選ぶ）。"""
    if "摘要" in changes or not terms_by_account:
        return {}
    for acct_col in ("借方勘定科目", "貸方勘定科目"):
        if acct_col in changes:
            terms = terms_by_account.get(str(changes[acct_col] or "").strip(), [])
            if len(terms) == 1:
                return {"摘要": terms[0]}
    return {}


def tax_sync(changes: dict) -> dict:
    """科目を変えたのに税区分は触っていない行について、税区分を科目に合わせる。"""
    synced = {}
    for acct_col, tax_col in (("借方勘定科目", "借方税区分"), ("貸方勘定科目", "貸方税区分")):
        if acct_col in changes and tax_col not in changes:
            account = str(changes[acct_col] or "").strip()
            if account:
                synced[tax_col] = yayoi_tax(account)
    return synced


def learn_from_row_edit(target_client: str, original_desc, changes: dict) -> int:
    """仕訳表の直接編集から摘要・科目のルールを学習する。学習件数を返す。

    - 摘要が書き換えられた → 「元の摘要 → 新しい摘要」を摘要ルールに
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


def persist_pending_edits(target_client: str) -> bool:
    """仕訳表の未保存の編集（「変更を保存」前のもの）をDBに書き込む。

    画面を切り替えると表の編集状態が消えるため、切り替え時と取り込みの
    直前に呼ぶ。保存した場合 True を返す。
    """
    state = st.session_state.get(ledger_editor_key())
    if not state:
        return False
    edited = state.get("edited_rows") or {}
    added = state.get("added_rows") or []
    deleted = state.get("deleted_rows") or []
    if not (edited or added or deleted):
        return False

    full = storage.load_entries(target_client)
    shown = _shown_df(full)
    _terms = dict_terms_by_account(storage.list_desc_dict(target_client))
    positions = list(shown.index)  # 表の行番号 → 全体のindex
    for row_pos, changes in edited.items():
        row_pos = int(row_pos)
        if row_pos < len(positions):
            idx = positions[row_pos]
            learn_from_row_edit(target_client, full.loc[idx, "摘要"], changes)
            for col, value in {**changes, **tax_sync(changes), **desc_sync(changes, _terms)}.items():
                if col in full.columns:
                    full.loc[idx, col] = value
    if deleted:
        drop_idx = [positions[int(d)] for d in deleted if int(d) < len(positions)]
        full = full.drop(index=drop_idx)
    for row in added:
        base = {
            "取引日付": datetime.now().strftime("%Y/%m/%d"),
            "借方勘定科目": "", "借方補助科目": "", "借方税区分": "対象外",
            "貸方勘定科目": "", "貸方補助科目": "", "貸方税区分": "対象外",
            "金額": 0, "摘要": "", "要確認": True, "出典ファイル": "",
        }
        base.update({k: v for k, v in row.items() if k in base})
        full = pd.concat([full, pd.DataFrame([base])], ignore_index=True)
    try:
        storage.replace_entries(target_client, full.reset_index(drop=True))
        bump_ledger()
        return True
    except Exception:
        return False


# =====================================================================
# 書類の取り込み
# =====================================================================


def _doc_type_hint(document_type: str) -> str:
    if document_type == "通帳":
        return "残高の計算が合わない行には要確認が付きます。口座（銀行）を選ぶと補助科目に入ります。"
    if document_type == "買掛表":
        return (
            "取引先名（または行番号）と当月金額の表をExcel（xlsx・CSV）のままアップロードしてください。"
            "月末日付・取引先ごとに1本の仕訳（税込10%）を作ります。"
        )
    if document_type == "売上":
        return (
            "取引先別の月次売上一覧（売掛表）の写真・PDFを読み取り、月末日付・取引先ごとに"
            "1本の仕訳（税込10%）を作ります。行番号だけの表は「事前登録」で取引先名を登録してください。"
        )
    if document_type in INVOICE_TYPES:
        return (
            "請求書の写真・PDFから「当月合計額＋消費税」を1本の仕訳にします。"
            "取引先は補助科目に入り、初めての取引先は自動でマスタに登録されます。"
        )
    if document_type == "給与台帳":
        return "会計事務所の給与仕訳ルール（発生主義・月末日付・諸口）で複数行の仕訳を作ります。"
    return "迷ったらそのままでOK。内容から自動判定します（カード明細・通帳など）。"


def _log_add(name: str, doc_type: str, detail: str, kind: str, status: str) -> None:
    st.session_state.setdefault("import_log", [])
    st.session_state["import_log"].insert(
        0, {"name": name, "doc_type": doc_type, "detail": detail, "kind": kind, "status": status}
    )


def _render_import_log(slot) -> None:
    log = st.session_state.get("import_log", [])
    rows = []
    for item in log[:12]:
        rows.append(
            f'<div class="row {"skip" if item["kind"] == "muted" else ""}">'
            f'<span>📄</span>'
            f'<div class="name">{html.escape(item["name"])}</div>'
            f'<div class="detail">{html.escape(item["doc_type"])}'
            f'{(" ・ " + html.escape(item["detail"])) if item["detail"] else ""}</div>'
            f'{T.pill(item["status"], item["kind"])}</div>'
        )
    body = "".join(rows) if rows else '<div class="empty">まだ取り込みはありません。ファイルをアップロードして「変換を開始」を押してください。</div>'
    slot.markdown(
        f'<div class="yc-log"><div class="head">今日の取り込み'
        f'<span>同じ名前のファイルは自動でスキップされます</span></div>{body}</div>',
        unsafe_allow_html=True,
    )


def render_import(client: str) -> None:
    df_all = storage.load_entries(client)
    review = int(df_all["要確認"].sum()) if not df_all.empty else 0
    T.page_header(
        NAV_IMPORT,
        meta=f"蓄積された仕訳: {len(df_all)}件 ・ 要確認: {review}件",
    )

    if not credentials_available():
        st.error(
            "Azure の認証情報が見つかりません。"
            "`.env` に `AZURE_VISION_ENDPOINT` と `AZURE_VISION_KEY` を設定してください"
            "（`.env.example` 参照）。"
        )

    # --- 1. 書類タイプ ---
    with st.container(border=True):
        T.card_title("1. 書類タイプを選ぶ")
        document_type = st.pills(
            "書類タイプ", DOC_TYPES, default="領収書", key="doc_type_pill",
            label_visibility="collapsed",
        ) or "領収書"
        st.markdown(f'<div class="yc-hint">{html.escape(_doc_type_hint(document_type))}</div>', unsafe_allow_html=True)

        # 通帳のときは、補助科目マスタ（普通預金）に登録された銀行から口座を選べる
        bank_sub = None
        if document_type == "通帳":
            _banks = [r["sub_name"] for r in storage.list_subaccounts(client, "普通預金")]
            if _banks:
                _bank_choice = st.selectbox(
                    "銀行（通帳の口座）", ["（指定なし）"] + _banks,
                    help="選んだ銀行が、仕訳の普通預金側の補助科目に入ります。",
                )
                if _bank_choice != "（指定なし）":
                    bank_sub = _bank_choice
            else:
                st.caption("💡 「事前登録」で普通預金の補助科目（銀行名）を登録すると、ここで口座を選べます。")

    # --- 2. アップロード ---
    # 買掛表はExcelでもらう運用のため、アップロードもExcel（xlsx/CSV）に限定する
    if document_type == "買掛表":
        upload_types = ["xlsx", "csv"]
        T.set_upload_note("1ファイル200MBまで ・ XLSX / CSV に対応（買掛表はExcelのまま）")
    else:
        upload_types = ["pdf", "png", "jpg", "jpeg", "xlsx", "csv"]
        T.set_upload_note("PDF / PNG / JPG / XLSX / CSV ・ 複数選択可 ・ スマホ写真は自動で圧縮されます")

    with st.container(border=True):
        T.card_title("2. ファイルをアップロードして変換")
        uploaded_files = st.file_uploader(
            "ファイルをアップロード（複数選択できます）",
            type=upload_types, accept_multiple_files=True, label_visibility="collapsed",
        )
        col_btn, col_opt = st.columns([1, 2])
        run = col_btn.button("変換を開始", type="primary", use_container_width=True)
        reimport_ok = col_opt.checkbox(
            "取り込み済みと同名のファイルも再度取り込む（仕訳が重複します）",
            value=False, key="reimport_ok",
        )

    log_slot = st.empty()
    details = st.container()

    if run:
        if not uploaded_files:
            st.warning("ファイルをアップロードしてください。")
        else:
            with details:
                _run_conversion(client, uploaded_files, document_type, bank_sub, reimport_ok)

    _render_import_log(log_slot)

    with st.expander("📖 使い方"):
        st.markdown(
            """
            1. **書類タイプを選ぶ**（領収書／レシート・通帳・カード明細・給与台帳・売上・請求書・買掛表）
            2. **ファイルをアップロード**して **「変換を開始」** をクリック（読み取りに数十秒かかることがあります）
               - 買掛表は **ExcelやCSVのまま** アップロードします（OCR不要）。他の書類は写真・PDFでOKです
            3. **「仕訳の編集」** で内容を確認します
               - **「要確認」にチェックが付いた行**は、勘定科目を自動で判断できなかった行です。
                 摘要を見て科目を修正し、確認できたらチェックを外してください
               - 通帳は残高の計算が合わない行にもチェックが付きます（読み取り誤りの可能性）
               - 修正すると次回から自動で適用されます（学習）
            4. **「弥生CSV出力」** で件数・合計金額を確認し、**「弥生CSVをダウンロード」** をクリック
               - 期間（月）で絞って出力できます
               - ダウンロードしたCSVを弥生会計の「仕訳データのインポート」から取り込んでください

            仕訳はクライアント企業ごとに蓄積されるので、書類を数回に分けてアップロードし、
            最後にまとめてCSVを出力することもできます。
            **同じ名前のファイルを2回アップロードしても重複しないよう自動でスキップ**されます。
            間違えて取り込んだ場合は、「仕訳の編集」の「ファイル単位で取り込みを取り消す」から戻せます。
            """
        )


def _run_conversion(client, uploaded_files, document_type, bank_sub, reimport_ok) -> None:
    # 表の編集が「変更を保存」前でも消えないよう、先に自動保存する
    if persist_pending_edits(client):
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
            _log_add(f.name, document_type, "取り込み済みのためスキップ", "muted", "スキップ")
            progress.progress((i + 1) / len(uploaded_files))
            continue
        with st.expander(f"📄 {f.name}", expanded=False):
            try:
                result, preview, new_partners, detail = _parse_uploaded_file(
                    client, f, document_type, bank_sub, learned_expense, learned_income
                )
                if result is None:
                    _log_add(f.name, document_type, detail or "プレビューのみ", "muted", "未解析")
                else:
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
                        st.caption("🆕 新しい取引先を補助科目マスタに登録しました: " + "、".join(registered))
                    # 摘要辞書（弥生の摘要科目一覧）で摘要・科目をその会社の流儀に揃える
                    _dict = storage.list_desc_dict(client)
                    if _dict:
                        _dict_applied = apply_desc_dictionary(result.entries, _dict, context_text=preview)
                        if _dict_applied:
                            st.caption(f"📚 摘要辞書から {_dict_applied} 件の摘要・科目を決めました。")
                    # 学習済みの摘要ルール（セブンイレブン→飲食代 等）を適用（辞書より優先）
                    _desc_rules = storage.list_desc_rules(client)
                    if _desc_rules:
                        _replaced = apply_description_rules(result.entries, _desc_rules)
                        if _replaced:
                            st.caption(f"📝 学習済みの摘要ルールを {_replaced} 件に適用しました。")
                    for w in result.warnings:
                        st.warning(w)
                    added = storage.add_entries(client, result.entries, source_file=f.name)
                    added_total += added
                    review = result.needs_review_count
                    if added:
                        st.success(f"{added} 件の仕訳を追加しました（うち要確認 {review} 件）。")
                        if review:
                            _log_add(f.name, document_type, detail, "warn", f"要確認 {review}件")
                        else:
                            _log_add(f.name, document_type, detail, "ok", f"仕訳 {added}件")
                    else:
                        _log_add(f.name, document_type, detail or "仕訳にできる行なし", "muted", "0件")
                    st.text_area("読み取り結果", preview, height=200, key=f"ocr_{i}_{f.name}")
            except AzureOCRError as e:
                st.error(f"OCRエラー: {e}")
                _log_add(f.name, document_type, "OCRエラー", "warn", "エラー")
            except Exception as e:
                st.error(f"処理に失敗しました: {e}")
                _log_add(f.name, document_type, "処理に失敗", "warn", "エラー")
        progress.progress((i + 1) / len(uploaded_files))

    if added_total:
        bump_ledger()  # 新しい台帳内容でエディタを作り直す
        st.success(
            f"合計 {added_total} 件の仕訳を「{client}」の台帳に追加しました。"
            "「仕訳の編集」で確認・修正してください。"
        )


def _parse_uploaded_file(client, f, document_type, bank_sub, learned_expense, learned_income):
    """1ファイルを解析する。戻り値: (ParseResult|None, プレビュー文字列, 新規取引先, 補足)。"""
    is_tabular = f.name.lower().endswith((".xlsx", ".csv"))

    if is_tabular and document_type not in TABULAR_DOC_TYPES:
        if f.name.lower().endswith(".xlsx"):
            st.dataframe(pd.read_excel(f))
        else:
            st.dataframe(pd.DataFrame(tabular_rows_from_bytes(f.name, f.getvalue())))
        st.caption(
            "この書類タイプでは xlsx / CSV はプレビューのみです。"
            "自動解析は「買掛表」（Excel）で対応しています。"
        )
        return None, "", [], "プレビューのみ"

    if is_tabular:
        # 買掛表など: xlsx / CSV を直接解析（OCR不要）
        rows = tabular_rows_from_bytes(f.name, f.getvalue())
        subs_master = storage.list_subaccounts(client)
        acct_names = [r["name"] for r in storage.list_account_master(client)]
        rule = storage.get_doctype_rule(client, document_type) or default_doctype_rule(
            document_type, acct_names
        )
        if document_type in PARTNER_LEDGER_TYPES:
            _side = "sales" if document_type == "売上" else "purchase"
            pmap = {r["row_no"]: r["partner_name"] for r in storage.list_partner_rows(client, _side)}
            result, new_partners = parse_partner_ledger(
                rows, document_type, source_name=f.name, rule=rule,
                partner_map=pmap, subaccounts=subs_master,
                custom_expense_rules=learned_expense, custom_income_rules=learned_income,
            )
        else:
            result, new_partners = parse_invoice(
                rows, document_type, client_name=client, source_name=f.name,
                rule=rule, subaccounts=subs_master, account_names=acct_names,
            )
        preview = "\n".join(" | ".join(c for c in row if c) for row in rows if any(row))
        return result, preview, new_partners, "Excelを直接解析"

    # --- OCR経由 ---
    # スマホ写真などの大きな画像はOCRの上限(4MB)内に自動圧縮
    file_bytes, compress_note = compress_image_if_needed(f.getvalue(), f.name)
    if compress_note:
        st.caption(f"🗜 {compress_note}")
    with st.spinner("OCR処理中..."):
        ocr_lines = run_ocr_lines(file_bytes)
    texts = [ln.text for ln in ocr_lines]

    if document_type in TABULAR_DOC_TYPES:
        # 売上・請求書をPDF・画像でもらった場合。読み取り誤りがあり得るため
        # 要確認を立て、取引先の自動登録もしない
        subs_master = storage.list_subaccounts(client)
        acct_names = [r["name"] for r in storage.list_account_master(client)]
        rule = storage.get_doctype_rule(client, document_type) or default_doctype_rule(
            document_type, acct_names
        )
        if document_type in INVOICE_TYPES:
            result, _ = parse_invoice(
                [[t] for t in texts], document_type, client_name=client, source_name=f.name,
                rule=rule, subaccounts=subs_master, account_names=acct_names, force_review=True,
            )
        else:
            _side = "sales" if document_type == "売上" else "purchase"
            pmap = {r["row_no"]: r["partner_name"] for r in storage.list_partner_rows(client, _side)}
            result, _ = parse_partner_ledger(
                [[c.text for c in row] for row in group_rows(ocr_lines)],
                document_type, source_name=f.name, rule=rule,
                partner_map=pmap, subaccounts=subs_master,
                custom_expense_rules=learned_expense, custom_income_rules=learned_income,
            )
            for e in result.entries:
                e.needs_review = True
        return result, "\n".join(texts), [], "OCRで読み取り"

    # 領収書×写真は、複数レシートの可能性を最優先で確認する。
    # 駐車場領収書等は「カード利用明細」等の印字を含み、書類タイプの
    # 自動判定がカード明細に誤反応するため、複数のかたまりを検出したら
    # 自動判定より分割解析を優先する
    receipt_clusters = None
    if document_type == "領収書" and is_image_filename(f.name):
        _clusters = split_text_clusters(ocr_lines)
        if len(_clusters) > 1:
            receipt_clusters = _clusters

    # 書類タイプの選び間違い対策: OCR内容から自動判定し、
    # 選択と食い違っていれば判定結果の方で解析する
    effective_type = document_type
    detail = ""
    if receipt_clusters is None:
        detected = detect_document_type(texts, selected=document_type)
        if detected and detected != document_type:
            effective_type = detected
            detail = f"内容から「{detected}」と判定"
            st.info(
                f"書類の内容から「{detected}」と判定して解析しました"
                f"（書類タイプの選択は「{document_type}」でした）。"
            )

    if receipt_clusters is not None:
        result = parse_receipt_clusters(
            [[ln.text for ln in cluster] for cluster in receipt_clusters],
            source_name=f.name, custom_expense_rules=learned_expense, client_name=client,
        )
        detail = f"{len(result.entries)}件のレシートを検出"
        st.info(
            f"1枚の画像から {len(result.entries)} 件のレシートを検出し、"
            "それぞれ解析しました（結果はすべて要確認です）。"
        )
        preview = "\n\n――― レシート区切り ―――\n\n".join(
            "\n".join(ln.text for ln in cluster) for cluster in receipt_clusters
        )
    elif effective_type == "給与台帳":
        rows = group_rows(ocr_lines)
        result = parse_payroll(rows, source_name=f.name)
        preview = "\n".join(" | ".join(c.text for c in row) for row in rows)
    elif effective_type in ("通帳", "カード明細"):
        # 座標で表の行を復元してから解析する
        rows = group_rows(ocr_lines)
        result = parse_table_document(
            rows, effective_type, source_name=f.name,
            custom_expense_rules=learned_expense, custom_income_rules=learned_income,
        )
        preview = "\n".join(" | ".join(c.text for c in row) for row in rows)
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
                    e.description, subs_master, side="deposit" if is_deposit else "withdrawal",
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
                    # 名前の直接一致は確定扱い。サーチキー経由は略称ゆえ誤マッチが
                    # あり得るため要確認を残す。残高不一致・年仮定（note あり）の行も残す
                    if matched["by"] == "name" and not e.note:
                        e.needs_review = False
            if matched_count:
                st.caption(f"🔎 補助科目マスタと {matched_count} 件の摘要が一致しました。")
            if bank_sub:
                detail = (detail + " ・ " if detail else "") + bank_sub
    else:
        result = parse_document(
            texts, effective_type, source_name=f.name,
            custom_expense_rules=learned_expense, client_name=client,
        )
        preview = "\n".join(texts)
    return result, preview, [], detail


# =====================================================================
# 仕訳の編集
# =====================================================================


def render_ledger(client: str) -> None:
    full = storage.load_entries(client)
    review = int(full["要確認"].sum()) if not full.empty else 0
    T.page_header(NAV_LEDGER, meta=f"{len(full)}件の仕訳 ・ 学習ルール {len(storage.list_account_rules()) + len(storage.list_desc_rules(client))}件")

    if flash := st.session_state.pop("flash", None):
        st.success(flash)

    if full.empty:
        st.info("まだ仕訳がありません。「書類の取り込み」からファイルをアップロードしてください。")
        return

    col_filter, col_save = st.columns([3, 1])
    with col_filter:
        st.pills(
            "表示", ["all", "review"],
            format_func=lambda k: f"すべて {len(full)}" if k == "all" else f"要確認のみ {review}",
            default="all", key="ledger_filter", label_visibility="collapsed",
        )
    shown = _shown_df(full)

    if review:
        T.notice(
            f"要確認の仕訳が {review}件 あります。科目・金額を確認してチェックを外してください。"
            "修正内容は自動で学習されます。"
        )
    else:
        T.notice("要確認の仕訳はありません。CSV出力に進めます。", kind="ok")

    _acct_options = account_options(client, full)
    _dict_terms = dict_terms_by_account(storage.list_desc_dict(client))
    edited = st.data_editor(
        shown,
        num_rows="dynamic",
        use_container_width=True,
        height=min(60 + 35 * max(len(shown), 3), 620),
        column_config={
            "取引日付": st.column_config.TextColumn(help="YYYY/MM/DD 形式", width="small"),
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
        key=ledger_editor_key(),
    )
    merged = _merge_editor_result(full, shown, edited)

    with col_save:
        if st.button("💾 変更を保存", type="primary", use_container_width=True):
            try:
                # 直接編集の差分から摘要・科目のルールを学習する
                learned_total = 0
                for idx in shown.index.intersection(edited.index):
                    changes = {}
                    for col in ("摘要", "借方勘定科目", "貸方勘定科目", "借方税区分", "貸方税区分"):
                        if str(shown.loc[idx, col]) != str(edited.loc[idx, col]):
                            changes[col] = edited.loc[idx, col]
                    if changes:
                        learned_total += learn_from_row_edit(client, shown.loc[idx, "摘要"], changes)
                        # 科目だけ変えた行は税区分（と辞書の摘要が1つなら摘要）も合わせる
                        for col, value in {**tax_sync(changes), **desc_sync(changes, _dict_terms)}.items():
                            edited.loc[idx, col] = value
                merged = _merge_editor_result(full, shown, edited)
                saved = storage.replace_entries(client, merged)
                message = f"{saved} 件を保存しました。"
                if learned_total:
                    message += f" 編集内容から {learned_total} 件のルールを学習しました（次回から自動適用）。"
                st.session_state["flash"] = message
                bump_ledger()
                st.rerun()
            except Exception as e:
                st.error(f"保存に失敗しました: {e}")

    st.caption(
        f"{len(shown)}件を表示 ・ 摘要や科目を書き換えると、次回から自動で適用されます"
        "（学習したルールは「学習ルール」で確認・削除できます）"
    )

    col_review, col_confirm, col_clear, _ = st.columns([1.2, 1, 1, 1.8])
    with col_review:
        if st.button("✅ 要確認を一括解除", disabled=not review, use_container_width=True,
                     help="すべての行の「要確認」チェックを外して保存します"):
            cleared = merged.copy()
            cleared["要確認"] = False
            storage.replace_entries(client, cleared)
            bump_ledger()
            st.rerun()
    with col_confirm:
        confirm_clear = st.checkbox("全削除を許可", key="confirm_clear")
    with col_clear:
        if st.button("🗑 台帳を全削除", disabled=not confirm_clear, use_container_width=True):
            storage.clear_entries(client)
            bump_ledger()
            st.rerun()

    # --- 摘要辞書から摘要を入れる（科目を選ぶ → その科目に登録された摘要を選ぶ） ---
    with st.expander("📚 摘要辞書から摘要を入れる（科目を選んで、登録された摘要を選ぶ）"):
        if not _dict_terms:
            st.caption("「事前登録」の「③摘要辞書」に弥生の摘要科目一覧を登録すると使えます。")
        else:
            st.caption("選んだ勘定科目の行の摘要を、辞書に登録された摘要にまとめて入れ替えます。")
            _accts_in_use = list(dict.fromkeys(
                [a for a in merged["借方勘定科目"].astype(str) if a in _dict_terms]
                + [a for a in merged["貸方勘定科目"].astype(str) if a in _dict_terms]
            ))
            if not _accts_in_use:
                st.caption("今の仕訳に、辞書に摘要が登録されている科目はありません。")
            else:
                col_da, col_dt, col_dm = st.columns([2, 2, 2])
                dict_account = col_da.selectbox("勘定科目", _accts_in_use, key="dict_account")
                dict_term = col_dt.selectbox("摘要（辞書）", _dict_terms.get(dict_account, []), key="dict_term")
                dict_mode = col_dm.radio("対象の行", ["要確認の行だけ", "その科目のすべての行"], key="dict_mode")
                if st.button("摘要を入れる", key="dict_apply"):
                    updated = merged.copy()
                    mask = (updated["借方勘定科目"].astype(str) == dict_account) | (
                        updated["貸方勘定科目"].astype(str) == dict_account
                    )
                    if dict_mode == "要確認の行だけ":
                        mask &= updated["要確認"].astype(bool)
                    count = int(mask.sum())
                    if count == 0:
                        st.warning("対象の行がありません。")
                    else:
                        updated.loc[mask, "摘要"] = dict_term
                        storage.replace_entries(client, updated)
                        st.session_state["flash"] = f"{count} 件の摘要を「{dict_term}」にしました。"
                        bump_ledger()
                        st.rerun()

    # --- 科目の一括置換（学習機能付き） ---
    with st.expander("🔁 科目の一括置換（次回からの自動適用も学習できます）"):
        st.caption("摘要にキーワードを含む行の勘定科目をまとめて変更します。税区分も新しい科目に合わせて更新されます。")
        col_kw, col_side, col_acct = st.columns([2, 1, 2])
        bulk_keyword = col_kw.text_input("摘要に含まれるキーワード", key="bulk_keyword", placeholder="例: タイムズ")
        bulk_side = col_side.radio("変更する列", ["借方", "貸方"], key="bulk_side",
                                   help="経費の科目は借方、通帳の入金の科目は貸方です")
        bulk_account = col_acct.text_input("変更後の勘定科目", key="bulk_account", placeholder="例: 旅費交通費")
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
                updated = merged.copy()
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
                            keyword, account, side="expense" if bulk_side == "借方" else "income",
                        )
                        if new_desc:
                            storage.add_desc_rule(client, keyword, new_desc)
                        message += " ルールを学習しました（次回の変換から自動適用）。"
                    st.session_state["flash"] = message
                    bump_ledger()
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
            confirm_undo = st.checkbox(f"「{selected_name}」由来の仕訳をすべて削除する", key="undo_file_confirm")
            if st.button("取り込みを取り消す", disabled=not confirm_undo, key="undo_file_btn"):
                deleted = storage.delete_entries_by_source(client, selected_name)
                st.session_state["flash"] = f"「{selected_name}」の仕訳 {deleted} 件を削除しました。"
                bump_ledger()
                st.rerun()


# =====================================================================
# 弥生CSV出力
# =====================================================================


def _entries_from_df(df: pd.DataFrame) -> tuple[list[JournalEntry], str | None]:
    entries: list[JournalEntry] = []
    idx = -1
    try:
        for idx, row in df.iterrows():
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
        return entries, f"{int(idx) + 1} 行目の内容が不正です（日付は YYYY/MM/DD、金額は数値）: {e}"
    return entries, None


def render_export(client: str) -> None:
    T.page_header(NAV_EXPORT, meta="弥生「仕訳データ」取込形式 ・ 27列 ・ Shift-JIS ・ ヘッダなし")
    df = storage.load_entries(client)
    if df.empty:
        st.info("まだ仕訳がありません。「書類の取り込み」からファイルをアップロードしてください。")
        return

    # 期間（年月）で絞り込んで出力できるようにする（経理の月次業務向け）
    months = sorted({str(d)[:7] for d in df["取引日付"] if len(str(d)) >= 7}, reverse=True)
    col_period, col_dest, col_dl = st.columns([1.2, 1.6, 1.2])
    period = col_period.selectbox(
        "出力する期間", ["すべて"] + months, key="output_period", label_visibility="collapsed",
        format_func=lambda m: "すべての期間" if m == "すべて" else f"{m[:4]}年{int(m[5:7])}月",
    )
    col_dest.markdown(
        f'<div style="padding-top:6px">{T.pill("出力先: 弥生会計（デスクトップ）", "info")}</div>',
        unsafe_allow_html=True,
    )
    target_df = df if period == "すべて" else df[df["取引日付"].astype(str).str.startswith(period)]

    entries, csv_error = _entries_from_df(target_df)
    review_left = int(target_df["要確認"].sum()) if not target_df.empty else 0

    if csv_error:
        st.error(csv_error + " — 「仕訳の編集」で修正してください。")
        return
    if not entries:
        st.info("選択した期間に仕訳がありません。")
        return

    csv_suffix = "" if period == "すべて" else "_" + period.replace("/", "")
    with col_dl:
        st.download_button(
            "⬇️ 弥生CSVをダウンロード",
            data=to_yayoi_csv(entries),
            file_name=f"yayoi_{client}{csv_suffix}.csv",
            mime="text/csv",
            use_container_width=True,
            help="弥生会計デスクトップ版の「仕訳データ」インポート形式（Shift-JIS・ヘッダなし）",
        )

    dates = sorted(e.date for e in entries)
    review_html = (
        f"{review_left}件 — 出力前に確認" if review_left else "0件 — 出力できます"
    )
    T.summary_cards(
        [
            ("仕訳件数", f"{len(entries)}<small> 件</small>", ""),
            ("合計金額", f"¥{sum(e.amount for e in entries):,}", ""),
            ("期間", f"{dates[0]:%-m/%-d} 〜 {dates[-1]:%-m/%-d}", ""),
            ("要確認", ("⚠️ " if review_left else "✅ ") + html.escape(review_html), "warn" if review_left else "ok"),
        ]
    )
    if review_left:
        T.notice(f"要確認が {review_left} 件残っています。出力前に「仕訳の編集」で確認してください。")

    rows_html = "".join(
        "<tr>"
        f'<td class="no">{i}</td>'
        f"<td>{e.date:%Y/%m/%d}</td>"
        f"<td>{html.escape(e.debit_account + (f'（{e.debit_sub}）' if e.debit_sub else ''))}</td>"
        f"<td>{html.escape(e.credit_account + (f'（{e.credit_sub}）' if e.credit_sub else ''))}</td>"
        f'<td class="num">{e.amount:,}</td>'
        f'<td class="desc">{html.escape(e.description)}</td>'
        "</tr>"
        for i, e in enumerate(entries, start=1)
    )
    st.markdown(
        '<div class="yc-table-wrap">'
        '<div class="head">出力プレビュー<span>このままブラウザの印刷（Mac: ⌘P / Windows: Ctrl+P）で印刷できます</span></div>'
        '<table class="yc-table"><thead><tr>'
        '<th>No</th><th>日付</th><th>借方科目（補助）</th><th>貸方科目（補助）</th>'
        '<th class="num">金額</th><th>摘要</th></tr></thead>'
        f"<tbody>{rows_html}</tbody></table>"
        '<div class="foot">ダウンロード後は弥生会計の「仕訳データのインポート」から取り込んでください</div>'
        "</div>",
        unsafe_allow_html=True,
    )


# =====================================================================
# 事前登録（マスタ）
# =====================================================================


def render_masters(client: str) -> None:
    _master = storage.list_subaccounts(client)
    _acct_master = storage.list_account_master(client)
    _desc_dict = storage.list_desc_dict(client)
    T.page_header(
        NAV_MASTERS,
        meta=f"{client} ・ 勘定科目 {len(_acct_master)}件 ・ 補助科目 {len(_master)}件 ・ 摘要辞書 {len(_desc_dict)}件",
    )
    st.markdown(
        "クライアント企業ごとの弥生の科目体系を登録しておくと、通帳の摘要や売掛表・請求書の"
        "取引先から、勘定科目・補助科目を**自動で振り分け**られるようになります。"
    )
    if sub_flash := st.session_state.pop("sub_flash", None):
        st.success(sub_flash)

    # 3つのカードに分ける。各タブの中身は下の with ブロックで描画する
    # （タブは作ったカードの中に表示される）
    # 補助科目と勘定科目のマスタは横並び2カラム
    col_sub_master, col_acct_master, col_dict = st.columns(3)
    with col_dict, st.container(border=True):
        T.card_title(
            f"📚 事前登録③：摘要辞書（{len(_desc_dict)} 件登録済み）" if _desc_dict else "📚 事前登録③：摘要辞書（未登録）",
            "書類の内容に辞書の語（駐車料・タクシー代 など）があれば、その会社の流儀の摘要と科目が最初から入ります",
        )
        tab_dict_pdf, tab_dict_list = st.tabs(["📄 PDFから一括登録", "📝 登録内容の確認・編集"])
    with col_sub_master, st.container(border=True):
        T.card_title(
            f"🗂 事前登録①：補助科目マスタ（{len(_master)} 件登録済み）" if _master else "🗂 事前登録①：補助科目マスタ（未登録）",
            "通帳の摘要や売掛表・請求書の取引先から、勘定科目・補助科目を自動で振り分けるための登録",
        )
        tab_pdf_import, tab_master_list = st.tabs(["📄 PDFから一括登録", "📝 登録内容の確認・編集"])
    with col_acct_master, st.container(border=True):
        T.card_title(
            f"📒 事前登録②：勘定科目マスタ（{len(_acct_master)} 件登録済み）" if _acct_master else "📒 事前登録②：勘定科目マスタ（未登録）",
            "仕訳表の科目をプルダウンで選べるようになり、売上・買掛表の既定の科目もここから決まります",
        )
        tab_acct_pdf, tab_acct_list = st.tabs(["📄 PDFから一括登録", "📝 登録内容の確認・編集"])
    with st.container(border=True):
        T.card_title(
            "⚙️ 売掛・買掛の設定",
            "書類タイプの紐付けと、行番号と取引先の対応。上の2つのマスタとは独立して設定できます",
        )
        tab_doctype, tab_rowmap = st.tabs(["🔗 書類タイプの紐付け", "🔢 売掛・買掛の行番号"])

    # --- 摘要辞書: PDFから一括登録 ---
    with tab_dict_pdf:
        st.markdown(
            """
            **手順**
            1. 弥生会計で「摘要辞書（摘要科目一覧）」を **PDF出力** します
            2. そのPDFを下にアップロードします
            3. 読み取り結果を確認して「登録する」を押します
            """
        )
        dict_pdf = st.file_uploader("摘要科目一覧のPDF", type=["pdf"], key="dict_pdf", label_visibility="collapsed")
        if dict_pdf is not None:
            try:
                dict_records = parse_yayoi_desc_dict_pdf(dict_pdf.getvalue())
            except Exception as e:
                dict_records = []
                st.error(f"PDFの読み取りに失敗しました: {e}")
            if not dict_records:
                st.error("摘要辞書を読み取れませんでした。弥生の「摘要科目一覧」のPDFかどうか確認してください。")
            else:
                st.success(f"✅ {len(dict_records)} 件の摘要を読み取りました。内容を確認してください:")
                summary = (
                    pd.DataFrame(dict_records)
                    .groupby("account", sort=False)
                    .agg(件数=("description", "count"), 摘要の例=("description", lambda s: "、".join(s.head(3)) + ("…" if len(s) > 3 else "")))
                    .rename_axis("勘定科目")
                )
                st.dataframe(summary, use_container_width=True, height=300)
                if _desc_dict:
                    st.caption(f"※ 登録すると「{client}」の既存の摘要辞書（{len(_desc_dict)}件）は置き換えられます。")
                if st.button(f"この {len(dict_records)} 件を登録する", type="primary", key="dict_import"):
                    saved = storage.replace_desc_dict(client, dict_records)
                    st.session_state["sub_flash"] = f"✅ {saved} 件の摘要辞書を登録しました。"
                    st.rerun()

    # --- 摘要辞書: 確認・編集 ---
    with tab_dict_list:
        if not _desc_dict:
            st.info("まだ登録されていません。「📄 PDFから一括登録」タブで弥生のPDFをアップするか、下の表に直接入力して「保存」を押してください。")
            dict_view = pd.DataFrame(columns=["摘要", "勘定科目", "サーチキー"])
        else:
            dict_view = pd.DataFrame(_desc_dict)[["description", "account", "search_key"]].rename(
                columns={"description": "摘要", "account": "勘定科目", "search_key": "サーチキー"}
            )
        edited_dict = st.data_editor(
            dict_view, num_rows="dynamic", use_container_width=True, key="dict_editor",
            column_config={
                "摘要": st.column_config.TextColumn(help="弥生の摘要辞書の語（例: 駐車料、タクシー代）"),
                "勘定科目": st.column_config.TextColumn(help="この摘要を使う勘定科目"),
                "サーチキー": st.column_config.TextColumn(help="弥生のサーチキー数字"),
            },
        )
        if st.button("💾 変更を保存", key="dict_save"):
            records = [
                {"description": r["摘要"], "account": r["勘定科目"], "search_key": r["サーチキー"]}
                for _, r in edited_dict.iterrows()
            ]
            saved = storage.replace_desc_dict(client, records)
            st.session_state["sub_flash"] = f"✅ {saved} 件の摘要辞書を保存しました。"
            st.rerun()

    # --- 補助科目: PDFから一括登録 ---
    with tab_pdf_import:
        st.markdown(
            """
            **手順**
            1. 弥生会計で［集計表］→［補助科目一覧表］を **PDF出力** します
            2. そのPDFを下にアップロードします
            3. 読み取り結果を確認して「登録する」を押します
            """
        )
        sub_pdf = st.file_uploader("補助科目一覧表のPDF", type=["pdf"], key="sub_pdf", label_visibility="collapsed")
        if sub_pdf is not None:
            try:
                pdf_records = parse_yayoi_subaccount_pdf(sub_pdf.getvalue())
            except Exception as e:
                pdf_records = []
                st.error(f"PDFの読み取りに失敗しました: {e}")
            if not pdf_records:
                st.error("補助科目を読み取れませんでした。弥生の「補助科目一覧表」のPDFかどうか確認してください。")
            else:
                st.success(f"✅ {len(pdf_records)} 件の補助科目を読み取りました。内容を確認してください:")
                summary = (
                    pd.DataFrame(pdf_records)
                    .groupby("account", sort=False)
                    .agg(件数=("sub_name", "count"), 補助科目の例=("sub_name", lambda s: "、".join(s.head(3)) + ("…" if len(s) > 3 else "")))
                    .rename_axis("勘定科目")
                )
                st.dataframe(summary, use_container_width=True)
                if _master:
                    st.caption(f"※ 登録すると「{client}」の既存のマスタ（{len(_master)}件）は置き換えられます。")
                if st.button(f"この {len(pdf_records)} 件を登録する", type="primary", key="sub_import"):
                    saved = storage.replace_subaccounts(client, pdf_records)
                    st.session_state["sub_flash"] = f"✅ {saved} 件の補助科目を登録しました。"
                    st.rerun()

    # --- 補助科目: 確認・編集 ---
    with tab_master_list:
        if not _master:
            st.info("まだ登録されていません。「📄 補助科目：PDF登録」タブで弥生のPDFをアップするか、下の表に直接入力して「保存」を押してください。")
            master_view = pd.DataFrame(columns=["勘定科目", "補助科目", "サーチキー"])
            account_filter = "すべて"
        else:
            accounts_in_master = list(dict.fromkeys(r["account"] for r in _master))
            account_filter = st.selectbox(
                "表示する勘定科目で絞り込み",
                ["すべて"] + [f"{a}（{sum(1 for r in _master if r['account'] == a)}件）" for a in accounts_in_master],
                key="sub_filter",
            )
            selected_account = None if account_filter == "すべて" else account_filter.rsplit("（", 1)[0]
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
                # 絞り込み表示中は、表示していない科目の登録内容を保持したまま表示分だけを差し替える
                selected_account = account_filter.rsplit("（", 1)[0]
                edited_records = [r for r in _master if r["account"] != selected_account] + edited_records
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
        acct_pdf = st.file_uploader("勘定科目一覧表のPDF", type=["pdf"], key="acct_pdf", label_visibility="collapsed")
        if acct_pdf is not None:
            try:
                acct_records = parse_yayoi_account_pdf(acct_pdf.getvalue())
            except Exception as e:
                acct_records = []
                st.error(f"PDFの読み取りに失敗しました: {e}")
            if not acct_records:
                st.error("勘定科目を読み取れませんでした。弥生の「勘定科目一覧表」のPDFかどうか確認してください。")
            else:
                st.success(f"✅ {len(acct_records)} 件の勘定科目を読み取りました。内容を確認してください:")
                st.dataframe(
                    pd.DataFrame(acct_records).rename(
                        columns={"name": "勘定科目", "search_key": "サーチキー", "side": "貸借", "tax_class": "税区分"}
                    ),
                    use_container_width=True, height=300,
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
            st.info("まだ登録されていません。「📒 勘定科目：PDF登録」タブで弥生のPDFをアップするか、下の表に直接入力して「保存」を押してください。")
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
                {"name": r["勘定科目"], "search_key": r["サーチキー"], "side": r["貸借"], "tax_class": r["税区分"]}
                for _, r in edited_accts.iterrows()
            ]
            saved = storage.replace_account_master(client, records)
            st.session_state["sub_flash"] = f"✅ {saved} 件の勘定科目を保存しました。"
            st.rerun()

    # --- 書類タイプ→科目の紐付け ---
    with tab_doctype:
        st.markdown(
            "売上（売掛表）・請求書・買掛表を仕訳にするときの**借方・貸方の勘定科目**を、書類タイプごとに設定します。"
            "取引先名は補助科目に入ります。未設定でも、勘定科目マスタから既定の科目"
            "（完成工事未収入金・工事未払金 等）を自動で選びます。"
        )
        _acct_names = [r["name"] for r in _acct_master]
        _doctype_inputs: dict[str, tuple[str, str, str]] = {}
        for dt in ("売上", "売上請求書", "仕入請求書", "買掛表"):
            current = storage.get_doctype_rule(client, dt) or default_doctype_rule(dt, _acct_names)
            col_dt, col_debit, col_credit = st.columns([1, 2, 2])
            col_dt.markdown(f"**{dt}**")
            _options = list(dict.fromkeys([current["debit_account"], current["credit_account"]] + _acct_names))
            debit = col_debit.selectbox("借方科目", _options, index=_options.index(current["debit_account"]), key=f"doctype_debit_{dt}")
            credit = col_credit.selectbox("貸方科目", _options, index=_options.index(current["credit_account"]), key=f"doctype_credit_{dt}")
            _doctype_inputs[dt] = (debit, credit, current["sub_side"])
        st.caption("補助科目（取引先名）は、売上側は借方（売掛・未収系）、仕入側は貸方（買掛・未払系）に自動で入ります。")
        if st.button("💾 紐付けを保存", key="doctype_save"):
            for dt, (debit, credit, sub_side) in _doctype_inputs.items():
                storage.set_doctype_rule(client, dt, debit, credit, sub_side)
            st.session_state["sub_flash"] = "✅ 書類タイプの紐付けを保存しました。"
            st.rerun()

    # --- 売掛表・買掛表の行番号→取引先 ---
    with tab_rowmap:
        st.markdown(
            "売掛表・買掛表が**「行番号と金額だけ」**の形式のとき、行番号を取引先名（補助科目）に変換するための対応表です。"
            "取引先名の列がある表では登録不要です。"
        )
        rowmap_choice = st.radio("対象の表", ["売掛表（売上）", "買掛表"], horizontal=True, key="rowmap_side")
        rowmap_side = "sales" if rowmap_choice.startswith("売掛") else "purchase"
        _rowmap = storage.list_partner_rows(client, rowmap_side)
        rowmap_view = (
            pd.DataFrame(_rowmap)[["row_no", "partner_name"]].rename(columns={"row_no": "行番号", "partner_name": "取引先名"})
            if _rowmap else pd.DataFrame(columns=["行番号", "取引先名"])
        )
        edited_rowmap = st.data_editor(
            rowmap_view, num_rows="dynamic", use_container_width=True, key=f"rowmap_editor_{rowmap_side}",
            column_config={
                "行番号": st.column_config.NumberColumn(min_value=1, step=1),
                "取引先名": st.column_config.TextColumn(help="補助科目に入る取引先名。弥生の補助科目名と合わせてください"),
            },
        )
        if st.button("💾 対応表を保存", key="rowmap_save"):
            records = [
                {"row_no": r["行番号"], "partner_name": r["取引先名"]}
                for _, r in edited_rowmap.iterrows() if pd.notna(r["行番号"])
            ]
            saved = storage.replace_partner_rows(client, rowmap_side, records)
            st.session_state["sub_flash"] = f"✅ {rowmap_choice}の対応表 {saved} 件を保存しました。"
            st.rerun()


# =====================================================================
# 学習ルール
# =====================================================================


def render_rules(client: str) -> None:
    learned_rules = storage.list_account_rules()
    learned_descs = storage.list_desc_rules(client)
    T.page_header(NAV_RULES, meta=f"科目ルール {len(learned_rules)}件 ・ 摘要ルール {len(learned_descs)}件")
    st.markdown(
        "仕訳表の直接編集や一括置換から学習したルールです。変換のたびに自動で適用されます。"
        "科目ルールは全クライアント共通、摘要ルールはクライアントごとです。"
    )

    col_a, col_d = st.columns(2)
    with col_a, st.container(border=True):
        T.card_title("🧠 科目ルール", "摘要にキーワードを含む仕訳の勘定科目を自動で決めます")
        if not learned_rules:
            st.caption("まだありません。「仕訳の編集」で科目を書き換えると学習します。")
        for rule in learned_rules:
            c1, c2, c3 = st.columns([3, 2, 1])
            c1.write(f"摘要に「{rule['keyword']}」")
            side_label = "借方" if rule["side"] == "expense" else "貸方"
            c2.write(f"→ {side_label}: {rule['account']}")
            if c3.button("削除", key=f"rule_del_{rule['id']}"):
                storage.delete_account_rule(rule["id"])
                st.rerun()
    with col_d, st.container(border=True):
        T.card_title(f"🧠 摘要ルール（{client}）", "摘要を会社の流儀（例: セブンイレブン→飲食代）に書き換えます")
        if not learned_descs:
            st.caption("まだありません。「仕訳の編集」で摘要を書き換えると学習します。")
        for rule in learned_descs:
            c1, c2, c3 = st.columns([3, 2, 1])
            c1.write(f"摘要に「{rule['keyword']}」")
            c2.write(f"→ 「{rule['description']}」")
            if c3.button("削除", key=f"desc_del_{rule['id']}"):
                storage.delete_desc_rule(rule["id"])
                st.rerun()
