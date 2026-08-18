# officemcp — Codex 接手交接文件

> 交接日期：2026-08-18（交接者：OpenCode；接手者：Codex）
> 專案目的：OfficeCLI 驅動的 FastAPI 伺服器 + LINE Bot，讓使用者用 LINE 以自然語言建立/編輯/下載 Word、Excel、PowerPoint 文件，成功後自動回傳下載連結。

---

## 1. 快速開始

| 項目 | 值 |
|------|-----|
| GitHub 倉庫 | `https://github.com/Harry5450/officemcp`（branch: `master`） |
| 部署平台 | Zeabur（Git push 自動部署） |
| 服務網址 | `https://mcpoffice.zeabur.app` |
| 單一應用 | `app.py`（FastAPI：API + LINE Webhook + OfficeCLI 包裝 + Gemini fallback） |
| 工作目錄 | `/app/output`（**已掛載 Zeabur Volume，跨重啟持久化**） |
| OfficeCLI 版本 | v1.0.144（GitHub release 直接下載 `/usr/local/bin/officecli`） |

本地開發：直接改 `app.py` → push master → Zeabur 自動部署（約 1–3 分鐘，可透過 `/health` 的 `boot` 時間戳確認新版上線）。

## 2. 環境變數（Zeabur 後台設定）

| 變數 | 說明 | 目前值 |
|------|------|--------|
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Bot 存取 token（已設定） | - |
| `LINE_CHANNEL_SECRET` | Webhook 簽名驗證（已設定） | - |
| `GEMINI_API_KEY` | 自然語言 LLM fallback（AQ.A 開頭新版 key） | - |
| `GEMINI_MODEL` | 預設 `gemini-flash-latest` | `gemini-flash-latest` |
| `PORT` | 服務埠 | `8080`（Zeabur 會注入） |
| `PUBLIC_BASE_URL` | 下載連結的公開網域 | `https://mcpoffice.zeabur.app` |
| `LINE_USER_ID` | 使用者個人 userId（預設已寫死在 code） | `U3cd7fa54416c25e1472fd8b747a8ead2` |
| `LINE_GROUP_ID` | 群組 push 目標（可選） | `Cd164b3ea2d88b891de1781bc2e442798` |

## 3. 架構與關鍵決策（重要背景）

### 3.1 LINE 回覆機制（踩過的坑，勿回退）
- **改用 push API（`/v2/bot/message/push`）**，因為 reply API 的 token **只能用一次且必須 5 秒內送出**，officecli 處理超過 5 秒會靜默失敗。
- **`send_line_message()`（app.py:135）**：優先 `LINE_GROUP_ID` → `LINE_USER_ID` → 退回 reply。失敗會記錄到 `/debug-log`。
- **不能 push 到「bot 自己的 userId」**：`Uf8528f219ab515ba80017faad4a8746d` 是 bot 自身，會回 `You can't send messages to yourself`。使用者正確 userId 是 `U3cd7fa54416c25e1472fd8b747a8ead2`。

### 3.2 檔案回傳（免費方案相容）
- LINE 免費方案**不支援 `type:"file"`**（會回 `This message type is not available for your account`）。
- 所以改成**文字訊息 + 下載連結**（`reply_file` 與 `reply_result`），相容免費方案。
- `reply_result()` 成功後自動附上 `📎 檔名 下載：<URL>` 連結。

### 3.3 自然語言解析（規則優先 + Gemini 兜底）
- `parse_rule()` 先跑正規表示式，**失敗才呼叫 Gemini**（`ask_llm`）。
- 支援動作：`create` / `create_text` / `add_text` / `add_title` / `replace_text` / `merge` / `merge_open` / `download` / `list` / `command`。
- Gemini prompt 在 `gemini_payload()`，要求回傳 `{"action": "...", "args": [...]}` JSON。
- 已驗證說法（`app.py` 內有測試對照）：
  - 「幫我建立 report.docx」「建立一份簡報叫產品發表會」「新增一份試算表叫銷售」
  - 「在 notes.docx 加一句 你好世界」「notes.docx 幫我加上 這是一段內容」
  - 「幫我在報告.docx 加入標題 季度報告」「把 notes.docx 的內容改成 新的內容」
  - 「幫我建立一個 Excel 檔案叫 客戶名單」
- **已知限制**：「把 letter.docx 合併成 out.docx」走 Gemini（merge 規則在 parse_rule 中位置導致）—可接受。

### 3.4 OfficeCLI 用法（關鍵指令）
- 二進位：`/usr/local/bin/officecli`，執行時 `cwd=/app/output`。
- `create <檔名>` / `add <檔名> / --type paragraph --prop text=內容` / `set <檔名> /body/p[1] --prop text=內容 --force` / `merge <模板> <輸出> --data <json> --force` / `save <檔名>`（flush 到磁碟）。
- **記憶體快取**：`create/add` 後需 `save` 才寫入磁碟。`reply_result` 會自動補 `save`。

### 3.5 Volume 持久化
- Zeabur Volume 掛載在 `/app/output`。掛載後**資料在重啟時清空一次**（已發生過），之後跨重啟持久。
- 掛載後服務**無法零停機重啟**（重啟會先停後啟）。

## 4. 診斷端點

| 端點 | 用途 |
|------|------|
| `/health` | 狀態 + `boot` 時間戳 + git commit（驗證新版上線） |
| `/files` | 列出已建立檔案 |
| `/diag-files` | 檔案 + UTF-8 hex |
| `/diag-env` | 環境變數是否設定（不顯示值） |
| `/debug-log` | 最近 webhook 事件（received/handled/reply_error/source） |
| `/test-push` | 測 push（可 `?to=userId`） |
| `/test-gemini` | 測 Gemini key + 列可用模型 |
| `/line-status` | LINE Bot 連線狀態 |
| `/create` `/command` `/download/{file}` | 直接 API |

## 5. 已知問題 / 待辦

1. **無刪除端點**：伺服器上有測試殘留檔（`volume_test.docx`、`persist_test.docx`、`group_test.docx`），無 `/delete` API。可加刪除功能或清理。
2. **Merge 在規則引擎中位置**：`merge_open` 會在沒有 data 時提示補 JSON，體驗可再優化。
3. **`replace_text` 目前只改第一段**（`/body/p[1]`），多段文件「改內容」語意待確認。
4. **自然語言規則可持續擴充**：測試集在 `C:\Users\User\AppData\Local\Temp\opencode\test_nl3.py`（本機驗證用，不入 git）。建議未來移入專案作為單元測試。
5. **Gemini 模型**：`gemini-2.0-flash` 已不存在（404），目前用 `gemini-flash-latest`。
6. **LLM fallback 的 args 若含中文檔名**：需確認 quote 處理（`/download/` URL 已用 `urllib.parse.quote`，安全）。

## 6. 給 Codex 的建議工作流程

1. 讀 `AGENTS.md`（本檔）與 `app.py` 全貌。
2. 本地改動後，先 `python -m py_compile app.py` + 本機跑 `test_nl3.py` 驗證規則。
3. push master → Zeabur 自動部署 → 用 `/health` boot 時間戳確認新版。
4. 在 LINE 群組/個人聊天實測（bot `@486ddxkk`；或直接用 `/command`、`/create` API 測）。

## 7. 安全注意

- 本機 PowerShell 輸出中文會亂碼（編碼問題），驗證規則請寫入 UTF-8 檔再讀，勿看 console。
- 環境變數含 secrets，`/diag-env` 只回 bool 不洩值，勿改。
- LINE webhook 簽名驗證 `verify_line_signature()` 用 HMAC-SHA256，勿移除。
