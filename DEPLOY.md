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
   # SUPABASE_KEY = "xxxxx"
   ```

5. 「Deploy」。数分でビルドされ、`https://<名前>.streamlit.app` が発行される。

6. 共有相手にURLと APP_PASSWORD を伝える。

## PoC 時点の注意

- **データは揮発性**: Supabase 未設定だと SQLite に保存されるが、
  Community Cloud ではアプリ再起動・再デプロイで消える。デモには十分だが、
  溜めたい場合は Supabase を設定する（8月契約後）。
- **サーバーは海外（US）**: Community Cloud のホストは US。PoC はサンプル/
  テストデータで行い、実顧客の本物の通帳等は本番SaaS（国内リージョン）まで待つ。
- **秘密情報**: Azure キー・パスワードは Secrets にのみ入れる。コードや
  `.env` を Git にコミットしない（`.gitignore` 済み）。
