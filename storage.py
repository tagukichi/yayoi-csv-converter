"""仕訳データの永続化（Supabase / SQLite の二段構え）。

.env に SUPABASE_URL と SUPABASE_KEY があれば Supabase(Postgres) を使い、
なければローカルの SQLite にフォールバックする。テーブル構造は両者で同一
（supabase_schema.sql 参照）。テストは db_path を明示指定するため常に SQLite。

SQLite の DB ファイルは data/ 配下に置き、リポジトリにはコミットしない
（.gitignore 済み）。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pandas as pd

from models import JournalEntry

DB_PATH = Path(__file__).resolve().parent / "data" / "journal.db"

# 初回起動時に登録する企業（後から画面で追加・削除できる）
DEFAULT_CLIENTS = ["A建設", "B工務店", "C社"]

# data_editor での表示順・編集対象の列。DB の列と一対一。
EDITABLE_COLUMNS = [
    "取引日付", "借方勘定科目", "借方補助科目", "借方税区分",
    "貸方勘定科目", "貸方補助科目", "貸方税区分",
    "金額", "摘要", "要確認", "出典ファイル",
]

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    date TEXT NOT NULL,
    debit_account TEXT NOT NULL,
    debit_sub TEXT NOT NULL DEFAULT '',
    debit_tax TEXT NOT NULL DEFAULT '対象外',
    credit_account TEXT NOT NULL,
    credit_sub TEXT NOT NULL DEFAULT '',
    credit_tax TEXT NOT NULL DEFAULT '対象外',
    amount INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    needs_review INTEGER NOT NULL DEFAULT 0,
    source_file TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
)
"""


