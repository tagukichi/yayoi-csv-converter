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
from datetime import datetime
from pathlib import Path

import pandas as pd

from models import JournalEntry

DB_PATH = Path(__file__).resolve().parent / "data" / "journal.db"

# 以前に見本として自動登録していた企業。空のものは起動時に片付ける
# （企業は画面の「企業の追加・削除」から登録する）
_SAMPLE_CLIENTS = ["A建設", "B工務店", "C社"]

# data_editor での表示順・編集対象の列。DB の列と一対一。
EDITABLE_COLUMNS = [
    "取引日付", "借方勘定科目", "借方補助科目", "借方部門", "借方税区分",
    "貸方勘定科目", "貸方補助科目", "貸方部門", "貸方税区分",
    "金額", "摘要", "要確認", "備考", "出典ファイル",
]

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    date TEXT NOT NULL,
    debit_account TEXT NOT NULL,
    debit_sub TEXT NOT NULL DEFAULT '',
    debit_dept TEXT NOT NULL DEFAULT '',
    debit_tax TEXT NOT NULL DEFAULT '対象外',
    credit_account TEXT NOT NULL,
    credit_sub TEXT NOT NULL DEFAULT '',
    credit_dept TEXT NOT NULL DEFAULT '',
    credit_tax TEXT NOT NULL DEFAULT '対象外',
    amount INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    needs_review INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
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

# 給与台帳の会社独自の控除項目（駐車場代・社宅・水道光熱費・立替返済 等）
# → 勘定科目・補助科目の対応（クライアント別）。account が空の行は
# 「台帳で見つかったが科目未設定」の状態。
_CREATE_PAYROLL_DEDUCTIONS_SQL = """
CREATE TABLE IF NOT EXISTS payroll_deductions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    label TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    sub_account TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (client, label)
)
"""

# 部門マスタ（クライアント別）。弥生の仕訳CSVの借方部門・貸方部門の列や、
# 会社が作った部門一覧（PDF・画像）から登録する。任意の事前登録
_CREATE_DEPARTMENTS_SQL = """
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (client, name)
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

# 事前登録の登録元ファイル（どのPDFをいつ登録したか）。画面で
# 「登録済み・ファイル名・日時」を出し、差し替えの判断に使う。
# kind: subaccounts / accounts / desc_dict
_CREATE_MASTER_META_SQL = """
CREATE TABLE IF NOT EXISTS master_meta (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL,
    kind TEXT NOT NULL,
    file_name TEXT NOT NULL DEFAULT '',
    registered_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (client, kind)
)
"""

# 企業セレクタの表示設定（ピン留め・最後に開いた日時）。
# ログイン導入後は user_id を足して人ごとの設定にする。
_CREATE_CLIENT_PREFS_SQL = """
CREATE TABLE IF NOT EXISTS client_prefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client TEXT NOT NULL UNIQUE,
    pinned INTEGER NOT NULL DEFAULT 0,
    last_opened_at TEXT NOT NULL DEFAULT ''
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
        "debit_dept": e.debit_dept,
        "debit_tax": e.debit_tax,
        "credit_account": e.credit_account,
        "credit_sub": e.credit_sub,
        "credit_dept": e.credit_dept,
        "credit_tax": e.credit_tax,
        "amount": e.amount,
        "description": e.description,
        "needs_review": bool(e.needs_review),
        "note": e.note,
        "source_file": source_file,
    }


def _text(value) -> str:
    """表のセルを文字列にする（空欄・欠損は空文字）。"""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _row_to_record(client: str, r: pd.Series) -> dict:
    return {
        "client": client,
        "date": str(r["取引日付"]).strip(),
        "debit_account": str(r["借方勘定科目"]).strip(),
        "debit_sub": str(r.get("借方補助科目", "") or "").strip(),
        "debit_dept": _text(r.get("借方部門")),
        "debit_tax": str(r["借方税区分"]).strip() or "対象外",
        "credit_account": str(r["貸方勘定科目"]).strip(),
        "credit_sub": str(r.get("貸方補助科目", "") or "").strip(),
        "credit_dept": _text(r.get("貸方部門")),
        "credit_tax": str(r["貸方税区分"]).strip() or "対象外",
        "amount": int(r["金額"]),
        "description": str(r["摘要"]).strip(),
        "needs_review": bool(r["要確認"]),
        "note": str(r.get("備考", "") or "").strip(),
        "source_file": str(r["出典ファイル"]).strip(),
    }


