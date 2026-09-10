-- Supabase の SQL Editor でこのファイルの内容を実行してテーブルを作成する。
-- 構造はローカル SQLite 版（storage.py）と同一。
--
-- 接続は service_role キーで行い、全テーブルで RLS を有効にする
-- （末尾を参照）。ログインを入れてテナントを分ける段階で、テナント単位の
-- ポリシーを追加する。

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

-- クライアント別の摘要辞書（弥生の摘要科目一覧から取り込む「事前登録」）
-- 摘要 → 勘定科目 の対応（同じ摘要が複数の科目に登録されることもある）
create table if not exists desc_dict (
  id bigint generated always as identity primary key,
  client text not null,
  description text not null,
  account text not null,
  search_key text not null default '',
  created_at timestamptz not null default now(),
  unique (client, description, account)
);

-- 事前登録の登録元ファイル（どのPDFをいつ登録したか）
-- kind: subaccounts / accounts / desc_dict
create table if not exists master_meta (
  id bigint generated always as identity primary key,
  client text not null,
  kind text not null,
  file_name text not null default '',
  registered_at text not null default '',
  unique (client, kind)
);

-- 仕訳の備考（日付を読み取れず本日日付を仮置き、残高不一致 など）
alter table entries add column if not exists note text not null default '';

-- 企業セレクタの表示設定（ピン留め・最後に開いた日時）
-- ログイン導入後は user_id を足して人ごとの設定にする
create table if not exists client_prefs (
  id bigint generated always as identity primary key,
  client text not null unique,
  pinned boolean not null default false,
  last_opened_at text not null default ''
);

-- ================================================================
-- アクセス制限（RLS）
-- ================================================================
-- すべてのテーブルで行レベルセキュリティを有効にし、ポリシーは作らない。
-- これで anon キー（公開前提のキー）からは一切読み書きできなくなる。
--
-- アプリ（Streamlit）は service_role キーで接続するので RLS を迂回する。
-- Streamlit はサーバー側で動き、キーがブラウザに渡ることはないため、
-- 会計データを anon キー任せにするより安全。
--
-- ログインを入れてテナントを分ける段階で、ここにテナント単位のポリシーを
-- 追加し、アプリ側もログインユーザーのキーで接続するように変える。

alter table clients        enable row level security;
alter table entries        enable row level security;
alter table subaccounts    enable row level security;
alter table account_master enable row level security;
alter table desc_dict      enable row level security;
alter table desc_rules     enable row level security;
alter table account_rules  enable row level security;
alter table doctype_rules  enable row level security;
alter table partner_rows   enable row level security;
alter table master_meta    enable row level security;
alter table client_prefs   enable row level security;
