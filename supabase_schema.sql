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
  debit_tax text not null default '対象外',    -- 借方税区分
  credit_account text not null,                -- 貸方勘定科目
  credit_tax text not null default '対象外',   -- 貸方税区分
  amount bigint not null,                      -- 金額（円）
  description text not null default '',        -- 摘要
  needs_review boolean not null default false, -- 要確認フラグ
  source_file text not null default '',        -- 出典ファイル名
  created_at timestamptz not null default now()
);

create index if not exists entries_client_idx on entries (client, date);

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