_JP_COLUMNS = {
    "date": "取引日付",
    "debit_account": "借方勘定科目",
    "debit_sub": "借方補助科目",
    "debit_dept": "借方部門",
    "debit_tax": "借方税区分",
    "credit_account": "貸方勘定科目",
    "credit_sub": "貸方補助科目",
    "credit_dept": "貸方部門",
    "credit_tax": "貸方税区分",
    "amount": "金額",
    "description": "摘要",
    "needs_review": "要確認",
    "note": "備考",
    "source_file": "出典ファイル",
}


def _records_to_df(records: list[dict]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(columns=list(_JP_COLUMNS.values()))
    df = pd.DataFrame(records)
    for col in ("debit_dept", "credit_dept"):  # 列追加前に作ったテーブル向け
        if col not in df.columns:
            df[col] = ""
    df = df[list(_JP_COLUMNS.keys())].rename(columns=_JP_COLUMNS)
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
    conn.execute(_CREATE_PAYROLL_DEDUCTIONS_SQL)
    conn.execute(_CREATE_DEPARTMENTS_SQL)
    conn.execute(_CREATE_DESC_DICT_SQL)
    conn.execute(_CREATE_MASTER_META_SQL)
    conn.execute(_CREATE_CLIENT_PREFS_SQL)
    # 既存DBへの列追加（後方互換のためのマイグレーション）
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(entries)")}
    if "debit_sub" not in existing_cols:
        conn.execute("ALTER TABLE entries ADD COLUMN debit_sub TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE entries ADD COLUMN credit_sub TEXT NOT NULL DEFAULT ''")
        conn.commit()
    if "note" not in existing_cols:
        conn.execute("ALTER TABLE entries ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        conn.commit()
    if "debit_dept" not in existing_cols:
        conn.execute("ALTER TABLE entries ADD COLUMN debit_dept TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE entries ADD COLUMN credit_dept TEXT NOT NULL DEFAULT ''")
        conn.commit()
    # 見本として入れていた既定の企業（A建設・B工務店・C社）は、テスト用の
    # ダミーなので仕訳ごと片付ける（本番のクライアントは画面から登録する）
    if conn.execute("PRAGMA user_version").fetchone()[0] < 3:
        for name in _SAMPLE_CLIENTS:
            _purge_client(conn, name)
        conn.execute("PRAGMA user_version = 3")
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


# 企業ごとに持っているデータ（企業を消すときはまとめて消す）
_CLIENT_TABLES = (
    "entries", "subaccounts", "account_master", "desc_dict",
    "desc_rules", "doctype_rules", "partner_rows", "payroll_deductions",
    "departments", "master_meta", "client_prefs",
)


def _purge_client(conn: sqlite3.Connection, name: str) -> None:
    """その企業のデータ（仕訳・各マスタ・学習した摘要ルール）を全部消す。"""
    for table in _CLIENT_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE client = ?", (name,))
    conn.execute("DELETE FROM clients WHERE name = ?", (name,))


def delete_client(name: str, db_path: Path = DB_PATH) -> None:
    """企業を削除する。仕訳・事前登録のマスタ・摘要ルールもまとめて削除する。"""
    if _supabase_enabled(db_path):
        for table in _CLIENT_TABLES:
            _sb().table(table).delete().eq("client", name).execute()
        _sb().table("clients").delete().eq("name", name).execute()
        return
    with _connect(db_path) as conn:
        _purge_client(conn, name)


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
               (client, date, debit_account, debit_sub, debit_dept, debit_tax,
                credit_account, credit_sub, credit_dept, credit_tax, amount, description,
                needs_review, note, source_file)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    client,
                    e.date.strftime("%Y/%m/%d"),
                    e.debit_account,
                    e.debit_sub,
                    e.debit_dept,
                    e.debit_tax,
                    e.credit_account,
                    e.credit_sub,
                    e.credit_dept,
                    e.credit_tax,
                    e.amount,
                    e.description,
                    int(e.needs_review),
                    e.note,
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
                      debit_dept AS 借方部門,
                      debit_tax AS 借方税区分,
                      credit_account AS 貸方勘定科目,
                      credit_sub AS 貸方補助科目,
                      credit_dept AS 貸方部門,
                      credit_tax AS 貸方税区分,
                      amount AS 金額,
                      description AS 摘要,
                      needs_review AS 要確認,
                      note AS 備考,
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
                _text(r.get("借方部門")),
                str(r["借方税区分"]).strip() or "対象外",
                str(r["貸方勘定科目"]).strip(),
                str(r.get("貸方補助科目", "") or "").strip(),
                _text(r.get("貸方部門")),
                str(r["貸方税区分"]).strip() or "対象外",
                int(r["金額"]),
                str(r["摘要"]).strip(),
                int(bool(r["要確認"])),
                str(r.get("備考", "") or "").strip(),
                str(r["出典ファイル"]).strip(),
            )
            for _, r in df.iterrows()
        ]
        conn.executemany(
            """INSERT INTO entries
               (client, date, debit_account, debit_sub, debit_dept, debit_tax,
                credit_account, credit_sub, credit_dept, credit_tax, amount, description,
                needs_review, note, source_file)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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


def review_counts_by_client(db_path: Path = DB_PATH) -> dict[str, int]:
    """企業名 → 要確認の残件数。企業セレクタのバッジに使う（1回の問い合わせで全社分）。"""
    if _supabase_enabled(db_path):
        res = (
            _sb().table("entries").select("client")
            .eq("needs_review", True).execute()
        )
        counts: dict[str, int] = {}
        for r in res.data:
            counts[r["client"]] = counts.get(r["client"], 0) + 1
        return counts
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT client, COUNT(*) FROM entries WHERE needs_review = 1 GROUP BY client"
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def list_client_prefs(db_path: Path = DB_PATH) -> dict[str, dict]:
    """企業名 → 表示設定（pinned・last_opened_at）。未設定の企業は入らない。"""
    if _supabase_enabled(db_path):
        rows = _sb().table("client_prefs").select("*").execute().data
    else:
        with _connect(db_path) as conn:
            fetched = conn.execute(
                "SELECT client, pinned, last_opened_at FROM client_prefs"
            ).fetchall()
        rows = [
            {"client": r[0], "pinned": r[1], "last_opened_at": r[2]} for r in fetched
        ]
    return {
        r["client"]: {
            "pinned": bool(r["pinned"]),
            "last_opened_at": r["last_opened_at"] or "",
        }
        for r in rows
    }


def set_client_pinned(client: str, pinned: bool, db_path: Path = DB_PATH) -> None:
    """企業のピン留めを設定する（セレクタの先頭に固定される）。"""
    if _supabase_enabled(db_path):
        _sb().table("client_prefs").upsert(
            {"client": client, "pinned": bool(pinned)}, on_conflict="client"
        ).execute()
        return
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO client_prefs (client, pinned) VALUES (?, ?)
               ON CONFLICT (client) DO UPDATE SET pinned = excluded.pinned""",
            (client, int(bool(pinned))),
        )


