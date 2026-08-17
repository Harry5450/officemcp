import os
import logging
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

# 環境變數
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
API_URL = os.environ.get("API_URL", "https://mcpoffice.zeabur.app")
PORT = int(os.environ.get("PORT", 8080))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("linebot")

app = FastAPI(title="OfficeCLI Line Bot")


async def call_api(path: str, data: dict) -> dict:
    """呼叫 API 伺服器。"""
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            r = await client.post(f"{API_URL}{path}", json=data)
            return r.json()
        except Exception as e:
            return {"ok": False, "error": str(e)}


async def reply(token: str, text: str):
    """回覆 Line 訊息。"""
    async with httpx.AsyncClient() as client:
        await client.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
            json={"replyToken": token, "messages": [{"type": "text", "text": text}]}
        )


HELP = """OfficeCLI Line Bot

/create [檔名] - 建立文件
/cmd [指令] - 執行 officecli
/preview [檔名] - 預覽圖
/help - 說明

範例：
/create report.docx
/cmd add deck.pptx / --type slide --prop title="Hello"
/preview deck.pptx"""


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    
    for event in body.get("events", []):
        if event.get("type") != "message" or event["message"]["type"] != "text":
            continue
        
        text = event["message"]["text"].strip()
        token = event["replyToken"]
        
        logger.info("收到: %s", text)
        
        if text in ["/help", "help"]:
            await reply(token, HELP)
        
        elif text.startswith("/create "):
            filename = text[8:].strip()
            r = await call_api("/api/create", {"filename": filename})
            await reply(token, r.get("message") or r.get("error", "未知錯誤"))
        
        elif text.startswith("/cmd "):
            args = text[5:].strip().split()
            r = await call_api("/api/command", {"args": args})
            output = r.get("output") or r.get("error", "完成")
            await reply(token, output)
        
        elif text.startswith("/preview "):
            filename = text[9:].strip()
            r = await call_api("/api/preview", {"filename": filename})
            await reply(token, r.get("output") or r.get("error", "失敗"))
        
        else:
            await reply(token, "輸入 /help 查看說明")
    
    return {"status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    logger.info("Line Bot 啟動，API: %s", API_URL)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
