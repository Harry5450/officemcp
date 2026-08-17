import os
import subprocess
import shutil
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

PORT = int(os.environ.get("PORT", 8080))
HOST = os.environ.get("HOST", "0.0.0.0")
WORK_DIR = Path("/app/output")
WORK_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="OfficeCLI API")


def get_officecli() -> str:
    """找到 officecli 路徑。"""
    path = shutil.which("officecli")
    if path:
        return path
    local = "/root/.local/bin/officecli"
    if os.path.isfile(local):
        return local
    return "officecli"


def run_cmd(cmd: str, timeout: int = 60) -> dict:
    """執行指令。"""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=str(WORK_DIR),
            env={**os.environ, "PATH": f"/root/.local/bin:{os.environ.get('PATH', '')}"}
        )
        return {"ok": r.returncode == 0, "out": r.stdout.strip(), "err": r.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "out": "", "err": "timeout"}
    except Exception as e:
        return {"ok": False, "out": "", "err": str(e)}


@app.get("/")
def root():
    return {"status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/create")
async def create_file(request: Request):
    """建立 Office 文件。"""
    data = await request.json()
    filename = data.get("filename", "")

    if not filename.endswith((".docx", ".xlsx", ".pptx")):
        return JSONResponse({"ok": False, "error": "僅支援 .docx, .xlsx, .pptx"})

    oc = get_officecli()
    result = run_cmd(f"{oc} create {filename}", timeout=30)

    if result["ok"]:
        return {"ok": True, "message": f"已建立：{filename}"}
    else:
        return JSONResponse({"ok": False, "error": result["err"] or result["out"]})


@app.post("/api/command")
async def run_command(request: Request):
    """執行 officecli 指令。"""
    data = await request.json()
    args = data.get("args", [])

    if not args:
        return JSONResponse({"ok": False, "error": "請提供指令參數"})

    oc = get_officecli()
    args_str = " ".join(args)
    result = run_cmd(f"{oc} {args_str}", timeout=120)

    return {"ok": result["ok"], "output": result["out"], "error": result["err"]}


@app.post("/api/preview")
async def preview(request: Request):
    """生成預覽圖。"""
    data = await request.json()
    filename = data.get("filename", "")
    output = data.get("output", "")

    if not output:
        output = Path(filename).stem + "_preview.png"

    oc = get_officecli()
    result = run_cmd(f"{oc} view {filename} screenshot -o {output}", timeout=120)

    return {"ok": result["ok"], "output": output, "error": result["err"]}


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