def touch_client_opened(client: str, db_path: Path = DB_PATH) -> None:
    """企業を開いた時刻を記録する（「最近使った順」の並び替えに使う）。"""
    now = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    if _supabase_enabled(db_path):
        _sb().table("client_prefs").upsert(
            {"client": client, "last_opened_at": now}, on_conflict="client"
        ).execute()
        return
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO client_prefs (client, last_opened_at) VALUES (?, ?)
               ON CONFLICT (client) DO UPDATE SET last_opened_at = excluded.last_opened_at""",
            (client, now),
        )


# 企業セレクタの並び替え（画面のラベル → 並べ方の指定）
CLIENT_SORTS = {
    "最近使った順": "recent",
    "名前順": "name",
    "要確認が多い順": "review",
}


def sort_clients(
    clients: list[str],
    prefs: dict[str, dict],
    review_counts: dict[str, int],
    order: str = "recent",
) -> list[str]:
    """企業セレクタの並び順を作る。ピン留めした企業は常に先頭。"""
    def key(name: str):
        pref = prefs.get(name, {})
        pinned = 0 if pref.get("pinned") else 1  # ピン留めを先に
        if order == "name":
            return (pinned, name)
        if order == "review":
            return (pinned, -review_counts.get(name, 0), name)
        # 最近使った順（未使用の企業は後ろ）
        return (pinned, _reverse_time(pref.get("last_opened_at", "")), name)

    return sorted(clients, key=key)


def _reverse_time(stamp: str) -> str:
    """新しい日時ほど小さくなる文字列（昇順ソートで降順になる）。

    一度も開いていない企業は "9" を返し、開いたことのある企業（"1" 始まり）
    より後ろに並ぶようにする。
    """
    if not stamp:
        return "9"
    # 数字を反転させると、文字列の昇順がそのまま日時の降順になる
    return "1" + "".join(str(9 - int(c)) if c.isdigit() else c for c in stamp)


def list_source_files_detail(client: str, db_path: Path = DB_PATH) -> list[dict]:
    """出典ファイル名・件数・取り込み日時を返す（新しい順）。"""
    if _supabase_enabled(db_path):
        res = (
            _sb().table("entries").select("source_file,created_at")
            .eq("client", client).neq("source_file", "").order("id", desc=True).execute()
        )
        agg: dict[str, dict] = {}
        for r in res.data:
            item = agg.setdefault(
                r["source_file"],
                {"name": r["source_file"], "count": 0, "imported_at": r.get("created_at", "")},
            )
            item["count"] += 1
        return list(agg.values())
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT source_file, COUNT(*), MAX(created_at) FROM entries
               WHERE client = ? AND source_file != ''
               GROUP BY source_file ORDER BY MAX(id) DESC""",
            (client,),
        ).fetchall()
    return [{"name": r[0], "count": r[1], "imported_at": r[2] or ""} for r in rows]