_CREATE_CLIENTS_SQL = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
)
"""

# クライアント別の補助科目マスタ（弥生の補助科目一覧表から取り込む「事前登録」）
_CREATE_SUBACCOUNTS_SQL = """
CREATE TABLE IF NOT EXISTS subaccounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    account TEXT NOT NULL,
    sub_name TEXT NOT NULL,
    search_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (client, account, sub_name)
)
"""

# 摘要の書き換えルール（クライアント別）。摘要にキーワードを含む仕訳の
# 摘要を description に置き換える。「セブンイレブン→飲食代」のような
# 会社ごとの摘要の流儀を学習する
_CREATE_DESC_RULES_SQL = """
CREATE TABLE IF NOT EXISTS desc_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    keyword TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (client, keyword)
)
"""

# クライアント別の勘定科目マスタ（弥生の勘定科目一覧表から取り込む「事前登録」）
_CREATE_ACCOUNT_MASTER_SQL = """
CREATE TABLE IF NOT EXISTS account_master (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    name TEXT NOT NULL,
    search_key TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL DEFAULT '借方',
    tax_class TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (client, name)
)
"""

# 書類タイプ→勘定科目の紐付け（クライアント別）。売上（売掛表）・請求書・
# 買掛表の仕訳で使う借方/貸方科目と、取引先を補助科目に入れる側を保存する。
# sub_side: debit=借方に取引先の補助科目, credit=貸方に
_CREATE_DOCTYPE_RULES_SQL = """
CREATE TABLE IF NOT EXISTS doctype_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    debit_account TEXT NOT NULL DEFAULT '',
    credit_account TEXT NOT NULL DEFAULT '',
    sub_side TEXT NOT NULL DEFAULT 'debit',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (client, doc_type)
)
"""

# 売掛表・買掛表の「行番号 → 取引先名」の対応（クライアント別）。
# side: sales=売掛表（売上）, purchase=買掛表
_CREATE_PARTNER_ROWS_SQL = """
CREATE TABLE IF NOT EXISTS partner_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    side TEXT NOT NULL DEFAULT 'sales',
    row_no INTEGER NOT NULL,
    partner_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (client, side, row_no)
)
"""

# クライアント別の摘要辞書（弥生の摘要科目一覧から取り込む「事前登録」）。
# 摘要 → 勘定科目 の対応。同じ摘要が複数の科目に登録されることもある
# （「飲食代」が福利厚生費・交際費・会議費 など）
_CREATE_DESC_DICT_SQL = """
CREATE TABLE IF NOT EXISTS desc_dict (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    description TEXT NOT NULL,
    account TEXT NOT NULL,
    search_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (client, description, account)
)
"""

# 一括置換から学習した「摘要キーワード → 勘定科目」ルール。
# side: expense=借方（費用）, income=貸方（収益）
_CREATE_RULES_SQL = """
CREATE TABLE IF NOT EXISTS account_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL,
    account TEXT NOT NULL,
    side TEXT NOT NULL DEFAULT 'expense',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (keyword, side)
)
"""

# --- Supabase バックエンド ---

_sb_client = None


def _supabase_enabled(db_path: Path) -> bool:
    """Supabase を使うか。テスト等で db_path が明示された場合は常に SQLite。"""
    if db_path is not DB_PATH:
        return False
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_KEY"))


def backend_name() -> str:
    """画面表示用: 現在使っているDBの名前。"""
    return "Supabase" if _supabase_enabled(DB_PATH) else "ローカル (SQLite)"


def _sb():
    """Supabase クライアント（遅延生成のシングルトン）。"""
    global _sb_client
    if _sb_client is None:
        from supabase import create_client

        _sb_client = create_client(
            os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"]
        )
    return _sb_client


def _entry_to_record(client: str, e: JournalEntry, source_file: str) -> dict:
    return {
        "client": client,
        "date": e.date.strftime("%Y/%m/%d"),
        "debit_account": e.debit_account,
        "debit_sub": e.debit_sub,
        "debit_tax": e.debit_tax,
        "credit_account": e.credit_account,
        "credit_sub": e.credit_sub,
        "credit_tax": e.credit_tax,
        "amount": e.amount,
        "description": e.description,
        "needs_review": bool(e.needs_review),
        "source_file": source_file,
    }


def _row_to_record(client: str, r: pd.Series) -> dict:
    return {
        "client": client,
        "date": str(r["取引日付"]).strip(),
        "debit_account": str(r["借方勘定科目"]).strip(),
        "debit_sub": str(r.get("借方補助科目", "") or "").strip(),
        "debit_tax": str(r["借方税区分"]).strip() or "対象外",
        "credit_account": str(r["貸方勘定科目"]).strip(),
        "credit_sub": str(r.get("貸方補助科目", "") or "").strip(),
        "credit_tax": str(r["貸方税区分"]).strip() or "対象外",
        "amount": int(r["金額"]),
        "description": str(r["摘要"]).strip(),
        "needs_review": bool(r["要確認"]),
        "source_file": str(r["出典ファイル"]).strip(),
    }


_JP_COLUMNS = {
    "date": "取引日付",
    "debit_account": "借方勘定科目",
    "debit_sub": "借方補助科目",
    "debit_tax": "借方税区分",
    "credit_account": "貸方勘定科目",
    "credit_sub": "貸方補助科目",
    "credit_tax": "貸方税区分",
    "amount": "金額",
    "description": "摘要",
    "needs_review": "要確認",
    "source_file": "出典ファイル",
}


def _records_to_df(records: list[dict]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(columns=list(_JP_COLUMNS.values()))
    df = pd.DataFrame(records)[list(_JP_COLUMNS.keys())].rename(columns=_JP_COLUMNS)
    df["要確認"] = df["要確認"].astype(bool)
    return df


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(_CREATE_SQL)
    conn.execute(_CREATE_CLIENTS_SQL)
    conn.execute(_CREATE_RULES_SQL)
    conn.execute(_CREATE_DESC_RULES_SQL)
    conn.execute(_CREATE_SUBACCOUNTS_SQL)
    conn.execute(_CREATE_ACCOUNT_MASTER_SQL)
    conn.execute(_CREATE_DOCTYPE_RULES_SQL)
    conn.execute(_CREATE_PARTNER_ROWS_SQL)
    conn.execute(_CREATE_DESC_DICT_SQL)
    # 既存DBへの補助科目列の追加（後方互換のためのマイグレーション）
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(entries)")}
    if "debit_sub" not in existing_cols:
        conn.execute("ALTER TABLE entries ADD COLUMN debit_sub TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE entries ADD COLUMN credit_sub TEXT NOT NULL DEFAULT ''")
        conn.commit()
    # 初回のみ既定の企業を登録する（user_version を「初期化済み」フラグに使う。
    # 全企業を削除しても勝手に復活しないようにするため）
    if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
        conn.executemany(
            "INSERT OR IGNORE INTO clients (name) VALUES (?)",
            [(name,) for name in DEFAULT_CLIENTS],
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    return conn


def list_clients(db_path: Path = DB_PATH) -> list[str]:
    """登録済みの企業名一覧を返す。"""
    if _supabase_enabled(db_path):
        res = _sb().table("clients").select("name").order("name").execute()
        return [r["name"] for r in res.data]
    with _connect(db_path) as conn:
        return [r[0] for r in conn.execute("SELECT name FROM clients ORDER BY name")]


def add_client(name: str, db_path: Path = DB_PATH) -> bool:
    """企業を追加する。空文字・重複は False を返す。"""
    name = name.strip()
    if not name:
        return False
    if _supabase_enabled(db_path):
        try:
            _sb().table("clients").insert({"name": name}).execute()
        except Exception:  # 重複（unique違反）など
            return False
        return True
    with _connect(db_path) as conn:
        try:
            conn.execute("INSERT INTO clients (name) VALUES (?)", (name,))
        except sqlite3.IntegrityError:
            return False
    return True


def delete_client(name: str, db_path: Path = DB_PATH) -> None:
    """企業を削除する。その企業の仕訳もまとめて削除する。"""
    if _supabase_enabled(db_path):
        _sb().table("entries").delete().eq("client", name).execute()
        _sb().table("clients").delete().eq("name", name).execute()
        return
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM entries WHERE client = ?", (name,))
        conn.execute("DELETE FROM clients WHERE name = ?", (name,))


def add_entries(
    client: str,
    entries: list[JournalEntry],
    source_file: str = "",
    db_path: Path = DB_PATH,
) -> int:
    """解析結果の仕訳をクライアントの台帳に追記する。追加件数を返す。"""
    if not entries:
        return 0
    if _supabase_enabled(db_path):
        _sb().table("entries").insert(
            [_entry_to_record(client, e, source_file) for e in entries]
        ).execute()
        return len(entries)
    with _connect(db_path) as conn:
        conn.executemany(
            """INSERT INTO entries
               (client, date, debit_account, debit_sub, debit_tax,
                credit_account, credit_sub, credit_tax, amount, description,
                needs_review, source_file)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    client,
                    e.date.strftime("%Y/%m/%d"),
                    e.debit_account,
                    e.debit_sub,
                    e.debit_tax,
                    e.credit_account,
                    e.credit_sub,
                    e.credit_tax,
                    e.amount,
                    e.description,
                    int(e.needs_review),
                    source_file,
                )
                for e in entries
            ],
        )
    return len(entries)


