import os
import asyncio
import subprocess
import shutil
import json
import logging
import hashlib
import hmac
import base64
import time
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
import uvicorn
import httpx

# 環境變數
PORT = int(os.environ.get("PORT", 8080))
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")
LINE_GROUP_ID = os.environ.get("LINE_GROUP_ID", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://mcpoffice.zeabur.app")
WORK_DIR = Path("/app/output")
WORK_DIR.mkdir(parents=True, exist_ok=True)

# 記憶型 log（最近 N 條 webhook 事件，供診斷）
EVENT_LOG: list = []
EVENT_LOG_MAX = 50
BOOT_TIME = int(time.time())

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
    import subprocess as sp
    git = ""
    try:
        git = sp.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    except Exception:
        git = ""
    return {"status": "ok", "version": "4", "boot": BOOT_TIME, "git": git}


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

async def send_line_message(token: str, messages: list):
    """送出 Line 訊息。
    push 目標優先：群組/房間 > 使用者（避免「不能傳給 bot 自己」的限制，
    因為 bot 擁有者的個人帳號無法用 API 收 push）。
    否則退回 reply（5 秒內、僅一次）。
    """
    if not LINE_TOKEN:
        return
    target = LINE_GROUP_ID or LINE_USER_ID
    async with httpx.AsyncClient() as c:
        if target:
            r = await c.post(
                "https://api.line.me/v2/bot/message/push",
                headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
                json={"to": target, "messages": messages},
            )
        else:
            r = await c.post(
                "https://api.line.me/v2/bot/message/reply",
                headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
                json={"replyToken": token, "messages": messages},
            )
    if r.status_code != 200:
        log_event("reply_error", f"HTTP {r.status_code}: {r.text[:200]}")


async def reply_line(token: str, text: str):
    await send_line_message(token, [{"type": "text", "text": text}])


def public_url(path: str) -> str:
    """用 Zeabur 網域建立公開 URL。"""
    return f"{PUBLIC_BASE_URL}{path}"


# --- 自然語言解析 ---

def parse_rule(text: str) -> dict | None:
    """規則式解析：從文字中抽取出 officecli 指令。傳回 None 表示無法解析。"""
    import re
    t = text.strip()
    low = t.lower()

    # 建立文件：請幫我建立 aaa.docx / 新增 bbb.xlsx 等
    KEYS = {
        "word": ".docx", "excel": ".xlsx", "powerpoint": ".pptx",
        "簡報": ".pptx", "投影片": ".pptx", "試算表": ".xlsx",
        "表格": ".xlsx", "文書": ".docx", "文件": ".docx", "doc": ".docx",
        "word": ".docx", "spreadsheet": ".xlsx",
    }
    # 先找「建立...（類型）...檔名」或「建立...檔名」
    m = re.search(r"(建立|新增|創建|製作|產生|生成|做|開|建)(?:一份|一個|個)?\s*"
                  r"([a-zA-Z0-9_\-\u4e00-\u9fff]{1,80}\.(?:docx|xlsx|pptx))", text)
    if not m:
        # 「建立一份 Word 文件叫報告.docx」或「建立一份 Excel 叫銷售」
        m = re.search(r"(建立|新增|創建|製作|產生|做|開|建).{0,16}?(word|excel|powerpoint|簡報|投影片|試算表|表格|文書|文件)"
                      r".{0,6}?(?:叫|命名為|名為)\s*([a-zA-Z0-9_\-\u4e00-\u9fff]{1,40}(?:\.[a-z0-9]{2,5})?)", text, re.IGNORECASE)
        if m and m.group(3):
            ext = KEYS.get(m.group(2).lower(), ".docx")
            fname = m.group(3)
            if not re.search(r"\.\w+$", fname):
                fname += ext
            return {"action": "create", "args": ["create", fname]}
        # 「建立一份 Word 文件」（未指定檔名，用類型當預設名）
        m = re.search(r"(建立|新增|創建|製作|產生|做|開|建).{0,8}?(word|excel|powerpoint|簡報|投影片|試算表|表格|文書|文件)(?!.{0,8}\.(?:docx|xlsx|pptx))(?!.{0,4}叫)", text, re.IGNORECASE)
        if m:
            base = m.group(2).title()
            return {"action": "create", "args": ["create", base + KEYS.get(m.group(2).lower(), ".docx")]}
    if m:
        return {"action": "create", "args": ["create", m.group(2)]}

    # 「建立一份簡報 deck.pptx / 一份試算表 budget.xlsx」類型名+檔名
    m = re.search(r"(建立|新增|創建|製作|產生|做|開|建).{0,6}?(簡報|投影片|試算表|表格|文書|word|excel|powerpoint|文件)"
                  r".{0,4}?\s*([a-zA-Z0-9_\-\u4e00-\u9fff]{1,40}\.(?:docx|xlsx|pptx))", text, re.IGNORECASE)
    if m:
        return {"action": "create", "args": ["create", m.group(3)]}

    # 建立文字檔
    m = re.search(r"(建立|新增|創建|做|建)(一份)?\s*([a-zA-Z0-9_\-\u4e00-\u9fff]{1,80}\.(txt|csv|json))", text)
    if m:
        return {"action": "create_text", "args": ["create", m.group(3)]}

    # 合併 / 套用模板
    m = re.search(r"(合併|套用模板|merge|填)(模板|template)?\s*([a-zA-Z0-9_\-\u4e00-\u9fff]+\.(docx|xlsx|pptx))\s*(?:成|至|為|輸出|存為|到)?\s*([a-zA-Z0-9_\-\u4e00-\u9fff]+\.\w+)?", text, re.IGNORECASE)
    if m:
        tmpl = m.group(3)
        out = m.group(5) or tmpl.replace(".", "_out.", 1)
        return {"action": "merge_open", "args": ["merge", tmpl, out, "--data"]}

    # 下載
    m = re.search(r"(下載|拿|給我|傳|取)\s*([a-zA-Z0-9_\-\u4e00-\u9fff]+\.\w+)", text)
    if m:
        return {"action": "download", "args": [m.group(2)]}

    # 列出檔案
    if re.search(r"(列出|有哪些|看看|查看|有.*檔案|list|檔案列表)", low) and re.search(r"檔案|文件|files", low, re.IGNORECASE):
        return {"action": "list", "args": []}

    # 加文字到文件
    m = re.search(r"([a-zA-Z0-9_\-\u4e00-\u9fff]+\.docx)\s*(?:裡|中)?\s*(?:請|幫我|麻煩|幫)?\s*(加|插入|新增|寫入|加入|加上|填)(?:入|上)?\s*(.{1,120}?)\s*$", text)
    if m:
        fname, content = m.group(1), m.group(3)
        return {"action": "add_text", "args": ["add", fname, "/", "--type", "paragraph", "--prop", f"text={content}"]}

    return None


def gemini_url() -> str:
    """建立 Gemini API URL，新格式 key 使用 x-goog-api-key header。"""
    return f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"


def gemini_headers() -> dict:
    """新版 AQ.A 格式 key 用 header 傳遞；舊 AIza 用 query param。"""
    if GEMINI_KEY.startswith("AIza"):
        return {"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY}
    return {"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY}


def gemini_payload(text: str, file_context: list) -> dict:
    sys_prompt = (
        "你是 officecli 指令轉換器。officecli 是操作 Office 文件的 CLI。"
        "使用者的訊息可能是自然語言請求，請判斷使用者想要執行的動作，"
        "並回覆一個 JSON 物件，格式為 {\"action\": \"create|add_text|merge|command|download|list\", "
        "\"args\": [\"officecli 參數陣列\", ...]}。"
        "只回覆 JSON，不要有任何額外文字。"
        "可用動作與對應指令：\n"
        "create: 建立檔案，args 形如 [\"create\", \"檔案.docx\"]\n"
        "add_text: 在文件加入文字段落，args 形如 [\"add\", \"檔案.docx\", \"/\", \"--type\", \"paragraph\", \"--prop\", \"text=內容\"]\n"
        "merge: 合併模板，args 形如 [\"merge\", \"模板.docx\", \"輸出.docx\", \"--data\", \"{\\\"name\\\":\\\"值\\\"}\", \"--force\"]\n"
        "command: 其他 officecli 指令\n"
        "download: 下載檔案，args 形如 [\"檔案.docx\"]\n"
        "list: 列出檔案，args 為 []\n"
    )
    files_str = "，".join(file_context) if file_context else "（目前沒有檔案）"
    user_prompt = f"使用者訊息：{text}\n伺服器上現有檔案：{files_str}\n請回覆動作 JSON。"
    return {
        "contents": [{"parts": [{"text": sys_prompt}, {"text": user_prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 300},
    }


async def ask_llm(text: str, file_context: list) -> dict | None:
    """呼叫 Gemini 將自然語言轉成 officecli 指令。失敗或無 key 時回傳 None。"""
    if not GEMINI_KEY:
        return None
    url = gemini_url()
    headers = gemini_headers()
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(url, headers=headers, json=gemini_payload(text, file_context))
            if r.status_code != 200:
                logger.error("Gemini error %s: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
            out = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            out = out.strip("`")
            if out.lower().startswith("json"):
                out = out[4:].lstrip()
            rj = json.loads(out)
            return {"action": rj.get("action"), "args": rj.get("args", [])}
    except Exception as e:
        logger.warning("Gemini fallback failed: %s", e)
        return None


async def reply_file(token: str, filename: str):
    """透過 Line 回傳檔案訊息。"""
    if not LINE_TOKEN:
        return
    fsafe = Path(filename).name
    path = WORK_DIR / fsafe
    if not path.is_file():
        await reply_line(token, f"檔案不存在：{fsafe}")
        return
    import urllib.parse
    dl_url = public_url(f"/download/{urllib.parse.quote(fsafe)}")
    await send_line_message(token, [{
        "type": "file",
        "originalContentUrl": dl_url,
        "fileName": fsafe,
    }])


def office_file_in_args(args: list) -> str | None:
    """從 officecli args 找出主要 Office 檔案名（第一個含副檔名的檔案）。"""
    for a in args:
        n = Path(str(a)).name
        if n.lower().endswith((".docx", ".xlsx", ".pptx")):
            return n
    return None


async def reply_result(token: str, r: dict, ok_msg: str, args: list | None = None):
    """執行結果回覆：一次 reply 送出文字 + 自動產生的檔案（若該動作產生文件）。"""
    if not LINE_TOKEN:
        return
    if not r["ok"]:
        await reply_line(token, f"失敗：{r['err']}")
        return
    messages = [{"type": "text", "text": ok_msg}]
    fname = office_file_in_args(args or [])
    if fname:
        # 確保 resident 快取已寫入磁碟（officecli save 強制 flush）
        run_officecli(["save", fname])
        path = WORK_DIR / Path(fname).name
        if path.is_file():
            import urllib.parse
            messages.append({
                "type": "file",
                "originalContentUrl": public_url(f"/download/{urllib.parse.quote(fname)}"),
                "fileName": Path(fname).name,
            })
    await send_line_message(token, messages)


async def reply_text_multiple(token: str, texts: list):
    if not LINE_TOKEN or not texts:
        return
    msgs = [{"type": "text", "text": t} for t in texts[:5]]
    await send_line_message(token, msgs)


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


async def handle_natural_language(token: str, text: str, raw: str):
    """規則式優先解析，失敗再由 LLM 判斷。"""
    files_now = [f.name for f in WORK_DIR.iterdir() if f.is_file()]

    # 第一步：規則式
    plan = parse_rule(text)
    if plan is None and GEMINI_KEY:
        plan = await ask_llm(text, files_now)

    if plan is None:
        await reply_line(token, "輸入 /help 查看指令，或直接告訴我你想要做什麼（例如「幫我建立 report.docx」）")
        return

    action = plan.get("action")
    args = plan.get("args", [])

    if action == "create":
        r = run_officecli(["create", args[0]] if len(args) == 1 else args)
        await reply_result(token, r, f"已建立：{args[0]}", ["create", args[0]])
    elif action == "create_text":
        fname = Path(args[0]).name
        (WORK_DIR / fname).touch()
        await reply_line(token, f"已建立：{fname}")
    elif action == "add_text":
        r = run_officecli(args)
        await reply_result(token, r, "已加入內容", args)
    elif action in ("merge", "merge_open"):
        # merge_open 尚未帶 data，提示需要資料
        if action == "merge_open":
            await reply_line(token, "請補上資料 JSON，例如：/merge letter.docx out.docx {\"name\":\"小明\"}")
            return
        temp, out = args[0], args[1]
        data = args[3] if len(args) > 3 else "{}"
        r = run_officecli(["merge", temp, out, "--data", data, "--force"])
        await reply_result(token, r, f"已合併：{out}", ["merge", out])
    elif action == "download":
        await reply_file(token, args[0])
    elif action == "list":
        files_now = [f.name for f in WORK_DIR.iterdir() if f.is_file()]
        await reply_line(token, "目前檔案：\n" + ("\n".join(files_now) if files_now else "（沒有檔案）"))
    elif action == "command":
        r = run_officecli(args)
        await reply_result(token, r, r["out"] or "完成", args)
    else:
        await reply_line(token, "無法判斷你想要的動作，請用 /help 查看指令")


def log_event(kind: str, detail: str):
    """記錄 webhook 事件（記憶型，供診斷）。"""
    import time as _t
    EVENT_LOG.append({"t": _t.strftime("%H:%M:%S"), "kind": kind, "detail": detail})
    del EVENT_LOG[:-EVENT_LOG_MAX]


@app.get("/debug-log")
def debug_log():
    """診斷：回傳最近 webhook 事件紀錄。"""
    return {"ok": True, "events": EVENT_LOG}


@app.post("/webhook")
async def webhook(request: Request):
    raw = await request.body()
    signature = request.headers.get("x-line-signature", "")
    if not verify_line_signature(raw, signature):
        return JSONResponse(status_code=403, content={"error": "invalid signature"})

    body = json.loads(raw.decode("utf-8"))
    for event in body.get("events", []):
        # 記錄 target（群組/房間優先，因為 bot 擁有者的個人帳號無法收 push）
        global LINE_GROUP_ID
        global LINE_USER_ID
        src = event.get("source", {})
        gid = src.get("groupId")
        rid = src.get("roomId")
        uid = src.get("userId")
        if gid:
            LINE_GROUP_ID = gid
        if rid:
            LINE_USER_ID = rid
        if uid and not (gid or rid):
            LINE_USER_ID = uid
        log_event("source", f"group={gid} room={rid} user={uid}")
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
        log_event("received", text)

        try:
            if text in ["/help", "help"]:
                await reply_line(token, HELP)
                log_event("handled", "help")
            elif text.startswith("/create "):
                filename = text[8:].strip()
                r = run_officecli(["create", filename])
                await reply_result(token, r, f"已建立：{filename}", ["create", filename])
                log_event("handled", f"create {filename} ok={r['ok']}")
            elif text.startswith("/get "):
                filename = text[5:].strip()
                await reply_file(token, filename)
                log_event("handled", f"get {filename}")
            elif text in ["/list", "list"]:
                files = sorted([f.name for f in WORK_DIR.iterdir() if f.is_file()])
                await reply_line(token, "目前檔案：\n" + ("\n".join(files) if files else "（沒有檔案）"))
                log_event("handled", "list")
            elif text.startswith("/merge "):
                parts = text[7:].split()
                if len(parts) >= 3:
                    template, output = parts[0], parts[1]
                    data_json = " ".join(parts[2:])
                    data_file = None
                    cand = WORK_DIR / data_json
                    if cand.is_file():
                        data_file = cand
                    args = ["merge", template, output, "--data", str(data_file) if data_file else data_json, "--force"]
                    r = run_officecli(args)
                    await reply_result(token, r, f"已合併：{output}", args)
                    log_event("handled", f"merge ok={r['ok']}")
                else:
                    await reply_line(token, "用法：/merge 模板 輸出 JSON\n範例：/merge letter.docx out.docx {\"name\":\"小明\"}")
            elif text.startswith("/cmd "):
                args = text[5:].strip().split()
                r = run_officecli(args)
                await reply_result(token, r, r["out"] or "完成", args)
                log_event("handled", f"cmd ok={r['ok']}")
            else:
                await handle_natural_language(token, text, event["message"]["text"])
                log_event("handled", "natural_language")
        except Exception as e:
            log_event("error", f"{type(e).__name__}: {e}")
            try:
                await reply_line(token, f"處理時發生錯誤：{type(e).__name__}: {e}")
            except Exception:
                pass

    return {"status": "ok"}


@app.get("/test-push")
async def test_push():
    """診斷：直接推一則測試訊息到已記錄的群組/使用者（驗證 push API 通路）。"""
    if not LINE_TOKEN:
        return {"ok": False, "error": "LINE_TOKEN 未設定"}
    target = LINE_GROUP_ID or LINE_USER_ID
    if not target:
        return {"ok": False, "error": "尚未取得 target，請先在 Line 傳一則訊息或把 bot 加入群組"}
    async with httpx.AsyncClient() as c:
        r = await c.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
            json={"to": target, "messages": [{"type": "text", "text": "測試 push 訊息 ✅ 若你看到這則，代表 push API 正常"}]},
        )
    return {"ok": r.status_code == 200, "http": r.status_code, "body": r.text[:300],
            "to": target, "to_type": "group" if LINE_GROUP_ID else "user"}


@app.get("/diag-env")
def diag_env():
    """診斷：檢查關鍵環境變數是否已設定（不顯示內容）。"""
    return {
        "ok": True,
        "LINE_TOKEN": bool(LINE_TOKEN),
        "LINE_SECRET": bool(LINE_SECRET),
        "GEMINI_KEY_set": bool(GEMINI_KEY),
        "GEMINI_KEY_prefix": GEMINI_KEY[:4] if GEMINI_KEY else "",
        "GEMINI_MODEL": GEMINI_MODEL,
    }


@app.get("/test-gemini")
async def test_gemini():
    """測試 Gemini API key 是否可用（不顯示 key）。"""
    if not GEMINI_KEY:
        return {"ok": False, "error": "GEMINI_API_KEY 未設定"}
    results = {}
    async with httpx.AsyncClient(timeout=20) as c:
        r1 = await c.post(gemini_url(), headers=gemini_headers(),
                          json={"contents": [{"parts": [{"text": "hi"}]}]})
        results["header_key"] = {"http": r1.status_code, "ok": r1.status_code == 200,
                                  "msg": (r1.text[:150] if r1.status_code != 200 else "works")}
        r2 = await c.post(gemini_url() + f"?key={GEMINI_KEY}",
                          headers={"Content-Type": "application/json"},
                          json={"contents": [{"parts": [{"text": "hi"}]}]})
        results["query_key"] = {"http": r2.status_code, "ok": r2.status_code == 200,
                                 "msg": (r2.text[:150] if r2.status_code != 200 else "works")}
        # 列出可用 model（grep flash 系列）
        try:
            r3 = await c.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=100",
                             headers=gemini_headers())
            if r3.status_code == 200:
                models = [m["name"].replace("models/", "") for m in r3.json().get("models", [])
                          if "flash" in m["name"] or "pro" in m["name"]]
                results["models"] = models[:20]
            else:
                results["models"] = f"list failed: {r3.status_code} {r3.text[:120]}"
        except Exception as e:
            results["models"] = f"list error: {e}"
    return {"ok": True, "key_prefix": GEMINI_KEY[:4], "current_model": GEMINI_MODEL, "tests": results}


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