def get_master_meta(client: str, kind: str, db_path: Path = DB_PATH) -> dict | None:
    """事前登録の登録元ファイル情報（ファイル名・登録日時）を返す。"""
    if _supabase_enabled(db_path):
        rows = (
            _sb().table("master_meta").select("*")
            .eq("client", client).eq("kind", kind).execute().data
        )
        return (
            {"file_name": rows[0]["file_name"], "registered_at": rows[0]["registered_at"]}
            if rows else None
        )
    with _connect(db_path) as conn:
        r = conn.execute(
            "SELECT file_name, registered_at FROM master_meta WHERE client = ? AND kind = ?",
            (client, kind),
        ).fetchone()
    return {"file_name": r[0], "registered_at": r[1]} if r else None


def set_master_meta(client: str, kind: str, file_name: str, db_path: Path = DB_PATH) -> None:
    """事前登録の登録元ファイル情報を記録する（同じ種類は上書き）。"""
    now = datetime.now().strftime("%Y/%m/%d %H:%M")
    if _supabase_enabled(db_path):
        _sb().table("master_meta").upsert(
            {"client": client, "kind": kind, "file_name": file_name, "registered_at": now},
            on_conflict="client,kind",
        ).execute()
        return
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT INTO master_meta (client, kind, file_name, registered_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (client, kind) DO UPDATE SET
                 file_name = excluded.file_name, registered_at = excluded.registered_at""",
            (client, kind, file_name, now),
        )


def clear_master_meta(client: str, kind: str, db_path: Path = DB_PATH) -> None:
    """事前登録の登録元ファイル情報を削除する（マスタを空にするときに使う）。"""
    if _supabase_enabled(db_path):
        _sb().table("master_meta").delete().eq("client", client).eq("kind", kind).execute()
        return
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM master_meta WHERE client = ? AND kind = ?", (client, kind))


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


# --- 部門マスタ（クライアント別） ---


def list_departments(client: str, db_path: Path = DB_PATH) -> list[str]:
    """登録済みの部門名を登録順で返す。"""
    if _supabase_enabled(db_path):
        rows = (
            _sb().table("departments").select("name")
            .eq("client", client).order("id").execute().data
        )
        return [r["name"] for r in rows]
    with _connect(db_path) as conn:
        return [
            r[0] for r in conn.execute(
                "SELECT name FROM departments WHERE client = ? ORDER BY id", (client,)
            )
        ]


def replace_departments(client: str, records: list[dict], db_path: Path = DB_PATH) -> int:
    """部門マスタを一括で置き換える（空欄・重複は除く）。登録件数を返す。

    records は [{"name": 部門名}, ...]。事前登録の他のマスタと同じ形にしている。
    """
    names = list(dict.fromkeys(
        _text(r.get("name")) for r in records if _text(r.get("name"))
    ))
    if _supabase_enabled(db_path):
        _sb().table("departments").delete().eq("client", client).execute()
        if names:
            _sb().table("departments").insert(
                [{"client": client, "name": n} for n in names]
            ).execute()
        return len(names)
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM departments WHERE client = ?", (client,))
        conn.executemany(
            "INSERT INTO departments (client, name) VALUES (?, ?)",
            [(client, n) for n in names],
        )
    return len(names)


# --- 給与台帳の控除項目→勘定科目の対応（クライアント別） ---


def list_payroll_deductions(client: str, db_path: Path = DB_PATH) -> list[dict]:
    """控除項目の一覧を返す（登録順）。account が空なら科目未設定。"""
    if _supabase_enabled(db_path):
        return (
            _sb().table("payroll_deductions").select("*")
            .eq("client", client).order("id").execute().data
        )
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT id, label, account, sub_account FROM payroll_deductions
               WHERE client = ? ORDER BY id""",
            (client,),
        ).fetchall()
    return [{"id": r[0], "label": r[1], "account": r[2], "sub_account": r[3]} for r in rows]