def load_entries(client: str, db_path: Path = DB_PATH) -> pd.DataFrame:
    """クライアントの仕訳一覧を data_editor 用の DataFrame で返す。"""
    if _supabase_enabled(db_path):
        res = (
            _sb().table("entries").select("*")
            .eq("client", client).order("date").order("id").execute()
        )
        return _records_to_df(res.data)
    with _connect(db_path) as conn:
        df = pd.read_sql_query(
            """SELECT date AS 取引日付,
                      debit_account AS 借方勘定科目,
                      debit_sub AS 借方補助科目,
                      debit_tax AS 借方税区分,
                      credit_account AS 貸方勘定科目,
                      credit_sub AS 貸方補助科目,
                      credit_tax AS 貸方税区分,
                      amount AS 金額,
                      description AS 摘要,
                      needs_review AS 要確認,
                      source_file AS 出典ファイル
               FROM entries WHERE client = ? ORDER BY date, id""",
            conn,
            params=(client,),
        )
    df["要確認"] = df["要確認"].astype(bool)
    return df


def replace_entries(client: str, df: pd.DataFrame, db_path: Path = DB_PATH) -> int:
    """クライアントの台帳を編集後の DataFrame の内容で置き換える。

    data_editor 上での修正・行追加・行削除をまとめて反映するための操作。
    保存件数を返す。
    """
    if _supabase_enabled(db_path):
        records = [_row_to_record(client, r) for _, r in df.iterrows()]
        _sb().table("entries").delete().eq("client", client).execute()
        if records:
            _sb().table("entries").insert(records).execute()
        return len(records)
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM entries WHERE client = ?", (client,))
        rows = [
            (
                client,
                str(r["取引日付"]).strip(),
                str(r["借方勘定科目"]).strip(),
                str(r.get("借方補助科目", "") or "").strip(),
                str(r["借方税区分"]).strip() or "対象外",
                str(r["貸方勘定科目"]).strip(),
                str(r.get("貸方補助科目", "") or "").strip(),
                str(r["貸方税区分"]).strip() or "対象外",
                int(r["金額"]),
                str(r["摘要"]).strip(),
                int(bool(r["要確認"])),
                str(r["出典ファイル"]).strip(),
            )
            for _, r in df.iterrows()
        ]
        conn.executemany(
            """INSERT INTO entries
               (client, date, debit_account, debit_sub, debit_tax,
                credit_account, credit_sub, credit_tax, amount, description,
                needs_review, source_file)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
    return len(rows)


def list_source_files(client: str, db_path: Path = DB_PATH) -> list[tuple[str, int]]:
    """クライアントの台帳にある出典ファイル名と件数を返す（新しい順）。"""
    if _supabase_enabled(db_path):
        res = (
            _sb().table("entries").select("source_file")
            .eq("client", client).neq("source_file", "").order("id", desc=True).execute()
        )
        counts: dict[str, int] = {}
        for r in res.data:
            counts[r["source_file"]] = counts.get(r["source_file"], 0) + 1
        return list(counts.items())
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT source_file, COUNT(*) FROM entries
               WHERE client = ? AND source_file != ''
               GROUP BY source_file ORDER BY MAX(id) DESC""",
            (client,),
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def delete_entries_by_source(client: str, source_file: str, db_path: Path = DB_PATH) -> int:
    """指定した出典ファイル由来の仕訳をまとめて削除する。削除件数を返す。"""
    if _supabase_enabled(db_path):
        res = (
            _sb().table("entries").delete()
            .eq("client", client).eq("source_file", source_file).execute()
        )
        return len(res.data or [])
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM entries WHERE client = ? AND source_file = ?",
            (client, source_file),
        )
        return cur.rowcount


