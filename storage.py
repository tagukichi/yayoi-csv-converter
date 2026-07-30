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
    "取引日付", "借方勘定科目", "借方税区分", "貸方勘定科目", "貸方税区分",
    "金額", "摘要", "要確認", "出典ファイル",
]

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    date TEXT NOT NULL,
    debit_account TEXT NOT NULL,
    debit_tax TEXT NOT NULL DEFAULT '対象外',
    credit_account TEXT NOT NULL,
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
        "debit_tax": e.debit_tax,
        "credit_account": e.credit_account,
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
        "debit_tax": str(r["借方税区分"]).strip() or "対象外",
        "credit_account": str(r["貸方勘定科目"]).strip(),
        "credit_tax": str(r["貸方税区分"]).strip() or "対象外",
        "amount": int(r["金額"]),
        "description": str(r["摘要"]).strip(),
        "needs_review": bool(r["要確認"]),
        "source_file": str(r["出典ファイル"]).strip(),
    }


_JP_COLUMNS = {
    "date": "取引日付",
    "debit_account": "借方勘定科目",
    "debit_tax": "借方税区分",
    "credit_account": "貸方勘定科目",
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
               (client, date, debit_account, debit_tax,
                credit_account, credit_tax, amount, description,
                needs_review, source_file)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    client,
                    e.date.strftime("%Y/%m/%d"),
                    e.debit_account,
                    e.debit_tax,
                    e.credit_account,
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
                      debit_tax AS 借方税区分,
                      credit_account AS 貸方勘定科目,
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
                str(r["借方税区分"]).strip() or "対象外",
                str(r["貸方勘定科目"]).strip(),
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
               (client, date, debit_account, debit_tax,
                credit_account, credit_tax, amount, description,
                needs_review, source_file)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
