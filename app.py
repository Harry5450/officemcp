import os
import subprocess
import shutil
import json
import logging
import hashlib
import hmac
import base64
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
import uvicorn
import httpx

# 環境變數
PORT = int(os.environ.get("PORT", 8080))
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
WORK_DIR = Path("/app/output")
WORK_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

app = FastAPI()


def find_officecli() -> str:
    """尋找 officecli 二進位檔。"""
    candidates = [
        shutil.which("officecli"),
        "/usr/local/bin/officecli",
        "/root/.local/bin/officecli",
        "/home/app/.local/bin/officecli",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return "/usr/local/bin/officecli"


def run_officecli(args: list, timeout: int = 60) -> dict:
    """執行 officecli 指令。"""
    oc = find_officecli()
    cmd = [oc] + args
    env = {**os.environ, "PATH": f"{os.path.dirname(oc)}:{os.environ.get('PATH', '')}"}
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
    return {"status": "ok", "version": "3"}


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


@app.get("/files")
def list_files():
    """列出已建立的 Office 檔案。"""
    files = sorted(
        [f.name for f in WORK_DIR.iterdir()
         if f.is_file() and f.suffix.lower() in (".docx", ".xlsx", ".pptx", ".json", ".csv")]
    )
    return {"ok": True, "files": files}


@app.get("/diag-files")
def diag_files():
    """診斷：回傳實際檔名與 UTF-8 hex。"""
    out = []
    for f in WORK_DIR.iterdir():
        if f.is_file():
            out.append({"name": f.name, "hex": f.name.encode("utf-8").hex()})
    return {"ok": True, "files": out}


@app.get("/download/{filename}")
def download(filename: str):
    """下載伺服器上的檔案。"""
    fsafe = Path(filename).name
    path = WORK_DIR / fsafe
    if not path.is_file():
        return JSONResponse(status_code=404, content={"ok": False, "error": "檔案不存在"})
    return FileResponse(path, filename=fsafe)


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


def public_url(path: str) -> str:
    """用 Zeabur 網域建立公開 URL。"""
    domain = os.environ.get("PUBLIC_BASE_URL", "https://mcpoffice.zeabur.app")
    return f"{domain}{path}"


async def reply_file(token: str, filename: str):
    """透過 Line 回傳檔案訊息。"""
    if not LINE_TOKEN:
        return
    fsafe = Path(filename).name
    path = WORK_DIR / fsafe
    if not path.is_file():
        await reply_line(token, f"檔案不存在：{fsafe}")
        return
    async with httpx.AsyncClient() as c:
        await c.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
            json={
                "replyToken": token,
                "messages": [{
                    "type": "file",
                    "originalContentUrl": public_url(f"/download/{fsafe}"),
                    "fileName": fsafe,
                }]
            }
        )


async def reply_text_multiple(token: str, texts: list):
    if not LINE_TOKEN or not texts:
        return
    msgs = [{"type": "text", "text": t} for t in texts[:5]]
    async with httpx.AsyncClient() as c:
        await c.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
            json={"replyToken": token, "messages": msgs}
        )


HELP = """OfficeCLI Line Bot

/create [檔名] - 建立文件
/get [檔名] - 下載文件
/list - 列出檔案
/merge [模板] [輸出] [JSON] - 合併模板
/cmd [指令] - 執行 officecli
/help - 說明

範例：
/create report.docx
/get report.docx
/merge letter.docx out.docx {"name":"王小明"}
/cmd add deck.pptx / --type slide --prop title="Hello"
"""


def verify_line_signature(raw_body: bytes, signature: str) -> bool:
    """驗證 Line webhook 請求簽名（HMAC-SHA256 with channel secret）。
    未設定 LINE_SECRET 時跳過驗證，方便初次串接測試。"""
    if not LINE_SECRET:
        return True
    if not signature:
        return False
    expected = hmac.new(LINE_SECRET.encode(), raw_body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(expected).decode(), signature)


@app.post("/webhook")
async def webhook(request: Request):
    raw = await request.body()
    signature = request.headers.get("x-line-signature", "")
    if not verify_line_signature(raw, signature):
        return JSONResponse(status_code=403, content={"error": "invalid signature"})

    body = json.loads(raw.decode("utf-8"))
    for event in body.get("events", []):
        if event.get("type") != "message":
            continue
        token = event["replyToken"]

        # 檔案上傳：下載 Line message content 存到 WORK_DIR
        if event["message"]["type"] == "file":
            msg_id = event["message"]["id"]
            filename = event["message"].get("fileName", "upload.bin")
            fsafe = Path(filename).name
            async with httpx.AsyncClient() as c:
                r = await c.get(
                    f"https://api-data.line.me/v2/bot/message/{msg_id}/content",
                    headers={"Authorization": f"Bearer {LINE_TOKEN}"},
                )
                if r.status_code == 200:
                    (WORK_DIR / fsafe).write_bytes(r.content)
                    await reply_line(token, f"已接收並儲存：{fsafe}\n輸入 /list 查看或 /cmd 操作它")
                else:
                    await reply_line(token, f"下載檔案失敗：HTTP {r.status_code}")
            continue

        if event["message"]["type"] != "text":
            continue

        text = event["message"]["text"].strip()

        if text in ["/help", "help"]:
            await reply_line(token, HELP)
        elif text.startswith("/create "):
            filename = text[8:].strip()
            r = run_officecli(["create", filename])
            await reply_line(token, f"已建立：{filename}" if r["ok"] else f"失敗：{r['err']}")
        elif text.startswith("/get "):
            filename = text[5:].strip()
            await reply_file(token, filename)
        elif text in ["/list", "list"]:
            files = sorted([f.name for f in WORK_DIR.iterdir() if f.is_file()])
            await reply_line(token, "目前檔案：\n" + ("\n".join(files) if files else "（沒有檔案）"))
        elif text.startswith("/merge "):
            parts = text[7:].split()
            if len(parts) >= 3:
                template, output = parts[0], parts[1]
                data_json = " ".join(parts[2:])
                data_file = None
                # 若第三個參數是現有 JSON 檔路徑
                cand = WORK_DIR / data_json
                if cand.is_file():
                    data_file = cand
                args = ["merge", template, output, "--data", str(data_file) if data_file else data_json, "--force"]
                r = run_officecli(args)
                await reply_line(token, f"已合併：{output}" if r["ok"] else f"失敗：{r['err']}")
            else:
                await reply_line(token, "用法：/merge 模板 輸出 JSON\n範例：/merge letter.docx out.docx {\"name\":\"小明\"}")
        elif text.startswith("/cmd "):
            args = text[5:].strip().split()
            r = run_officecli(args)
            await reply_line(token, r["out"] or r["err"] or "完成")
        else:
            await reply_line(token, "輸入 /help 查看說明")

    return {"status": "ok"}


@app.get("/line-status")
async def line_status():
    """檢查 LINE bot 連線狀態。"""
    if not LINE_TOKEN:
        return {"ok": False, "error": "LINE_CHANNEL_ACCESS_TOKEN 未設定"}
    async with httpx.AsyncClient() as c:
        r = await c.get(
            "https://api.line.me/v2/bot/info",
            headers={"Authorization": f"Bearer {LINE_TOKEN}"},
        )
        if r.status_code == 200:
            return {"ok": True, "data": r.json()}
        return {"ok": False, "http": r.status_code, "error": r.text[:300]}


if __name__ == "__main__":
    logger.info("Starting on port %d", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