def list_subaccounts(
    client: str, account: str | None = None, db_path: Path = DB_PATH
) -> list[dict]:
    """クライアントの補助科目マスタを返す。account 指定でその科目に絞る。"""
    if _supabase_enabled(db_path):
        q = _sb().table("subaccounts").select("*").eq("client", client)
        if account:
            q = q.eq("account", account)
        return q.order("id").execute().data
    with _connect(db_path) as conn:
        sql = "SELECT id, account, sub_name, search_key FROM subaccounts WHERE client = ?"
        params: list = [client]
        if account:
            sql += " AND account = ?"
            params.append(account)
        rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    return [
        {"id": r[0], "account": r[1], "sub_name": r[2], "search_key": r[3]}
        for r in rows
    ]


def replace_subaccounts(client: str, records: list[dict], db_path: Path = DB_PATH) -> int:
    """クライアントの補助科目マスタを一括で置き換える。登録件数を返す。"""
    seen: set[tuple[str, str]] = set()
    cleaned = []
    for r in records:
        account = str(r.get("account", "")).strip()
        sub_name = str(r.get("sub_name", "")).strip()
        if not account or not sub_name or (account, sub_name) in seen:
            continue
        seen.add((account, sub_name))
        cleaned.append(
            {
                "client": client,
                "account": account,
                "sub_name": sub_name,
                "search_key": str(r.get("search_key", "") or "").strip().lower(),
            }
        )
    if _supabase_enabled(db_path):
        _sb().table("subaccounts").delete().eq("client", client).execute()
        if cleaned:
            _sb().table("subaccounts").insert(cleaned).execute()
        return len(cleaned)
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM subaccounts WHERE client = ?", (client,))
        conn.executemany(
            "INSERT INTO subaccounts (client, account, sub_name, search_key) VALUES (?, ?, ?, ?)",
            [(c["client"], c["account"], c["sub_name"], c["search_key"]) for c in cleaned],
        )
    return len(cleaned)


