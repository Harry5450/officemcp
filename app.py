import os
import subprocess
import shutil
import json
import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn
import httpx

# 環境變數
PORT = int(os.environ.get("PORT", 8080))
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
WORK_DIR = Path("/app/output")
WORK_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

app = FastAPI()


def run_officecli(args: list, timeout: int = 60) -> dict:
    """執行 officecli 指令。"""
    oc = shutil.which("officecli") or "/root/.local/bin/officecli"
    cmd = [oc] + args
    env = {**os.environ, "PATH": f"/root/.local/bin:{os.environ.get('PATH', '')}"}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(WORK_DIR), env=env)
        return {"ok": r.returncode == 0, "out": r.stdout.strip(), "err": r.stderr.strip()}
    except Exception as e:
        return {"ok": False, "out": "", "err": str(e)}


# --- API 端點 ---

@app.get("/")
def root():
    return {"status": "ok", "service": "OfficeCLI"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/create")
async def create(request: Request):
    data = await request.json()
    filename = data.get("filename", "")
    if not filename.endswith((".docx", ".xlsx", ".pptx")):
        return {"ok": False, "error": "僅支援 .docx, .xlsx, .pptx"}
    r = run_officecli(["create", filename])
    return {"ok": r["ok"], "message": f"已建立：{filename}" if r["ok"] else r["err"]}


@app.post("/command")
async def command(request: Request):
    data = await request.json()
    args = data.get("args", [])
    if not args:
        return {"ok": False, "error": "請提供指令"}
    r = run_officecli(args)
    return {"ok": r["ok"], "output": r["out"] or r["err"]}


# --- Line Webhook ---

async def reply_line(token: str, text: str):
    if not LINE_TOKEN:
        return
    async with httpx.AsyncClient() as c:
        await c.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
            json={"replyToken": token, "messages": [{"type": "text", "text": text}]}
        )


HELP = """OfficeCLI Line Bot

/create [檔名] - 建立文件
/cmd [指令] - 執行 officecli
/help - 說明

範例：
/create report.docx
/cmd add deck.pptx / --type slide --prop title="Hello"
"""


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    for event in body.get("events", []):
        if event.get("type") != "message" or event["message"]["type"] != "text":
            continue

        text = event["message"]["text"].strip()
        token = event["replyToken"]

        if text in ["/help", "help"]:
            await reply_line(token, HELP)
        elif text.startswith("/create "):
            filename = text[8:].strip()
            r = run_officecli(["create", filename])
            await reply_line(token, f"已建立：{filename}" if r["ok"] else f"失敗：{r['err']}")
        elif text.startswith("/cmd "):
            args = text[5:].strip().split()
            r = run_officecli(args)
            await reply_line(token, r["out"] or r["err"] or "完成")
        else:
            await reply_line(token, "輸入 /help 查看說明")

    return {"status": "ok"}


if __name__ == "__main__":
    logger.info("Starting on port %d", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
