# Render 公网部署步骤

## 1. 上传代码到 GitHub

如果电脑没有安装 Git，可以直接用 GitHub 网页建立一个新仓库，然后上传这些文件：

- app.py
- index.html
- requirements.txt
- Procfile
- runtime.txt

不要上传 `.env`、`database.csv`、`settings.json`。

## 2. 在 Render 建立 Web Service

到 Render 建立新的 Web Service，并连接刚才的 GitHub 仓库。

设置如下：

- Language: Python 3
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`

## 3. 设置环境变量

在 Render 的 Environment 页面加入：

- `GEMINI_API_KEY`: 你的 Gemini API Key
- `TG_TOKEN`: 你的 Telegram Bot Token
- `TG_CHAT_ID`: 你的 Telegram Chat ID
- `APP_URL`: Render 给你的公网网址，例如 `https://your-app.onrender.com`
- `GEMINI_MODEL`: 可留空；若要固定模型，可填 `gemini-3-flash-preview`

## 4. 部署完成

部署成功后，Render 会提供一个公网网址：

`https://你的服务名.onrender.com`

手机和电脑都可以直接打开这个网址。