def add_subaccount(
    client: str, account: str, sub_name: str, search_key: str = "", db_path: Path = DB_PATH
) -> bool:
    """補助科目を1件追記する（既存マスタは残す）。登録済みなら False。

    売掛表・請求書に出てきた新しい取引先を「仕分けマスター」へ自動登録する
    ときに使う（replace_subaccounts は全置き換えなので使えない）。
    """
    account, sub_name = account.strip(), sub_name.strip()
    if not account or not sub_name:
        return False
    if _supabase_enabled(db_path):
        existing = (
            _sb().table("subaccounts").select("id").eq("client", client)
            .eq("account", account).eq("sub_name", sub_name).execute().data
        )
        if existing:
            return False
        _sb().table("subaccounts").insert(
            {
                "client": client,
                "account": account,
                "sub_name": sub_name,
                "search_key": search_key.strip().lower(),
            }
        ).execute()
        return True
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO subaccounts (client, account, sub_name, search_key)
               VALUES (?, ?, ?, ?)""",
            (client, account, sub_name, search_key.strip().lower()),
        )
        return cur.rowcount > 0


# --- 勘定科目マスタ（クライアント別・事前登録） ---


def list_account_master(client: str, db_path: Path = DB_PATH) -> list[dict]:
    """クライアントの勘定科目マスタを返す（登録順）。"""
    if _supabase_enabled(db_path):
        return (
            _sb().table("account_master").select("*")
            .eq("client", client).order("id").execute().data
        )
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT id, name, search_key, side, tax_class
               FROM account_master WHERE client = ? ORDER BY id""",
            (client,),
        ).fetchall()
    return [
        {"id": r[0], "name": r[1], "search_key": r[2], "side": r[3], "tax_class": r[4]}
        for r in rows
    ]


