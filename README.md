# 日語學習自動化系統

這是部署於 Render 的 Python Flask 日語學習平台，包含每日教材、Telegram 推送、Render Cron、Dashboard、錯題複習、間隔重複、文法拆解、SNS 語感訓練與學習報告。

## Render Starter 部署建議

Render Web Service 升級到 Starter 後會常駐運行，不再因閒置而 spin down。建議保留目前的 Flask + Gunicorn 架構，並設定以下環境變數：

```txt
TZ=Asia/Taipei
SQLITE_DB_PATH=/var/data/app.db
```

若使用 SQLite 保存狀態、錯題與 SNS 練習紀錄，請在 Render 後台手動新增 Persistent Disk，並掛載到：

```txt
/var/data
```

系統啟動時會檢查 `SQLITE_DB_PATH`。如果該路徑尚未有資料庫，但專案目錄內存在舊的 `state.sqlite3`，會自動用 `shutil.copy2` 複製舊資料到新路徑。若新路徑已存在，絕不覆蓋。

## 健康檢查

Render 可使用以下端點：

```txt
GET /healthz
GET /readyz
```

`/healthz` 只檢查 Flask 行程是否存活，不連資料庫，不初始化 MeCab，也不呼叫外部 AI。

`/readyz` 檢查 SQLite 路徑可寫入、SQLite 可連線；若設定 `DATABASE_URL`，也會檢查 PostgreSQL 可連線。失敗時回傳 HTTP 503 與錯誤細節。

## SQLite 架構限制

目前 `SQLite + Persistent Disk` 僅適合 Render 單一 instance，也就是 Single Worker。

若未來要水平擴展到多個 instance，請把錯題、SNS 練習紀錄、測驗紀錄與設定資料全面遷移至 PostgreSQL，避免多 instance 同時寫入 SQLite 造成資料競爭。

## Migration 注意事項

不要把 SQLite migration 放在 Render Build Command 或 Pre-Deploy Command，因為 Persistent Disk 只在 runtime 階段可用。

本專案的 SQLite migration 是 idempotent，可安全重複執行，會在 Flask app 啟動後檢查資料表與欄位，不會清空既有資料。

本機可手動執行：

```bash
python migrate_sqlite.py
```

Render 上通常不需要手動執行，Web Service 啟動時會自動檢查。
