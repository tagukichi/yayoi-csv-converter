"""仕訳データの永続化（SQLite）。

クライアント企業ごとに仕訳を蓄積し、アプリを再起動しても消えないようにする。
Google Drive版でも同じ蓄積の仕組みを使い回す想定。DBファイルは data/ 配下に
置き、リポジトリにはコミットしない（.gitignore 済み）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from models import JournalEntry

DB_PATH = Path(__file__).resolve().parent / "data" / "journal.db"

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


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(_CREATE_SQL)
    return conn


def add_entries(
    client: str,
    entries: list[JournalEntry],
    source_file: str = "",
    db_path: Path = DB_PATH,
) -> int:
    """解析結果の仕訳をクライアントの台帳に追記する。追加件数を返す。"""
    if not entries:
        return 0
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


def clear_entries(client: str, db_path: Path = DB_PATH) -> None:
    """クライアントの台帳を全削除する。"""
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM entries WHERE client = ?", (client,))