def replace_account_master(client: str, records: list[dict], db_path: Path = DB_PATH) -> int:
    """クライアントの勘定科目マスタを一括で置き換える。登録件数を返す。"""
    seen: set[str] = set()
    cleaned = []
    for r in records:
        name = str(r.get("name", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append(
            {
                "client": client,
                "name": name,
                "search_key": str(r.get("search_key", "") or "").strip(),
                "side": str(r.get("side", "") or "借方").strip() or "借方",
                "tax_class": str(r.get("tax_class", "") or "").strip(),
            }
        )
    if _supabase_enabled(db_path):
        _sb().table("account_master").delete().eq("client", client).execute()
        if cleaned:
            _sb().table("account_master").insert(cleaned).execute()
        return len(cleaned)
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM account_master WHERE client = ?", (client,))
        conn.executemany(
            """INSERT INTO account_master (client, name, search_key, side, tax_class)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (c["client"], c["name"], c["search_key"], c["side"], c["tax_class"])
                for c in cleaned
            ],
        )
    return len(cleaned)


# --- 摘要辞書（クライアント別・事前登録） ---


def list_desc_dict(client: str, db_path: Path = DB_PATH) -> list[dict]:
    """クライアントの摘要辞書（摘要→勘定科目）を返す（登録順）。"""
    if _supabase_enabled(db_path):
        return (
            _sb().table("desc_dict").select("*")
            .eq("client", client).order("id").execute().data
        )
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT id, description, account, search_key
               FROM desc_dict WHERE client = ? ORDER BY id""",
            (client,),
        ).fetchall()
    return [
        {"id": r[0], "description": r[1], "account": r[2], "search_key": r[3]}
        for r in rows
    ]


def replace_desc_dict(client: str, records: list[dict], db_path: Path = DB_PATH) -> int:
    """クライアントの摘要辞書を一括で置き換える。登録件数を返す。"""
    seen: set[tuple[str, str]] = set()
    cleaned = []
    for r in records:
        description = str(r.get("description", "") or "").strip()
        account = str(r.get("account", "") or "").strip()
        if not description or not account or (description, account) in seen:
            continue
        seen.add((description, account))
        cleaned.append(
            {
                "client": client,
                "description": description,
                "account": account,
                "search_key": str(r.get("search_key", "") or "").strip(),
            }
        )
    if _supabase_enabled(db_path):
        _sb().table("desc_dict").delete().eq("client", client).execute()
        if cleaned:
            _sb().table("desc_dict").insert(cleaned).execute()
        return len(cleaned)
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM desc_dict WHERE client = ?", (client,))
        conn.executemany(
            """INSERT INTO desc_dict (client, description, account, search_key)
               VALUES (?, ?, ?, ?)""",
            [(c["client"], c["description"], c["account"], c["search_key"]) for c in cleaned],
        )
    return len(cleaned)


# --- 書類タイプ→勘定科目の紐付け（クライアント別） ---


def get_doctype_rule(client: str, doc_type: str, db_path: Path = DB_PATH) -> dict | None:
    """書類タイプに紐付けた勘定科目の設定を返す。未設定なら None。"""
    if _supabase_enabled(db_path):
        rows = (
            _sb().table("doctype_rules").select("*")
            .eq("client", client).eq("doc_type", doc_type).execute().data
        )
        return rows[0] if rows else None
    with _connect(db_path) as conn:
        r = conn.execute(
            """SELECT debit_account, credit_account, sub_side FROM doctype_rules
               WHERE client = ? AND doc_type = ?""",
            (client, doc_type),
        ).fetchone()
    if r is None:
        return None
    return {"debit_account": r[0], "credit_account": r[1], "sub_side": r[2]}


def set_doctype_rule(
    client: str,
    doc_type: str,
    debit_account: str,
    credit_account: str,
    sub_side: str = "debit",
    db_path: Path = DB_PATH,
) -> None:
    """書類タイプ→勘定科目の紐付けを保存する（同タイプは上書き）。"""
    record = {
        "client": client,
        "doc_type": doc_type,
        "debit_account": debit_account.strip(),
        "credit_account": credit_account.strip(),
        "sub_side": sub_side if sub_side in ("debit", "credit") else "debit",
    }
    if _supabase_enabled(db_path):
        _sb().table("doctype_rules").upsert(
            record, on_conflict="client,doc_type"
        ).execute()
        return
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO doctype_rules (client, doc_type, debit_account, credit_account, sub_side)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (client, doc_type) DO UPDATE SET
                 debit_account = excluded.debit_account,
                 credit_account = excluded.credit_account,
                 sub_side = excluded.sub_side""",
            (record["client"], record["doc_type"], record["debit_account"],
             record["credit_account"], record["sub_side"]),
        )


# --- 売掛表・買掛表の行番号→取引先の対応（クライアント別） ---


def list_partner_rows(client: str, side: str = "sales", db_path: Path = DB_PATH) -> list[dict]:
    """行番号→取引先名の対応表を返す（行番号順）。"""
    if _supabase_enabled(db_path):
        return (
            _sb().table("partner_rows").select("*")
            .eq("client", client).eq("side", side).order("row_no").execute().data
        )
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT id, row_no, partner_name FROM partner_rows
               WHERE client = ? AND side = ? ORDER BY row_no""",
            (client, side),
        ).fetchall()
    return [{"id": r[0], "row_no": r[1], "partner_name": r[2]} for r in rows]


def replace_partner_rows(
    client: str, side: str, records: list[dict], db_path: Path = DB_PATH
) -> int:
    """行番号→取引先名の対応表を一括で置き換える。登録件数を返す。"""
    seen: set[int] = set()
    cleaned = []
    for r in records:
        try:
            row_no = int(r.get("row_no"))
        except (TypeError, ValueError):
            continue
        partner = str(r.get("partner_name", "") or "").strip()
        if not partner or row_no in seen:
            continue
        seen.add(row_no)
        cleaned.append(
            {"client": client, "side": side, "row_no": row_no, "partner_name": partner}
        )
    if _supabase_enabled(db_path):
        _sb().table("partner_rows").delete().eq("client", client).eq("side", side).execute()
        if cleaned:
            _sb().table("partner_rows").insert(cleaned).execute()
        return len(cleaned)
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM partner_rows WHERE client = ? AND side = ?", (client, side)
        )
        conn.executemany(
            """INSERT INTO partner_rows (client, side, row_no, partner_name)
               VALUES (?, ?, ?, ?)""",
            [(c["client"], c["side"], c["row_no"], c["partner_name"]) for c in cleaned],
        )
    return len(cleaned)


