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
from contextvars import ContextVar
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
import uvicorn
import httpx

# 環境變數
PORT = int(os.environ.get("PORT", 8080))
LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "U3cd7fa54416c25e1472fd8b747a8ead2")
LINE_GROUP_ID = os.environ.get("LINE_GROUP_ID", "")
ADMIN_USER_IDS = {
    value.strip() for value in os.environ.get("ADMIN_USER_IDS", LINE_USER_ID).split(",") if value.strip()
}
ADMIN_API_TOKEN = os.environ.get("ADMIN_API_TOKEN", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
ZEN_KEY = os.environ.get("OPENCODE_ZEN_API_KEY", "")
ZEN_BASE_URL = os.environ.get("ZEN_BASE_URL", "https://opencode.ai/zen/v1").rstrip("/")
ZEN_MODEL = os.environ.get("ZEN_MODEL", "deepseek-v4-flash-free")
AI_PROVIDER = os.environ.get("AI_PROVIDER", "zen" if ZEN_KEY else "gemini").lower()
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://mcpoffice.zeabur.app")
WORK_DIR = Path("/app/output")
WORK_DIR.mkdir(parents=True, exist_ok=True)
WORKSPACE_ROOT = WORK_DIR / "workspaces"
SHARED_TEMPLATE_DIR = WORK_DIR / "_templates"
SHARED_TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_SECRET = os.environ.get("DOWNLOAD_SECRET", "") or LINE_SECRET or "local-download-secret"

# 每個 webhook request 都有自己的 workspace／LINE 目標，避免多人同時使用時互相污染。
ACTIVE_WORKSPACE_ID: ContextVar[str] = ContextVar("active_workspace_id", default="legacy")
ACTIVE_LINE_TARGET: ContextVar[str] = ContextVar("active_line_target", default="")
ACTIVE_LINE_SOURCE_KIND: ContextVar[str] = ContextVar("active_line_source_kind", default="")
ACTIVE_IS_ADMIN: ContextVar[bool] = ContextVar("active_is_admin", default=False)

# 記憶型 log（最近 N 條 webhook 事件，供診斷）
EVENT_LOG: list = []
EVENT_LOG_MAX = 50
BOOT_TIME = int(time.time())
WEBHOOK_STATUS = {
    "requests": 0,
    "signature_rejected": 0,
    "events": 0,
    "messages": 0,
    "last_event_type": "",
    "last_source_kind": "",
    "last_target_kind": "",
    "last_target_matches_configured_user": False,
    "last_delivery": "",
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

app = FastAPI()


def workspace_id_for_source(source: dict) -> str:
    """以 LINE 群組／聊天室／個人建立穩定但不暴露原始 ID 的 workspace key。"""
    # 保留既有管理員個人 root 檔案的相容性；其他 user／group 走隔離目錄。
    if source.get("userId") == LINE_USER_ID and not source.get("groupId") and not source.get("roomId"):
        return "legacy"
    for kind, key in (("group", "groupId"), ("room", "roomId"), ("user", "userId")):
        value = source.get(key)
        if value:
            digest = hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()[:24]
            return f"{kind}-{digest}"
    return "legacy"


def is_admin_source(source: dict) -> bool:
    return bool(source.get("userId") and source.get("userId") in ADMIN_USER_IDS)


def is_admin_api_request(request: Request) -> bool:
    provided = request.headers.get("x-admin-token", "")
    return bool(ADMIN_API_TOKEN and provided and hmac.compare_digest(provided, ADMIN_API_TOKEN))


def active_work_dir() -> Path:
    """取得目前 request 的工作目錄；legacy 保留既有 root 檔案相容性。"""
    workspace_id = ACTIVE_WORKSPACE_ID.get()
    if workspace_id == "legacy":
        return WORK_DIR
    path = WORKSPACE_ROOT / workspace_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def active_template_dir() -> Path:
    """取得目前 workspace 的個人範本目錄。"""
    path = active_work_dir() / "templates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_template_file(filename: str) -> Path | None:
    """依個人範本優先、共用範本其次解析範本檔案。"""
    fsafe = Path(filename).name
    if not fsafe or fsafe in (".", ".."):
        return None
    candidates = [
        active_template_dir() / fsafe,
        active_work_dir() / fsafe,
        SHARED_TEMPLATE_DIR / fsafe,
    ]
    for candidate in candidates:
        if candidate.is_file() and candidate.suffix.lower() in (".docx", ".xlsx", ".pptx"):
            return candidate
    return None


def available_templates() -> list[dict]:
    """列出目前 workspace 可使用的個人與共用範本，不暴露實體路徑。"""
    found = {}
    for source, folder in (("personal", active_template_dir()), ("shared", SHARED_TEMPLATE_DIR)):
        for path in folder.iterdir():
            if path.is_file() and path.suffix.lower() in (".docx", ".xlsx", ".pptx"):
                found.setdefault(path.name, {"name": path.name, "source": source})
    return sorted(found.values(), key=lambda item: item["name"].lower())


async def handle_template_command(token: str, text: str):
    """管理個人範本，以及由管理員發布不可由一般使用者覆蓋的共用範本。"""
    parts = text.split(maxsplit=2)
    if len(parts) < 3 or parts[1].lower() not in {"private", "personal", "publish"}:
        await reply_line(token, "用法：/template private 檔名.docx\n管理員發布共用範本：/template publish 檔名.docx")
        return
    mode, filename = parts[1].lower(), Path(parts[2]).name
    if Path(filename).suffix.lower() not in (".docx", ".xlsx", ".pptx"):
        await reply_line(token, "範本只支援 .docx、.xlsx 或 .pptx")
        return
    if mode == "publish" and not ACTIVE_IS_ADMIN.get():
        await reply_line(token, "只有管理員可以發布共用範本。")
        return
    source = active_work_dir() / filename
    if not source.is_file():
        source = active_template_dir() / filename
    if not source.is_file():
        await reply_line(token, f"找不到檔案：{filename}；請先把檔案上傳到 LINE。")
        return
    destination = SHARED_TEMPLATE_DIR / filename if mode == "publish" else active_template_dir() / filename
    if mode == "publish" and destination.exists():
        await reply_line(token, f"共用範本已存在：{filename}；請使用新的版本檔名再發布。")
        return
    shutil.copy2(source, destination)
    label = "共用" if mode == "publish" else "個人"
    await reply_line(token, f"已儲存{label}範本：{filename}")


def download_signature(filename: str, workspace_id: str, expires: int) -> str:
    payload = f"{workspace_id}\n{Path(filename).name}\n{expires}"
    return hmac.new(DOWNLOAD_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_download_token(filename: str) -> str:
    expires = int(time.time()) + 3600
    workspace_id = ACTIVE_WORKSPACE_ID.get()
    signature = download_signature(filename, workspace_id, expires)
    raw = f"{workspace_id}|{Path(filename).name}|{expires}|{signature}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def verify_download_token(filename: str, token: str) -> str | None:
    if not token:
        return None
    try:
        padding = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode((token + padding).encode("ascii")).decode("utf-8")
        workspace_id, token_filename, expires_text, signature = raw.split("|", 3)
        expires = int(expires_text)
        if token_filename != Path(filename).name or expires < int(time.time()):
            return None
        expected = download_signature(token_filename, workspace_id, expires)
        if not hmac.compare_digest(signature, expected):
            return None
        return workspace_id
    except (ValueError, UnicodeError):
        return None


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
    work_dir = active_work_dir()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(work_dir), env=env)
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
    return {
        "status": "ok",
        "version": "4",
        "boot": BOOT_TIME,
        "git": git,
        "line_token_set": bool(LINE_TOKEN),
        "line_secret_set": bool(LINE_SECRET),
        "webhook": dict(WEBHOOK_STATUS),
    }


@app.post("/create")
async def create(request: Request):
    if not is_admin_api_request(request):
        return JSONResponse(status_code=403, content={"ok": False, "error": "管理員 API 權限不足"})
    data = await request.json()
    filename = data.get("filename", "")
    if not filename.endswith((".docx", ".xlsx", ".pptx")):
        return {"ok": False, "error": "僅支援 .docx, .xlsx, .pptx"}
    r = run_officecli(["create", filename])
    return {"ok": r["ok"], "message": f"已建立：{filename}" if r["ok"] else r["err"]}


@app.post("/command")
async def command(request: Request):
    if not is_admin_api_request(request):
        return JSONResponse(status_code=403, content={"ok": False, "error": "管理員 API 權限不足"})
    data = await request.json()
    args = data.get("args", [])
    if not args:
        return {"ok": False, "error": "請提供指令"}
    r = run_officecli(args)
    return {"ok": r["ok"], "output": r["out"] or r["err"]}


@app.get("/files")
def list_files(request: Request):
    """列出已建立的 Office 檔案。"""
    if not is_admin_api_request(request):
        return JSONResponse(status_code=403, content={"ok": False, "error": "管理員 API 權限不足"})
    work_dir = active_work_dir()
    files = sorted(
        [f.name for f in work_dir.iterdir()
         if f.is_file() and f.suffix.lower() in (".docx", ".xlsx", ".pptx", ".json", ".csv")]
    )
    return {"ok": True, "files": files}


@app.get("/templates")
def list_templates(request: Request):
    """列出目前 workspace 可使用的個人／共用範本名稱，不回傳實體路徑。"""
    if not is_admin_api_request(request):
        return JSONResponse(status_code=403, content={"ok": False, "error": "管理員 API 權限不足"})
    return {"ok": True, "workspace": ACTIVE_WORKSPACE_ID.get(), "templates": available_templates()}


@app.get("/diag-files")
def diag_files(request: Request):
    """診斷：回傳實際檔名與 UTF-8 hex。"""
    if not is_admin_api_request(request):
        return JSONResponse(status_code=403, content={"ok": False, "error": "管理員 API 權限不足"})
    out = []
    for f in active_work_dir().iterdir():
        if f.is_file():
            out.append({"name": f.name, "hex": f.name.encode("utf-8").hex()})
    return {"ok": True, "files": out}


@app.get("/download/{filename}")
def download(filename: str, token: str = ""):
    """下載伺服器上的檔案。"""
    fsafe = Path(filename).name
    workspace_id = verify_download_token(fsafe, token)
    if not workspace_id:
        return JSONResponse(status_code=403, content={"ok": False, "error": "下載連結無效或已過期"})
    path = WORK_DIR / fsafe if workspace_id == "legacy" else WORKSPACE_ROOT / workspace_id / fsafe
    if not path.is_file():
        return JSONResponse(status_code=404, content={"ok": False, "error": "檔案不存在"})
    return FileResponse(path, filename=fsafe)


# --- Line Webhook ---

async def send_line_message(token: str, messages: list):
    """送出 Line 訊息。
    push 目標優先：群組/房間 > 使用者（避免「不能傳給 bot 自己」的限制，
    因為 bot 擁有者的個人帳號無法用 API 收 push）。
    push 明確失敗時退回 reply（reply token 仍有效時可避免完全無回應）。
    """
    if not LINE_TOKEN:
        WEBHOOK_STATUS["last_delivery"] = "line_token_missing"
        log_event("reply_error", "LINE_TOKEN 未設定")
        return
    target = ACTIVE_LINE_TARGET.get() or LINE_GROUP_ID or LINE_USER_ID
    headers = {"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient() as c:
            if target:
                r = await c.post(
                    "https://api.line.me/v2/bot/message/push",
                    headers=headers,
                    json={"to": target, "messages": messages},
                )
                if r.status_code == 200:
                    WEBHOOK_STATUS["last_delivery"] = "push_ok"
                    return
                WEBHOOK_STATUS["last_delivery"] = f"push_http_{r.status_code}"
                log_event("push_error", f"HTTP {r.status_code}: {r.text[:200]}")
                if not token:
                    return
                r = await c.post(
                    "https://api.line.me/v2/bot/message/reply",
                    headers=headers,
                    json={"replyToken": token, "messages": messages},
                )
            else:
                r = await c.post(
                    "https://api.line.me/v2/bot/message/reply",
                    headers=headers,
                    json={"replyToken": token, "messages": messages},
                )
            if r.status_code != 200:
                WEBHOOK_STATUS["last_delivery"] = f"reply_http_{r.status_code}"
                log_event("reply_error", f"HTTP {r.status_code}: {r.text[:200]}")
            else:
                WEBHOOK_STATUS["last_delivery"] = "reply_ok"
    except Exception as e:
        WEBHOOK_STATUS["last_delivery"] = "exception"
        log_event("reply_error", f"{type(e).__name__}: {e}")


async def reply_line(token: str, text: str):
    await send_line_message(token, [{"type": "text", "text": text}])


def public_url(path: str) -> str:
    """用 Zeabur 網域建立公開 URL。"""
    return f"{PUBLIC_BASE_URL}{path}"


def latest_office_file() -> str | None:
    """取得最近修改的 Office 檔案，供「回傳剛剛建立的檔案」使用。"""
    files = [
        f for f in active_work_dir().iterdir()
        if f.is_file() and f.suffix.lower() in (".docx", ".xlsx", ".pptx")
    ]
    if not files:
        return None
    return max(files, key=lambda f: f.stat().st_mtime).name


def document_for_request(text: str) -> str | None:
    """從訊息找出要處理的 DOCX；沒有明寫檔名時使用最近上傳／建立的 DOCX。"""
    import re
    low = text.lower()
    docs = sorted(
        [f for f in active_work_dir().iterdir() if f.is_file() and f.suffix.lower() == ".docx"],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    for f in docs:
        if f.name.lower() in low:
            return f.name
    if docs and len(docs) == 1:
        return docs[0].name
    if docs and re.search(r"(這份|這個|剛上傳|剛剛|上傳的|目前的)\s*(文件|檔案|word)?", text):
        return docs[0].name
    return None


def is_document_content_request(text: str) -> bool:
    """判斷是否要求 AI 閱讀、摘要、分析或改寫文件內容。"""
    import re
    return bool(re.search(
        r"(摘要|總結|重點|整理|歸納|分析|閱讀|讀取|讀懂|改寫|潤稿|正式版|白話|校對|內容分析|整理成)",
        text,
    ))


def read_docx_content(filename: str, max_chars: int = 12000) -> dict:
    """以標準庫讀取 DOCX 段落與表格內文字，不改動原檔。"""
    import zipfile
    import xml.etree.ElementTree as ET

    fsafe = Path(filename).name
    path = active_work_dir() / fsafe
    if not path.is_file() or path.suffix.lower() != ".docx":
        return {"ok": False, "error": f"找不到 Word 文件：{fsafe}"}
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml")
        root = ET.fromstring(xml)
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paragraphs = []
        for paragraph in root.iter(f"{ns}p"):
            pieces = []
            for node in paragraph.iter():
                if node.tag == f"{ns}t":
                    pieces.append(node.text or "")
                elif node.tag == f"{ns}tab":
                    pieces.append("\t")
                elif node.tag == f"{ns}br":
                    pieces.append("\n")
            value = "".join(pieces).strip()
            if value:
                paragraphs.append(value)
        content = "\n".join(paragraphs).strip()
        if not content:
            return {"ok": False, "error": f"文件沒有可讀取的文字：{fsafe}"}
        truncated = len(content) > max_chars
        if truncated:
            content = content[:max_chars].rstrip() + "\n[文件內容過長，以上為前段內容]"
        return {"ok": True, "filename": fsafe, "content": content, "truncated": truncated}
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as e:
        logger.warning("DOCX read failed for %s: %s", fsafe, e)
        return {"ok": False, "error": f"無法讀取 Word 文件：{fsafe}"}


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

    # 「你要回傳給我」「把剛剛的檔案傳回來」等後續訊息：回傳最近建立的檔案。
    # 這讓 LINE 對話不必重複輸入檔名，也避免把無檔名的「回傳」交給 LLM 猜測。
    if re.search(r"(回傳|傳回|傳給我|發給我|下載剛才|下載剛剛|剛才.*檔案|剛剛.*檔案)", text):
        latest = latest_office_file()
        if latest:
            return {"action": "download", "args": [latest]}

    # 查詢目前 workspace 可用的個人／共用範本。
    if re.search(r"(範本|模板)", text) and re.search(r"(有哪些|可用|列出|查看|列表|目錄)", text):
        return {"action": "templates", "args": []}

    # 列出檔案
    if re.search(r"(列出|有哪些|看看|查看|有.*檔案|list|檔案列表)", low) and re.search(r"檔案|文件|files", low, re.IGNORECASE):
        return {"action": "list", "args": []}

    # --- 編輯既有文件 ---

    # 檔名模式：只允許行首或「空白/的/在/裡/中/把/將」之後接檔名，
    # 避免把「幫我在」這種前置字一起吃進檔名
    fname_pat = r"(?:^|(?<=[\s的在中裡把將]))([a-zA-Z0-9_\-\u4e00-\u9fff]+\.(?:docx|xlsx|pptx))"

    # 若檔名被「幫我在」等前置字污染，剝掉常見前綴
    def clean_fname(s: str) -> str:
        s = re.sub(r"^(幫我|請|麻煩|在|把|將|中的|裡面的)+", "", s)
        return s

    # 標題/heading：在 X 加入標題 標題文字（放在 add_text 前，避免被吃掉）
    m = re.search(fname_pat + r"\s*(?:裡|中)?\s*(?:請|幫我|麻煩|可不可以|能不能)?\s*(加|插入|新增|寫入|加入|加上|填|補)(?:入)?\s*(標題|標題列|heading|title)\s*(?::|：)?\s*(.{1,200}?)\s*$", text, re.IGNORECASE)
    if m:
        fname, content = m.group(1), m.group(4).strip()
        return {"action": "add_title", "args": ["add", clean_fname(fname), "/", "--type", "heading", "--prop", f"text={content}"]}

    # 「把 X.docx 內容改成/改為/換成 新內容」→ 修改第一段
    m = re.search(r"(?:把|將)?\s*" + fname_pat + r"\s*(?:的|裡面的|裡)?\s*(?:內容|文字|文字內容)?\s*(改為|改成|換成|更新成|覆蓋|覆寫|改)\s*(?:成|為)?\s*(.{1,200}?)\s*$", text)
    if m:
        fname, content = m.group(1), m.group(3).strip()
        content = re.sub(r"^(內容是|內容為|說|講|寫)\s*", "", content)
        return {"action": "replace_text", "args": ["set", clean_fname(fname), "/body/p[1]", "--prop", f"text={content}", "--force"]}

    # 加文字/段落/內容：把 X.docx 加上/加入/寫入 內容；在 X 中加一句...
    m = re.search(fname_pat + r"\s*(?:裡|中)?\s*(?:請|幫我|麻煩|可不可以|能不能)?\s*(加|插入|新增|寫入|加入|加上|填|補)(?:入|上|一句話|一段話|一句|一段|個段落|一段文字)?\s*(?::|：)?\s*(.{1,200}?)\s*$", text)
    if m:
        fname, content = m.group(1), m.group(3).strip()
        # 若內容含「說/講/寫」當引導詞，吃掉它
        content = re.sub(r"^(說|講|寫|內容是|內容為|打)\s*", "", content)
        return {"action": "add_text", "args": ["add", clean_fname(fname), "/", "--type", "paragraph", "--prop", f"text={content}"]}

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
        "並回覆一個 JSON 物件，格式為 {\"action\": \"create|add_text|add_title|replace_text|merge|command|download|list|templates\", "
        "\"args\": [\"officecli 參數陣列\", ...]}。"
        "只回覆 JSON，不要有任何額外文字。"
        "可用動作與對應指令：\n"
        "create: 建立檔案，args 形如 [\"create\", \"檔案.docx\"]\n"
        "add_text: 在文件加入文字段落，args 形如 [\"add\", \"檔案.docx\", \"/\", \"--type\", \"paragraph\", \"--prop\", \"text=內容\"]\n"
        "add_title: 在文件加入標題，args 形如 [\"add\", \"檔案.docx\", \"/\", \"--type\", \"heading\", \"--prop\", \"text=標題\"]\n"
        "replace_text: 修改/覆寫文件內容，args 形如 [\"set\", \"檔案.docx\", \"/body/p[1]\", \"--prop\", \"text=新內容\", \"--force\"]\n"
        "merge: 合併模板，args 形如 [\"merge\", \"模板.docx\", \"輸出.docx\", \"--data\", \"{\\\"name\\\":\\\"值\\\"}\", \"--force\"]\n"
        "command: 其他 officecli 指令\n"
        "download: 下載檔案，args 形如 [\"檔案.docx\"]\n"
        "list: 列出檔案，args 為 []\n"
        "templates: 列出可用範本，args 為 []\n"
    )
    files_str = "，".join(file_context) if file_context else "（目前沒有檔案）"
    user_prompt = f"使用者訊息：{text}\n伺服器上現有檔案：{files_str}\n請回覆動作 JSON。"
    return {
        "contents": [{"parts": [{"text": sys_prompt}, {"text": user_prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 300},
    }


def zen_url() -> str:
    """OpenCode Zen 的 OpenAI-compatible Chat Completions endpoint。"""
    return f"{ZEN_BASE_URL}/chat/completions"


def zen_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ZEN_KEY}",
    }


def zen_payload(text: str, file_context: list) -> dict:
    """建立 Zen 使用的 OpenAI-compatible 請求。"""
    sys_prompt = (
        "你是 officecli 指令轉換器。officecli 是操作 Office 文件的 CLI。"
        "判斷使用者的自然語言需求，並只回覆 JSON 物件。"
        "格式為 {\"action\": \"create|add_text|add_title|replace_text|merge|download|list|templates\", "
        "\"args\": [\"officecli 參數陣列\", ...]}。"
        "不要回覆 Markdown、解釋或其他文字。"
        "create: [\"create\", \"檔案.docx\"]；"
        "add_text: [\"add\", \"檔案.docx\", \"/\", \"--type\", \"paragraph\", \"--prop\", \"text=內容\"]；"
        "add_title: [\"add\", \"檔案.docx\", \"/\", \"--type\", \"heading\", \"--prop\", \"text=標題\"]；"
        "replace_text: [\"set\", \"檔案.docx\", \"/body/p[1]\", \"--prop\", \"text=新內容\", \"--force\"]；"
        "merge: [\"merge\", \"模板.docx\", \"輸出.docx\", \"--data\", \"{\\\"name\\\":\\\"值\\\"}\", \"--force\"]；"
        "download: [\"檔案.docx\"]；list: []；templates: []。"
    )
    files_str = "，".join(file_context) if file_context else "（目前沒有檔案）"
    return {
        "model": ZEN_MODEL,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"使用者訊息：{text}\n伺服器上現有檔案：{files_str}"},
        ],
        "temperature": 0.1,
        "max_tokens": 300,
    }


def parse_json_object(output: str) -> dict | None:
    """從 LLM 回覆取出 JSON 物件，容忍 code fence 或前後說明文字。"""
    if not isinstance(output, str):
        return None
    out = output.strip()
    if out.startswith("```"):
        out = out.split("\n", 1)[1] if "\n" in out else out[3:]
        if out.rstrip().endswith("```"):
            out = out.rstrip()[:-3].rstrip()
    if out.lower().startswith("json"):
        out = out[4:].lstrip()
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        # 容忍模型前後多出一句話，但只取最外層 JSON 物件。
        start, end = out.find("{"), out.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(out[start:end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    return data


def parse_llm_plan(output: str) -> dict | None:
    """解析並限制 LLM 回傳的動作，避免模型輸出說明文字或任意指令。"""
    data = parse_json_object(output)
    if data is None:
        return None
    action = data.get("action")
    args = data.get("args", [])
    allowed = {"create", "create_text", "add_text", "add_title", "replace_text", "merge", "download", "list", "templates"}
    if action not in allowed or not isinstance(args, list):
        return None
    return {"action": action, "args": [str(a) for a in args]}


def document_ai_prompts(request: str, filename: str, document_text: str) -> tuple[str, str]:
    """建立 Word 內容處理的 system/user prompt。"""
    system = (
        "你是繁體中文 Word 文件內容助理。請根據文件原文與使用者要求處理內容，"
        "不可捏造原文沒有的事實。只回覆 JSON，格式為 "
        "{\"mode\":\"reply|create_document\",\"filename\":\"檔名.docx\",\"content\":\"內容\"}。"
        "若使用者只要摘要、重點或分析，mode 使用 reply，filename 留空；"
        "若使用者要求整理成 Word、另存新檔或產生新版，mode 使用 create_document，"
        "filename 使用安全的 .docx 檔名，且不要覆蓋原檔。"
        "content 要是可直接給使用者閱讀或放進 Word 的繁體中文內容，"
        "不要輸出 Markdown code fence 或 JSON 以外的文字。"
    )
    user = f"使用者要求：{request}\n原始檔案：{filename}\n文件內容：\n{document_text}"
    return system, user


def zen_document_payload(request: str, filename: str, document_text: str) -> dict:
    system, user = document_ai_prompts(request, filename, document_text)
    return {
        "model": ZEN_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 1600,
    }


def gemini_document_payload(request: str, filename: str, document_text: str) -> dict:
    system, user = document_ai_prompts(request, filename, document_text)
    return {
        "contents": [{"parts": [{"text": system}, {"text": user}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1600},
    }


def parse_document_plan(output: str) -> dict | None:
    """解析文件內容 AI 的回覆。"""
    data = parse_json_object(output)
    if data is None:
        return None
    mode = data.get("mode")
    content = data.get("content")
    if mode not in {"reply", "create_document"} or not isinstance(content, str) or not content.strip():
        return None
    requested = data.get("filename", "")
    filename = Path(str(requested)).name if requested else ""
    if filename and not filename.lower().endswith(".docx"):
        filename += ".docx"
    return {"mode": mode, "filename": filename, "content": content.strip()}


async def ask_zen(text: str, file_context: list) -> dict | None:
    """呼叫 OpenCode Zen DeepSeek 將自然語言轉成 officecli 計畫。"""
    if not ZEN_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(zen_url(), headers=zen_headers(), json=zen_payload(text, file_context))
            if r.status_code != 200:
                logger.error("OpenCode Zen error %s: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
            out = data["choices"][0]["message"]["content"]
            return parse_llm_plan(out)
    except Exception as e:
        logger.warning("OpenCode Zen fallback failed: %s", e)
        return None


async def ask_gemini(text: str, file_context: list) -> dict | None:
    """呼叫 Gemini 將自然語言轉成 officecli 計畫。"""
    if not GEMINI_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(gemini_url(), headers=gemini_headers(), json=gemini_payload(text, file_context))
            if r.status_code != 200:
                logger.error("Gemini error %s: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
            out = data["candidates"][0]["content"]["parts"][0]["text"]
            return parse_llm_plan(out)
    except Exception as e:
        logger.warning("Gemini fallback failed: %s", e)
        return None


async def ask_zen_document(request: str, filename: str, document_text: str) -> dict | None:
    """用 OpenCode Zen 理解 DOCX 內容，回傳摘要或新文件內容。"""
    if not ZEN_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=45) as c:
            r = await c.post(
                zen_url(),
                headers=zen_headers(),
                json=zen_document_payload(request, filename, document_text),
            )
            if r.status_code != 200:
                logger.error("OpenCode Zen document error %s: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
            out = data["choices"][0]["message"]["content"]
            return parse_document_plan(out)
    except Exception as e:
        logger.warning("OpenCode Zen document failed: %s", e)
        return None


async def ask_gemini_document(request: str, filename: str, document_text: str) -> dict | None:
    """用 Gemini 理解 DOCX 內容，作為 Zen 的備援。"""
    if not GEMINI_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=45) as c:
            r = await c.post(
                gemini_url(),
                headers=gemini_headers(),
                json=gemini_document_payload(request, filename, document_text),
            )
            if r.status_code != 200:
                logger.error("Gemini document error %s: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
            out = data["candidates"][0]["content"]["parts"][0]["text"]
            return parse_document_plan(out)
    except Exception as e:
        logger.warning("Gemini document failed: %s", e)
        return None


async def ask_document_ai(request: str, filename: str, document_text: str) -> dict | None:
    """依 provider 順序處理 DOCX 內容，第一個失敗會使用另一個 provider。"""
    if AI_PROVIDER in ("gemini", "google"):
        providers = [("Gemini", ask_gemini_document), ("OpenCode Zen", ask_zen_document)]
    else:
        providers = [("OpenCode Zen", ask_zen_document), ("Gemini", ask_gemini_document)]
    for name, caller in providers:
        plan = await caller(request, filename, document_text)
        if plan is not None:
            logger.info("document AI provider=%s mode=%s", name, plan.get("mode"))
            return plan
    return None


async def ask_llm(text: str, file_context: list) -> dict | None:
    """依設定呼叫 Zen／Gemini；第一個失敗會自動嘗試另一個 provider。"""
    if AI_PROVIDER in ("gemini", "google"):
        providers = [("Gemini", ask_gemini), ("OpenCode Zen", ask_zen)]
    else:
        providers = [("OpenCode Zen", ask_zen), ("Gemini", ask_gemini)]
    for name, caller in providers:
        plan = await caller(text, file_context)
        if plan is not None:
            logger.info("LLM plan provider=%s action=%s", name, plan.get("action"))
            return plan
    return None


async def reply_file(token: str, filename: str):
    """透過 Line 回傳檔案（以文字 + 下載連結，相容免費方案）。"""
    if not LINE_TOKEN:
        return
    fsafe = Path(filename).name
    path = active_work_dir() / fsafe
    if not path.is_file():
        await reply_line(token, f"檔案不存在：{fsafe}")
        return
    import urllib.parse
    dl_token = make_download_token(fsafe)
    dl_url = public_url(
        f"/download/{urllib.parse.quote(fsafe, safe='')}?token={urllib.parse.quote(dl_token, safe='')}"
    )
    await send_line_message(token, [
        {"type": "text", "text": f"📎 {fsafe}\n\n下載：{dl_url}"}
    ])


def office_file_in_args(args: list) -> str | None:
    """從 officecli args 找出主要 Office 檔案名（第一個含副檔名的檔案）。"""
    for a in args:
        n = Path(str(a)).name
        if n.lower().endswith((".docx", ".xlsx", ".pptx")):
            return n
    return None


async def reply_result(token: str, r: dict, ok_msg: str, args: list | None = None):
    """執行結果回覆：文字 + 下載連結（免費方案不支援 type=file）。"""
    if not LINE_TOKEN:
        return
    if not r["ok"]:
        await reply_line(token, f"失敗：{r['err']}")
        return
    # 成功與下載連結合併成同一則訊息，避免 LINE 端只顯示第一則 push。
    message_text = ok_msg
    fname = office_file_in_args(args or [])
    if fname:
        # 確保 resident 快取已寫入磁碟（officecli save 強制 flush）
        run_officecli(["save", fname])
        path = active_work_dir() / Path(fname).name
        if path.is_file():
            import urllib.parse
            dl_token = make_download_token(fname)
            dl_url = public_url(
                f"/download/{urllib.parse.quote(fname, safe='')}?token={urllib.parse.quote(dl_token, safe='')}"
            )
            message_text += f"\n\n📎 {Path(fname).name}\n\n下載：{dl_url}"
        else:
            message_text += f"\n\n檔案已建立，但目前找不到下載檔：{Path(fname).name}"
    await send_line_message(token, [{"type": "text", "text": message_text}])


async def reply_text_multiple(token: str, texts: list):
    if not LINE_TOKEN or not texts:
        return
    msgs = [{"type": "text", "text": t} for t in texts[:5]]
    await send_line_message(token, msgs)


def split_line_text(text: str, limit: int = 4500) -> list[str]:
    """將 AI 長文切成 LINE 可接受的數段，保留段落邊界。"""
    if len(text) <= limit:
        return [text]
    chunks = []
    current = ""
    for paragraph in text.splitlines(keepends=True):
        if current and len(current) + len(paragraph) > limit:
            chunks.append(current.rstrip())
            current = ""
        if len(paragraph) > limit:
            while len(paragraph) > limit:
                chunks.append(paragraph[:limit].rstrip())
                paragraph = paragraph[limit:]
        current += paragraph
    if current.strip():
        chunks.append(current.rstrip())
    return chunks or [text[:limit]]


async def reply_document_ai_text(token: str, filename: str, content: str):
    """回傳文件摘要／分析結果，必要時切成多則 LINE 訊息。"""
    chunks = split_line_text(f"📄 {filename}\n\n{content}")
    await reply_text_multiple(token, chunks)


def document_output_filename(source_filename: str, requested: str = "") -> str:
    """產生不覆蓋原檔的安全 DOCX 輸出檔名。"""
    import re
    source = Path(source_filename).name
    candidate = Path(requested).name if requested else f"{Path(source).stem}_整理.docx"
    if not candidate.lower().endswith(".docx"):
        candidate += ".docx"
    stem = re.sub(r'[<>:"/\\|?*]', "_", Path(candidate).stem).strip(" .")
    if not stem:
        stem = f"{Path(source).stem}_整理"
    candidate = f"{stem}.docx"
    if candidate.lower() == source.lower():
        candidate = f"{stem}_整理.docx"
    base = Path(candidate).stem
    suffix = 2
    while (active_work_dir() / candidate).exists():
        candidate = f"{base}_{suffix}.docx"
        suffix += 1
    return candidate


async def handle_document_content(token: str, request_text: str, source_filename: str):
    """讀取 DOCX，交給 AI 摘要／整理，並保留原檔產出新檔。"""
    doc = read_docx_content(source_filename)
    if not doc["ok"]:
        await reply_line(token, doc["error"])
        return
    plan = await ask_document_ai(request_text, source_filename, doc["content"])
    if plan is None:
        await reply_line(token, "目前無法處理這份 Word 文件，請稍後再試；原始檔案沒有被修改。")
        return
    if plan["mode"] == "reply":
        await reply_document_ai_text(token, source_filename, plan["content"])
        return

    output_filename = document_output_filename(source_filename, plan.get("filename", ""))
    result = run_officecli(["create", output_filename])
    if not result["ok"]:
        await reply_line(token, f"建立整理後文件失敗：{result['err']}")
        return
    lines = [line.strip() for line in plan["content"].splitlines() if line.strip()]
    for line in lines[:120]:
        result = run_officecli([
            "add", output_filename, "/", "--type", "paragraph", "--prop", f"text={line}"
        ])
        if not result["ok"]:
            await reply_line(token, f"寫入整理內容失敗：{result['err']}")
            return
    await reply_result(token, result, f"已整理並建立：{output_filename}", ["create", output_filename])


HELP = """OfficeCLI Line Bot

/create [檔名] - 建立文件
/get [檔名] - 下載文件
/list - 列出檔案
/template private [檔名] - 儲存個人範本
/template publish [檔名] - 管理員發布共用範本
/merge [模板] [輸出] [JSON] - 合併模板
/cmd [指令] - 執行 officecli（僅管理員）
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
    import re

    # 文件內容任務先走「讀取內容 → AI → 摘要／另存新檔」流程，
    # 不讓一般 officecli 規則把「整理／摘要」誤當成單純改第一段。
    if is_document_content_request(text):
        source_filename = document_for_request(text)
        if source_filename:
            await handle_document_content(token, text, source_filename)
            return
        if re.search(r"(這份|這個|上傳|剛剛|摘要|讀取|分析|改寫|原文)", text):
            await reply_line(token, "請先上傳要處理的 Word 文件，再告訴我想摘要、整理或改寫什麼。")
            return

    files_now = [f.name for f in active_work_dir().iterdir() if f.is_file()]

    # 第一步：規則式
    plan = parse_rule(text)
    if plan is None and (ZEN_KEY or GEMINI_KEY):
        plan = await ask_llm(text, files_now)

    if plan is None:
        await reply_line(token, "輸入 /help 查看指令，或直接告訴我你想要做什麼（例如「幫我建立 report.docx」）")
        return

    action = plan.get("action")
    args = plan.get("args", [])

    if action == "create":
        # 規則與 LLM 都可能回傳 ["create", "檔名"]；舊版誤把 args[0]
        # 當檔名，因此截圖中會顯示「已建立：create」。
        cli_args = ["create", args[0]] if len(args) == 1 else args
        fname = office_file_in_args(cli_args)
        if not fname:
            await reply_line(token, "請提供要建立的檔名，例如：幫我建立一份 Word 文件叫會議紀錄")
            return
        r = run_officecli(cli_args)
        await reply_result(token, r, f"已建立：{fname}", cli_args)
    elif action == "create_text":
        fname = Path(args[0]).name
        (active_work_dir() / fname).touch()
        await reply_line(token, f"已建立：{fname}")
    elif action == "add_text":
        r = run_officecli(args)
        await reply_result(token, r, "已加入內容", args)
    elif action == "add_title":
        r = run_officecli(args)
        await reply_result(token, r, "已加入標題", args)
    elif action == "replace_text":
        r = run_officecli(args)
        await reply_result(token, r, "已更新內容", args)
    elif action in ("merge", "merge_open"):
        # merge_open 尚未帶 data，提示需要資料
        if action == "merge_open":
            await reply_line(token, "請補上資料 JSON，例如：/merge letter.docx out.docx {\"name\":\"小明\"}")
            return
        temp = Path(args[0]).name
        out = Path(args[1]).name
        template_path = resolve_template_file(temp)
        if not template_path:
            await reply_line(token, f"找不到範本：{temp}。請先上傳範本或使用 /templates 查看可用範本。")
            return
        data = args[3] if len(args) > 3 else "{}"
        r = run_officecli(["merge", str(template_path), out, "--data", data, "--force"])
        await reply_result(token, r, f"已合併：{out}", ["merge", out])
    elif action == "download":
        await reply_file(token, args[0])
    elif action == "list":
        files_now = [f.name for f in active_work_dir().iterdir() if f.is_file()]
        await reply_line(token, "目前檔案：\n" + ("\n".join(files_now) if files_now else "（沒有檔案）"))
    elif action == "templates":
        templates = available_templates()
        lines = [f"{item['source']}：{item['name']}" for item in templates]
        await reply_line(token, "目前可用範本：\n" + ("\n".join(lines) if lines else "（尚未建立範本）"))
    elif action == "command":
        r = run_officecli(args)
        await reply_result(token, r, r["out"] or "完成", args)
    else:
        await reply_line(token, "無法判斷你想要的動作，請用 /help 查看指令")


def log_event(kind: str, detail: str):
    """記錄 webhook 事件（記憶型，供診斷）。"""
    import time as _t
    EVENT_LOG.append({
        "t": _t.strftime("%H:%M:%S"),
        "workspace": ACTIVE_WORKSPACE_ID.get(),
        "kind": kind,
        "detail": detail,
    })
    del EVENT_LOG[:-EVENT_LOG_MAX]


@app.get("/debug-log")
def debug_log(request: Request):
    """診斷：回傳最近 webhook 事件紀錄。"""
    if not is_admin_api_request(request):
        return JSONResponse(status_code=403, content={"ok": False, "error": "管理員 API 權限不足"})
    return {"ok": True, "events": EVENT_LOG}


@app.post("/webhook")
async def webhook(request: Request):
    WEBHOOK_STATUS["requests"] += 1
    raw = await request.body()
    signature = request.headers.get("x-line-signature", "")
    if not verify_line_signature(raw, signature):
        WEBHOOK_STATUS["signature_rejected"] += 1
        return JSONResponse(status_code=403, content={"error": "invalid signature"})

    body = json.loads(raw.decode("utf-8"))
    events = body.get("events", [])
    WEBHOOK_STATUS["events"] += len(events)
    for event in events:
        WEBHOOK_STATUS["last_event_type"] = event.get("type", "")
        src = event.get("source", {})
        gid = src.get("groupId")
        rid = src.get("roomId")
        uid = src.get("userId")
        workspace_id = workspace_id_for_source(src)
        target = gid or rid or uid or ""
        source_type = "group" if gid else ("room" if rid else ("user" if uid else "unknown"))
        target_kind = "group" if gid else ("room" if rid else ("user" if uid else "configured_fallback"))
        WEBHOOK_STATUS["last_source_kind"] = source_type
        WEBHOOK_STATUS["last_target_kind"] = target_kind
        WEBHOOK_STATUS["last_target_matches_configured_user"] = bool(uid and uid == LINE_USER_ID)
        workspace_token = ACTIVE_WORKSPACE_ID.set(workspace_id)
        target_token = ACTIVE_LINE_TARGET.set(target)
        source_kind_token = ACTIVE_LINE_SOURCE_KIND.set(source_type)
        admin_token = ACTIVE_IS_ADMIN.set(is_admin_source(src))
        log_event("source", f"workspace={workspace_id} type={source_type}")
        if event.get("type") != "message":
            ACTIVE_LINE_TARGET.reset(target_token)
            ACTIVE_LINE_SOURCE_KIND.reset(source_kind_token)
            ACTIVE_WORKSPACE_ID.reset(workspace_token)
            ACTIVE_IS_ADMIN.reset(admin_token)
            continue
        token = event["replyToken"]
        WEBHOOK_STATUS["messages"] += 1

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
                    (active_work_dir() / fsafe).write_bytes(r.content)
                    await reply_line(token, f"已接收並儲存：{fsafe}\n需要時可輸入 /template private {fsafe} 將它設為個人範本")
                else:
                    await reply_line(token, f"下載檔案失敗：HTTP {r.status_code}")
            ACTIVE_LINE_TARGET.reset(target_token)
            ACTIVE_LINE_SOURCE_KIND.reset(source_kind_token)
            ACTIVE_WORKSPACE_ID.reset(workspace_token)
            ACTIVE_IS_ADMIN.reset(admin_token)
            continue

        if event["message"]["type"] != "text":
            ACTIVE_LINE_TARGET.reset(target_token)
            ACTIVE_LINE_SOURCE_KIND.reset(source_kind_token)
            ACTIVE_WORKSPACE_ID.reset(workspace_token)
            ACTIVE_IS_ADMIN.reset(admin_token)
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
                files = sorted([f.name for f in active_work_dir().iterdir() if f.is_file()])
                await reply_line(token, "目前檔案：\n" + ("\n".join(files) if files else "（沒有檔案）"))
                log_event("handled", "list")
            elif text.startswith("/merge "):
                parts = text[7:].split()
                if len(parts) >= 3:
                    template, output = parts[0], parts[1]
                    template_path = resolve_template_file(template)
                    if not template_path:
                        await reply_line(token, f"找不到範本：{Path(template).name}。請先上傳範本或使用 /templates 查看可用範本。")
                        continue
                    output = Path(output).name
                    data_json = " ".join(parts[2:])
                    data_file = None
                    cand = active_work_dir() / data_json
                    if cand.is_file():
                        data_file = cand
                    args = ["merge", str(template_path), output, "--data", str(data_file) if data_file else data_json, "--force"]
                    r = run_officecli(args)
                    await reply_result(token, r, f"已合併：{output}", ["merge", output])
                    log_event("handled", f"merge ok={r['ok']}")
                else:
                    await reply_line(token, "用法：/merge 模板 輸出 JSON\n範例：/merge letter.docx out.docx {\"name\":\"小明\"}")
            elif text.startswith("/template"):
                await handle_template_command(token, text)
            elif text.startswith("/cmd "):
                if not ACTIVE_IS_ADMIN.get():
                    await reply_line(token, "/cmd 僅限管理員使用；一般使用者請直接用自然語言描述需求。")
                else:
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
        finally:
            ACTIVE_LINE_TARGET.reset(target_token)
            ACTIVE_LINE_SOURCE_KIND.reset(source_kind_token)
            ACTIVE_WORKSPACE_ID.reset(workspace_token)
            ACTIVE_IS_ADMIN.reset(admin_token)

    return {"status": "ok"}


@app.get("/test-push")
async def test_push(request: Request):
    """診斷：直接推一則測試訊息到指定目標（預設群組）。可帶 ?to=userId 指定個人帳號。"""
    if not is_admin_api_request(request):
        return JSONResponse(status_code=403, content={"ok": False, "error": "管理員 API 權限不足"})
    if not LINE_TOKEN:
        return {"ok": False, "error": "LINE_TOKEN 未設定"}
    params = request.query_params
    target = params.get("to") or LINE_GROUP_ID or LINE_USER_ID
    if not target:
        return {"ok": False, "error": "尚未取得 target，請先在 Line 傳一則訊息或把 bot 加入群組"}
    async with httpx.AsyncClient() as c:
        r = await c.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"},
            json={"to": target, "messages": [{"type": "text", "text": "測試 push 訊息 ✅ 若你看到這則，代表 push API 正常"}]},
        )
    return {"ok": r.status_code == 200, "http": r.status_code, "body": r.text[:300],
            "to": target, "to_type": "group" if LINE_GROUP_ID else ("user" if params.get("to") else "recorded")}


@app.get("/diag-env")
def diag_env(request: Request):
    """診斷：檢查關鍵環境變數是否已設定（不顯示內容）。"""
    if not is_admin_api_request(request):
        return JSONResponse(status_code=403, content={"ok": False, "error": "管理員 API 權限不足"})
    return {
        "ok": True,
        "LINE_TOKEN": bool(LINE_TOKEN),
        "LINE_SECRET": bool(LINE_SECRET),
        "GEMINI_KEY_set": bool(GEMINI_KEY),
        "GEMINI_KEY_prefix": GEMINI_KEY[:4] if GEMINI_KEY else "",
        "GEMINI_MODEL": GEMINI_MODEL,
        "ZEN_KEY_set": bool(ZEN_KEY),
        "ZEN_KEY_prefix": ZEN_KEY[:4] if ZEN_KEY else "",
        "ZEN_MODEL": ZEN_MODEL,
        "AI_PROVIDER": AI_PROVIDER,
        "ADMIN_API_TOKEN_set": bool(ADMIN_API_TOKEN),
        "DOWNLOAD_SECRET_set": bool(DOWNLOAD_SECRET),
        "WORKSPACE_ISOLATION": True,
    }


@app.get("/test-zen")
async def test_zen(request: Request):
    """測試 OpenCode Zen key 與 chat/completions 連線，不顯示 key。"""
    if not is_admin_api_request(request):
        return JSONResponse(status_code=403, content={"ok": False, "error": "管理員 API 權限不足"})
    if not ZEN_KEY:
        return {"ok": False, "error": "OPENCODE_ZEN_API_KEY 未設定", "model": ZEN_MODEL}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                zen_url(),
                headers=zen_headers(),
                json=zen_payload('請只回覆 JSON：{"action":"list","args":[]}', []),
            )
        return {
            "ok": r.status_code == 200,
            "http": r.status_code,
            "model": ZEN_MODEL,
            "msg": "works" if r.status_code == 200 else r.text[:300],
        }
    except Exception as e:
        return {"ok": False, "model": ZEN_MODEL, "error": str(e)}


@app.get("/test-gemini")
async def test_gemini(request: Request):
    """測試 Gemini API key 是否可用（不顯示 key）。"""
    if not is_admin_api_request(request):
        return JSONResponse(status_code=403, content={"ok": False, "error": "管理員 API 權限不足"})
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
async def line_status(request: Request):
    """檢查 LINE bot 連線狀態。"""
    if not is_admin_api_request(request):
        return JSONResponse(status_code=403, content={"ok": False, "error": "管理員 API 權限不足"})
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
