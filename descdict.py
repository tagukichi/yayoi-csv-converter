"""摘要辞書（弥生の摘要科目一覧）と仕訳の突合。

摘要辞書は「摘要 → 勘定科目」のクライアント別の対応表（例: 駐車料→旅費交通費、
飲食代→交際費/会議費/福利厚生費、川崎幸ロータリークラブ→研修費）。
OCRで読んだ書類の本文に辞書の語（または「代・料・費」を除いた語幹）が
含まれていれば、その会社の流儀で摘要（と、確度が高ければ勘定科目）を決める。

適用順: 組み込みルールで科目を推定 → 補助科目マスタと突合 → 摘要辞書（ここ）
→ 学習済みの摘要ルール（一括置換・直接編集で覚えたもの。辞書より優先）。
"""

from __future__ import annotations

import re
import unicodedata

from accounts import UNKNOWN_EXPENSE, yayoi_tax
from models import JournalEntry

# 語幹を作るときに落とす接尾語（長いものから試す）
_SUFFIXES = ("購読料", "購入料", "利用税", "料金", "費用", "手当", "代", "料", "費", "税")

# 語幹にしても意味が広すぎて誤反応しやすい語（辞書語そのものの一致のみ使う）
_STEM_BLOCKLIST = {"交通", "食事", "飲食", "月分", "当月", "社長", "地代", "土産", "作業"}


def _norm(text: str) -> str:
    """全半角の統一・空白と記号の除去・小文字化（突合用）。"""
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = re.sub(r"[\s・.,、。()（）\-‐−/／]", "", s)
    return s.lower()


def _stem(term_norm: str) -> str:
    for suf in _SUFFIXES:
        if term_norm.endswith(suf) and len(term_norm) > len(suf):
            return term_norm[: -len(suf)]
    return ""


def _side_for_account(account: str) -> str | None:
    """辞書の科目を仕訳のどちら側に入れるか。費用→借方、収益→貸方、BS科目→None。"""
    tax = yayoi_tax(account)
    if tax == "対象外":
        return None
    return "credit" if "売上" in tax else "debit"


def apply_desc_dictionary(
    entries: list[JournalEntry], records: list[dict], context_text: str = ""
) -> int:
    """摘要辞書を仕訳に適用する。書き換えた件数を返す。

    - 仕訳の摘要（と、あれば書類の本文 context_text）に辞書の語が含まれるかを見る
    - 辞書の科目が仕訳の科目と同じなら、摘要を辞書の語に揃える
    - 科目が違っても、辞書の語が本文にそのまま含まれ、かつその語が辞書で
      1つの科目にしか登録されていなければ（例: 川崎幸ロータリークラブ→研修費）、
      科目も辞書に合わせて要確認を外す
    """
    if not records or not entries:
        return 0

    cleaned: list[tuple[str, str, str]] = []  # (term_norm, term, account)
    for r in records:
        term = str(r.get("description", "") or "").strip()
        account = str(r.get("account", "") or "").strip()
        term_norm = _norm(term)
        if len(term_norm) < 2 or not account:
            continue
        cleaned.append((term_norm, term, account))
    # 製造原価科目（[製]〜）は販管費の同名科目と対で登録されていることが多い。
    # 領収書等の仕訳は販管費側に寄せたいので、同じ摘要に販管費側の科目が
    # あれば [製] 側は候補から外す（一意判定の邪魔にもなるため）
    non_mfg_terms = {t for t, _term, a in cleaned if not a.startswith("[製]")}
    prepared: list[tuple[str, str, str, str]] = []  # (term_norm, stem_norm, term, account)
    accounts_by_term: dict[str, set[str]] = {}
    for term_norm, term, account in cleaned:
        if account.startswith("[製]") and term_norm in non_mfg_terms:
            continue
        stem = _stem(term_norm)
        if stem in _STEM_BLOCKLIST or len(stem) < 2:
            stem = ""
        prepared.append((term_norm, stem, term, account))
        accounts_by_term.setdefault(term_norm, set()).add(account)

    ctx_norm = _norm(context_text)
    applied = 0
    for e in entries:
        hay = _norm(e.description) + "\n" + ctx_norm
        best: tuple[int, bool, str, str, bool] | None = None
        for term_norm, stem, term, account in prepared:
            if term_norm in hay:
                hit, full = len(term_norm), True
            elif stem and stem in hay:
                hit, full = len(stem), False
            else:
                continue
            same_account = account in (e.debit_account, e.credit_account)
            score = hit * 10 + (5 if same_account else 0)
            if best is None or score > best[0]:
                best = (score, same_account, term, account, full)
        if best is None:
            continue
        _score, same_account, term, account, full = best
        term_norm = _norm(term)
        if same_account:
            if e.description != term:
                e.description = term
                applied += 1
            continue
        # 科目が違う: 辞書の語がそのまま本文にあり、科目が一意に決まる場合のみ採用
        if not full or len(accounts_by_term.get(term_norm, ())) != 1:
            continue
        side = _side_for_account(account)
        if side is None:
            continue
        if side == "debit":
            # 借方が費用科目（推定値）のときだけ差し替える。BS科目（預り金等）は触らない
            if yayoi_tax(e.debit_account) == "対象外" and e.debit_account != UNKNOWN_EXPENSE:
                continue
            e.debit_account = account
            e.debit_tax = yayoi_tax(account)
        else:
            if yayoi_tax(e.credit_account) == "対象外" and not e.needs_review:
                continue
            e.credit_account = account
            e.credit_tax = yayoi_tax(account)
        e.description = term
        e.needs_review = False
        applied += 1
    return applied


def dict_terms_by_account(records: list[dict]) -> dict[str, list[str]]:
    """勘定科目 → 辞書の摘要一覧（登録順）。画面のプルダウン用。"""
    result: dict[str, list[str]] = {}
    for r in records:
        result.setdefault(r["account"], [])
        if r["description"] not in result[r["account"]]:
            result[r["account"]].append(r["description"])
    return result
