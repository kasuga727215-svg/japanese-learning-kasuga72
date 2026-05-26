# Render 公網部署步驟

## 1. 上傳程式碼到 GitHub

如果電腦沒有安裝 Git，可以直接用 GitHub 網頁建立一個新倉庫，然後上傳這些檔案：

- app.py
- index.html
- requirements.txt
- Procfile
- runtime.txt

不要上傳 `.env`、`database.csv`、`settings.json`。

## 2. 在 Render 建立 Web Service

到 Render 建立新的 Web Service，並連接剛才的 GitHub 倉庫。

設定如下：

- Language: Python 3
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn --timeout 90 app:app`

## 3. 設定環境變數

在 Render 的 Environment 頁面加入：

- `DATABASE_URL`: Render PostgreSQL 提供的 External Database URL 或 Internal Database URL
- `GEMINI_API_KEY`: 你的 Gemini API Key
- `TG_TOKEN`: 你的 Telegram Bot Token
- `TG_CHAT_ID`: 你的 Telegram Chat ID
- `APP_URL`: Render 給你的公網網址，例如 `https://your-app.onrender.com`
- `GEMINI_MODEL`: 預設 `gemini-3-flash-preview`；若 Render 有設定，會以 Render 環境變數為準
- `GEMINI_TIMEOUT_SECONDS`: 預設 `20`。若 Render Start Command 已使用 `gunicorn --timeout 90 app:app`，可視情況調到 `40`
- `SLANG_CANDIDATE_WRITE_MODE`: 建議填 `sync`，讓新詞候選池在文法解析回傳前直接寫入資料庫
- `ENABLE_DEBUG_ENDPOINTS`: 平常填 `false`；排查新詞候選池時可短暫改成 `true`

新詞候選池排查時可暫時設定：

```txt
SLANG_CANDIDATE_WRITE_MODE=sync
ENABLE_DEBUG_ENDPOINTS=true
```

驗收完成後，請把 `ENABLE_DEBUG_ENDPOINTS` 改回 `false`。

## PostgreSQL 說明

如果設定了 `DATABASE_URL`，系統會自動使用 PostgreSQL 保存教材和設定。

如果沒有設定 `DATABASE_URL`，系統會退回本地 `database.csv` 和 `settings.json`。這個方式只適合本機開發，不適合 Render 免費版長期保存資料。

## 4. 部署完成

部署成功後，Render 會提供一個公網網址：

`https://你的服務名.onrender.com`

手機和電腦都可以直接開啟這個網址。
