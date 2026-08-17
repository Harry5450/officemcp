# OfficeCLI Line Bot

## 架構

```
Line → Line Bot (Zeabur) → API Server (Zeabur)
```

## 部署步驟

### 1. 推送到 GitHub

```bash
git init
git add .
git commit -m "init"
git remote add origin https://github.com/YOUR_USERNAME/officemcp.git
git push -u origin master
```

### 2. 部署 API Server

1. Zeabur → New Project → Deploy from GitHub
2. 選擇 `officemcp` 倉庫
3. 服務名稱：`officemcp-api`
4. Environment Variables 加入：
   ```
   PORT=8080
   ```
5. 等待部署完成，記下網址（如 `https://officemcp-api.zeabur.app`）

### 3. 部署 Line Bot

1. Zeabur → 同一專案 → New Service → GitHub
2. 選擇同一個倉庫
3. 服務名稱：`officemcp-linebot`
4. **Build Settings** → Dockerfile 改為：`Dockerfile.linebot`
5. Environment Variables 加入：
   ```
   LINE_CHANNEL_SECRET=你的Secret
   LINE_CHANNEL_ACCESS_TOKEN=你的Token
   API_URL=https://officemcp-api.zeabur.app
   ```
6. 等待部署完成

### 4. 設定 Line Webhook

1. LINE Developers → Messaging API
2. Webhook URL 設為：`https://officemcp-linebot.zeabur.app/webhook`
3. 啟用 Use webhook
4. 關閉 Auto-reply messages

## Line 指令

| 指令 | 說明 |
|------|------|
| `/create report.docx` | 建立 Word 文件 |
| `/create data.xlsx` | 建立 Excel 試算表 |
| `/create deck.pptx` | 建立 PowerPoint |
| `/cmd view report.docx outline` | 執行 officecli 指令 |
| `/preview deck.pptx` | 生成預覽圖 |
| `/help` | 使用說明 |