def list_desc_rules(client: str, db_path: Path = DB_PATH) -> list[dict]:
    """クライアントの摘要書き換えルール一覧を返す（キーワードの長い順）。"""
    if _supabase_enabled(db_path):
        rows = (
            _sb().table("desc_rules").select("*").eq("client", client)
            .order("id", desc=True).execute().data
        )
    else:
        with _connect(db_path) as conn:
            fetched = conn.execute(
                "SELECT id, keyword, description FROM desc_rules WHERE client = ? ORDER BY id DESC",
                (client,),
            ).fetchall()
        rows = [{"id": r[0], "keyword": r[1], "description": r[2]} for r in fetched]
    # 「セブン-イレブン川崎店」より「セブン-イレブン」のような短い一般則が
    # 先に食わないよう、長いキーワードを優先する
    return sorted(rows, key=lambda r: len(r["keyword"]), reverse=True)


def add_desc_rule(client: str, keyword: str, description: str, db_path: Path = DB_PATH) -> bool:
    """摘要書き換えルールを学習する。同じキーワードは上書き。"""
    keyword, description = keyword.strip(), description.strip()
    if len(keyword) < 2 or not description or keyword == description:
        return False
    if _supabase_enabled(db_path):
        _sb().table("desc_rules").upsert(
            {"client": client, "keyword": keyword, "description": description},
            on_conflict="client,keyword",
        ).execute()
        return True
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO desc_rules (client, keyword, description) VALUES (?, ?, ?)
               ON CONFLICT (client, keyword) DO UPDATE SET description = excluded.description""",
            (client, keyword, description),
        )
    return True


def delete_desc_rule(rule_id: int, db_path: Path = DB_PATH) -> None:
    """摘要書き換えルールを削除する。"""
    if _supabase_enabled(db_path):
        _sb().table("desc_rules").delete().eq("id", rule_id).execute()
        return
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM desc_rules WHERE id = ?", (rule_id,))


def list_account_rules(db_path: Path = DB_PATH) -> list[dict]:
    """学習済みの科目ルール一覧を返す（新しい順）。"""
    if _supabase_enabled(db_path):
        res = _sb().table("account_rules").select("*").order("id", desc=True).execute()
        return res.data
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, keyword, account, side FROM account_rules ORDER BY id DESC"
        ).fetchall()
    return [{"id": r[0], "keyword": r[1], "account": r[2], "side": r[3]} for r in rows]


def add_account_rule(
    keyword: str, account: str, side: str = "expense", db_path: Path = DB_PATH
) -> bool:
    """科目ルールを学習する。同じキーワード・側があれば科目を上書きする。"""
    keyword, account = keyword.strip(), account.strip()
    if not keyword or not account or side not in ("expense", "income"):
        return False
    if _supabase_enabled(db_path):
        _sb().table("account_rules").upsert(
            {"keyword": keyword, "account": account, "side": side},
            on_conflict="keyword,side",
        ).execute()
        return True
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO account_rules (keyword, account, side) VALUES (?, ?, ?)
               ON CONFLICT (keyword, side) DO UPDATE SET account = excluded.account""",
            (keyword, account, side),
        )
    return True


def delete_account_rule(rule_id: int, db_path: Path = DB_PATH) -> None:
    """学習済みの科目ルールを削除する。"""
    if _supabase_enabled(db_path):
        _sb().table("account_rules").delete().eq("id", rule_id).execute()
        return
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM account_rules WHERE id = ?", (rule_id,))


def clear_entries(client: str, db_path: Path = DB_PATH) -> None:
    """クライアントの台帳を全削除する。"""
    if _supabase_enabled(db_path):
        _sb().table("entries").delete().eq("client", client).execute()
        return
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM entries WHERE client = ?", (client,))