def payroll_deduction_map(client: str, db_path: Path = DB_PATH) -> dict[str, tuple[str, str]]:
    """parse_payroll に渡す形（項目名 → (勘定科目, 補助科目)）。科目未設定は含めない。"""
    return {
        r["label"]: (r["account"], r["sub_account"])
        for r in list_payroll_deductions(client, db_path)
        if r["account"]
    }


def ensure_payroll_deductions(client: str, labels: list[str], db_path: Path = DB_PATH) -> int:
    """台帳で見つかった控除項目を、未登録なら科目空欄で一覧に加える。追加件数を返す。"""
    existing = {r["label"] for r in list_payroll_deductions(client, db_path)}
    new_labels = list(dict.fromkeys(l.strip() for l in labels if l.strip() and l.strip() not in existing))
    if not new_labels:
        return 0
    records = [{"client": client, "label": l, "account": "", "sub_account": ""} for l in new_labels]
    if _supabase_enabled(db_path):
        _sb().table("payroll_deductions").insert(records).execute()
        return len(records)
    with _connect(db_path) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO payroll_deductions (client, label) VALUES (?, ?)",
            [(client, l) for l in new_labels],
        )
    return len(records)


def add_payroll_deduction(
    client: str, label: str, account: str = "", sub_account: str = "", db_path: Path = DB_PATH
) -> str:
    """控除項目を1つ追加する（よく使う項目のボタン用）。

    戻り値: "added"=新しく追加 / "filled"=登録済みで科目が空欄だったので科目を入れた /
    "exists"=登録済みで科目も決まっているので何もしない。
    """
    label = label.strip()
    current = {r["label"]: r for r in list_payroll_deductions(client, db_path)}
    if label in current:
        if current[label]["account"] or not account:
            return "exists"
        records = [
            {**r, "account": account, "sub_account": sub_account} if r["label"] == label else r
            for r in current.values()
        ]
        replace_payroll_deductions(client, records, db_path)
        return "filled"
    replace_payroll_deductions(
        client,
        list(current.values()) + [{"label": label, "account": account, "sub_account": sub_account}],
        db_path,
    )
    return "added"


def replace_payroll_deductions(client: str, records: list[dict], db_path: Path = DB_PATH) -> int:
    """控除項目→科目の対応を一括で置き換える。登録件数を返す。"""
    seen: set[str] = set()
    cleaned = []
    for r in records:
        label = str(r.get("label", "") or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        cleaned.append({
            "client": client,
            "label": label,
            "account": str(r.get("account", "") or "").strip(),
            "sub_account": str(r.get("sub_account", "") or "").strip(),
        })
    if _supabase_enabled(db_path):
        _sb().table("payroll_deductions").delete().eq("client", client).execute()
        if cleaned:
            _sb().table("payroll_deductions").insert(cleaned).execute()
        return len(cleaned)
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM payroll_deductions WHERE client = ?", (client,))
        conn.executemany(
            """INSERT INTO payroll_deductions (client, label, account, sub_account)
               VALUES (?, ?, ?, ?)""",
            [(c["client"], c["label"], c["account"], c["sub_account"]) for c in cleaned],
        )
    return len(cleaned)


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
