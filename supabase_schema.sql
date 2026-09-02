-- Supabase の SQL Editor でこのファイルの内容を実行してテーブルを作成する。
-- 構造はローカル SQLite 版（storage.py）と同一。
--
-- 注意: 現段階（検証フェーズ・単独利用）では RLS を無効のまま anon キーで
-- アクセスする。SaaS 化してログインを入れる段階で RLS を有効化し、
-- テナントごとのポリシーを設定すること。

create table if not exists clients (
  id bigint generated always as identity primary key,
  name text not null unique,
  created_at timestamptz not null default now()
);

create table if not exists entries (
  id bigint generated always as identity primary key,
  client text not null,
  date text not null,                          -- YYYY/MM/DD
  debit_account text not null,                 -- 借方勘定科目
  debit_sub text not null default '',          -- 借方補助科目
  debit_tax text not null default '対象外',    -- 借方税区分
  credit_account text not null,                -- 貸方勘定科目
  credit_sub text not null default '',         -- 貸方補助科目
  credit_tax text not null default '対象外',   -- 貸方税区分
  amount bigint not null,                      -- 金額（円）
  description text not null default '',        -- 摘要
  needs_review boolean not null default false, -- 要確認フラグ
  source_file text not null default '',        -- 出典ファイル名
  created_at timestamptz not null default now()
);

-- 既にテーブルを作成済みの場合は以下で補助科目列を追加する
alter table entries add column if not exists debit_sub text not null default '';
alter table entries add column if not exists credit_sub text not null default '';

create index if not exists entries_client_idx on entries (client, date);

-- クライアント別の補助科目マスタ（弥生の補助科目一覧表から取り込む「事前登録」）
create table if not exists subaccounts (
  id bigint generated always as identity primary key,
  client text not null,
  account text not null,
  sub_name text not null,
  search_key text not null default '',
  created_at timestamptz not null default now(),
  unique (client, account, sub_name)
);

-- 摘要の書き換えルール（クライアント別）。「セブンイレブン→飲食代」のような
-- 会社ごとの摘要の流儀を学習する
create table if not exists desc_rules (
  id bigint generated always as identity primary key,
  client text not null,
  keyword text not null,
  description text not null,
  created_at timestamptz not null default now(),
  unique (client, keyword)
);

-- 一括置換から学習した「摘要キーワード → 勘定科目」ルール
-- side: expense=借方（費用）, income=貸方（収益）
create table if not exists account_rules (
  id bigint generated always as identity primary key,
  keyword text not null,
  account text not null,
  side text not null default 'expense',
  created_at timestamptz not null default now(),
  unique (keyword, side)
);

-- クライアント別の勘定科目マスタ（弥生の勘定科目一覧表から取り込む「事前登録」）
create table if not exists account_master (
  id bigint generated always as identity primary key,
  client text not null,
  name text not null,
  search_key text not null default '',
  side text not null default '借方',        -- 貸借区分（借方/貸方）
  tax_class text not null default '',       -- 弥生の税区分（対象外/課対仕入/課税売上 等）
  created_at timestamptz not null default now(),
  unique (client, name)
);

-- 書類タイプ→勘定科目の紐付け（クライアント別）。売上（売掛表）・請求書・
-- 買掛表の仕訳で使う借方/貸方科目と、取引先を補助科目に入れる側。
create table if not exists doctype_rules (
  id bigint generated always as identity primary key,
  client text not null,
  doc_type text not null,
  debit_account text not null default '',
  credit_account text not null default '',
  sub_side text not null default 'debit',   -- debit=借方に取引先の補助科目, credit=貸方に
  created_at timestamptz not null default now(),
  unique (client, doc_type)
);

-- 売掛表・買掛表の「行番号 → 取引先名」の対応（クライアント別）
-- side: sales=売掛表（売上）, purchase=買掛表
create table if not exists partner_rows (
  id bigint generated always as identity primary key,
  client text not null,
  side text not null default 'sales',
  row_no integer not null,
  partner_name text not null,
  created_at timestamptz not null default now(),
  unique (client, side, row_no)
);
