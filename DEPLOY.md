# PoC デプロイ手順（Streamlit Community Cloud）

現状のアプリをURLで共有するための手順。Streamlit は Vercel では動かないため、
Streamlit 公式の無料ホスティング（Community Cloud）を使う。

## 前提
- GitHub リポジトリ: `tagukichi/yayoi-csv-converter`
- デプロイするブランチ: `claude/sharp-clarke-mvbrvm`（または main にマージ後 main）

## 手順

1. https://share.streamlit.io にアクセスし、GitHub アカウントでサインイン。

2. 「Create app」→「Deploy a public app from GitHub」を選択。

3. 以下を指定:
   - Repository: `tagukichi/yayoi-csv-converter`
   - Branch: `claude/sharp-clarke-mvbrvm`
   - Main file path: `app.py`
   - Python version（Advanced settings）: **3.11** 以上

4. 「Advanced settings」→「Secrets」に、以下を TOML 形式で貼り付ける
   （値は自分のものに置き換え。`.env` の中身と同じ）:

   ```toml
   AZURE_VISION_ENDPOINT = "https://xxxxx.cognitiveservices.azure.com"
   AZURE_VISION_KEY = "xxxxxxxx"

   # 公開URLをパスワードで保護する（PoCでは必須）。共有相手に別途伝える
   APP_PASSWORD = "好きなパスワード"

   # Supabase を使う場合のみ（未設定ならローカルSQLite=このホストでは揮発性）
   # SUPABASE_URL = "https://xxxxx.supabase.co"
   # SUPABASE_KEY = "service_role キー"
   ```

5. 「Deploy」。数分でビルドされ、`https://<名前>.streamlit.app` が発行される。

6. 共有相手にURLと APP_PASSWORD を伝える。

## Supabase を使う（データを消えないようにする）

Community Cloud は再起動のたびにディスクが消えるため、事前登録や仕訳を
残したい場合は Supabase に切り替える。`SUPABASE_URL` と `SUPABASE_KEY` の
両方が設定されていれば自動で Supabase を使う（`storage.py`）。

### 1. プロジェクトを作る

1. https://supabase.com にサインイン →「New project」
2. Name（例: `yayoi-csv`）、Database Password（自動生成でよい。控えておく）、
   Region は **Northeast Asia (Tokyo)** を選ぶ
3. 作成まで数分待つ

無料プランは **1組織あたり2プロジェクト**まで。埋まっている場合は
「New organization」で別組織を作れば追加できる。

### 2. テーブルを作る

左メニューの **SQL Editor** →「New query」に `supabase_schema.sql` の中身を
すべて貼り付けて「Run」。テーブル11個の作成と、RLS の有効化まで行われる。

このファイルは何度実行しても安全（`create table if not exists` のため）。
後からテーブルを追加したときも、同じ手順で流し直せばよい。

### 3. 接続情報を取得する

**Settings（歯車）→ API**（新しい画面では **API Keys**）から2つ控える:

- **Project URL**: `https://xxxxx.supabase.co`
- **service_role** のキー（`secret` と書かれている方。`anon` ではない）

`service_role` を使うのは、RLS を全テーブルで有効にして anon キーからの
アクセスを塞いでいるため。Streamlit はサーバー側で動くのでキーがブラウザに
渡ることはない。**このキーは絶対に公開・コミットしないこと。**

### 4. Streamlit Cloud に設定する

アプリの「⋮」→ Settings → Secrets に追記して Save:

```toml
SUPABASE_URL = "https://xxxxx.supabase.co"
SUPABASE_KEY = "service_role のキー"
```

保存するとアプリが自動で再起動する。

### 5. 確認

サイドバー左下の表示が **「保存先: Supabase」** になっていれば成功
（未設定なら「ローカル (SQLite)」）。企業を1社追加して、Supabase の
**Table Editor → clients** に行が増えていれば接続できている。

### 無料プランの注意

- **7日間アクセスがないとプロジェクトが一時停止**する。再開はダッシュボードの
  ボタン1つだが、停止中はアプリがエラーになる。週1回以上使うなら問題ない
- 容量は 500MB（仕訳データなら数十万件でも収まる）
- 自動バックアップは有料プランのみ。必要なら定期的に弥生CSVを出しておく
- これまで SQLite に溜めたデータは自動では移らない。切り替え後は事前登録を
  やり直す（PDFを再アップロードするだけ）

## PoC 時点の注意

- **データは揮発性**: Supabase 未設定だと SQLite に保存されるが、
  Community Cloud ではアプリ再起動・再デプロイで消える。デモには十分だが、
  溜めたい場合は Supabase を設定する（8月契約後）。
- **サーバーは海外（US）**: Community Cloud のホストは US。PoC はサンプル/
  テストデータで行い、実顧客の本物の通帳等は本番SaaS（国内リージョン）まで待つ。
- **秘密情報**: Azure キー・パスワードは Secrets にのみ入れる。コードや
  `.env` を Git にコミットしない（`.gitignore` 済み）。
