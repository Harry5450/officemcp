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
| 單一應用 | `app.py`（FastAPI：API + LINE Webhook + OfficeCLI 包裝 + Zen/Gemini fallback） |
| 工作目錄 | `/app/output`（**已掛載 Zeabur Volume，跨重啟持久化**） |
| OfficeCLI 版本 | v1.0.144（GitHub release 直接下載 `/usr/local/bin/officecli`） |

本地開發：直接改 `app.py` → push master → Zeabur 自動部署（約 1–3 分鐘，可透過 `/health` 的 `boot` 時間戳確認新版上線）。

## 2. 環境變數（Zeabur 後台設定）

| 變數 | 說明 | 目前值 |
|------|------|--------|
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Bot 存取 token（已設定） | - |
| `LINE_CHANNEL_SECRET` | Webhook 簽名驗證（已設定） | - |
| `ADMIN_USER_IDS` | 可使用 `/cmd` 與發布共用範本的 LINE userId，逗號分隔 | 預設使用 `LINE_USER_ID` |
| `ADMIN_API_TOKEN` | `/files`、診斷、管理 API 的 `x-admin-token`（勿提交 Git） | - |
| `DOWNLOAD_SECRET` | 下載連結簽名密鑰；未設定時回退 `LINE_CHANNEL_SECRET`，正式環境建議明確設定 | - |
| `GEMINI_API_KEY` | 自然語言 LLM fallback（AQ.A 開頭新版 key） | - |
| `GEMINI_MODEL` | Gemini fallback 模型 | `gemini-2.5-flash` |
| `OPENCODE_ZEN_API_KEY` | OpenCode Zen API key（可選，勿提交 Git） | - |
| `ZEN_BASE_URL` | Zen OpenAI-compatible API 根網址 | `https://opencode.ai/zen/v1` |
| `ZEN_MODEL` | Zen 模型 | `deepseek-v4-flash-free` |
| `AI_PROVIDER` | 首選 provider：`zen` 或 `gemini`；另一個會作備援 | `zen`（有 Zen key 時） |
| `PORT` | 服務埠 | `8080`（Zeabur 會注入） |
| `PUBLIC_BASE_URL` | 下載連結的公開網域 | `https://mcpoffice.zeabur.app` |
| `LINE_USER_ID` | 使用者個人 userId（預設已寫死在 code） | `U3cd7fa54416c25e1472fd8b747a8ead2` |
| `LINE_GROUP_ID` | 群組 push 目標（可選） | `Cd164b3ea2d88b891de1781bc2e442798` |

## 3. 架構與關鍵決策（重要背景）

### 3.1 LINE 回覆機制（踩過的坑，勿回退）
- **改用 push API（`/v2/bot/message/push`）**，因為 reply API 的 token **只能用一次且必須 5 秒內送出**，officecli 處理超過 5 秒會靜默失敗。
- **`send_line_message()`**：個人聊天室與群組／聊天室都優先 push 到該事件 target；push 明確失敗時再使用尚未過期的 reply token，並記錄到 `/debug-log`。這是相容原本可用傳送路徑的暫時穩定策略。
- **不能 push 到「bot 自己的 userId」**：`Uf8528f219ab515ba80017faad4a8746d` 是 bot 自身，會回 `You can't send messages to yourself`。使用者正確 userId 是 `U3cd7fa54416c25e1472fd8b747a8ead2`。

### 3.2 檔案回傳（免費方案相容）
- LINE 免費方案**不支援 `type:"file"`**（會回 `This message type is not available for your account`）。
- 所以改成**文字訊息 + 下載連結**（`reply_file` 與 `reply_result`），相容免費方案。
- `reply_result()` 成功後自動附上 `📎 檔名 下載：<URL>` 連結。
- 下載連結為 workspace 綁定的 HMAC token，預設有效 1 小時；部署此版本後，舊版未帶 token 的下載 URL 會失效。

### 3.3 自然語言解析（規則優先 + Zen/Gemini 兜底）
- 含「建立…Word，包含／包括…」的複合需求會走 create_content：先建立檔案，再寫入標題與欄位，避免只產生空白文件。
- 複合建立遇到同名檔案會自動加序號（例如 會議紀錄_2.docx），保留原檔，不直接覆蓋。
- 複合建立完成後會讀回 DOCX 驗證標題與欄位；驗證失敗不會回傳下載成功訊息。
- `parse_rule()` 先跑正規表示式；**規則失敗才呼叫 LLM**（`ask_llm`）。
- 有 `OPENCODE_ZEN_API_KEY` 時預設先呼叫 OpenCode Zen 的 `chat/completions`；失敗或限流時自動改呼叫 Gemini。
- 可用 `AI_PROVIDER=gemini` 將順序反轉。兩個 key 都沒有時，才回覆 `/help`。
- 支援動作：`create` / `create_content` / `create_text` / `add_text` / `add_title` / `replace_text` / `merge` / `merge_open` / `download` / `list` / `templates`；`/cmd` 與任意 `command` 僅限管理員。
- 「你要回傳給我」「把剛剛的檔案傳回來」等沒有檔名的後續訊息，會回傳最近建立的 Office 檔案。
- 建立成功訊息會使用實際檔名，並在同一則 LINE 訊息附上下載連結；不要回退成 `args[0]`（那會是 `create`）。
- Word 內容任務會先由 `read_docx_content()` 抽取 `word/document.xml` 的段落與表格文字，再交給 Zen／Gemini。
- 「摘要／整理／分析」預設直接回 LINE；「整理成 Word／另存新檔」會建立新的 `.docx`，保留原始檔不覆蓋。
- 目前輸出整理檔以純段落寫入；不會修改原 Word 的圖片、頁首頁尾、註解、追蹤修訂或複雜排版。
- Workspace 範圍：LINE 群組使用群組 key、聊天室使用 room key、個人聊天使用 user key；key 為 hash，不把原始 LINE ID 寫入路徑。
- 範本優先順序：目前 workspace 的 `templates/` 個人範本 → workspace 根目錄 → `/app/output/_templates` 共用範本。
- 上傳檔案後可輸入 `/template private 檔名.docx` 儲存個人範本；管理員可輸入 `/template publish 檔名.docx` 發布共用範本，同名共用範本不覆蓋，請用新版本檔名。
- `/cmd`、管理 API、診斷 API 均應只給管理員；一般使用者走自然語言與範本白名單。
- Gemini prompt 在 `gemini_payload()`，要求回傳 `{"action": "...", "args": [...]}` JSON。
- 已驗證說法（`app.py` 內有測試對照）：
  - 「幫我建立 report.docx」「建立一份簡報叫產品發表會」「新增一份試算表叫銷售」
  - 「在 notes.docx 加一句 你好世界」「notes.docx 幫我加上 這是一段內容」
  - 「幫我在報告.docx 加入標題 季度報告」「把 notes.docx 的內容改成 新的內容」
  - 「幫我建立一個 Excel 檔案叫 客戶名單」
  - 「幫我建立一份會議紀錄 Word，包含會議主題、日期、出席人員、討論事項與待辦事項」
- **已知限制**：「把 letter.docx 合併成 out.docx」走 Gemini（merge 規則在 parse_rule 中位置導致）—可接受。

### 3.4 OfficeCLI 用法（關鍵指令）
- docx add 目前只用 paragraph 寫入標題文字；不要使用不支援的 heading element type。
- 伺服器整合固定使用 OFFICECLI_NO_AUTO_RESIDENT=1 direct mode；Word 段落父節點使用 /body，避免跨 subprocess resident 快取不同步。
- 二進位：`/usr/local/bin/officecli`，執行時 `cwd=/app/output`。
- `create <檔名>` / `add <檔名> / --type paragraph --prop text=內容` / `set <檔名> /body/p[1] --prop text=內容 --force` / `merge <模板> <輸出> --data <json> --force` / `save <檔名>`（flush 到磁碟）。
- **記憶體快取**：`create/add` 後需 `save` 才寫入磁碟。`reply_result` 會自動補 `save`。

### 3.5 Volume 持久化
- Zeabur Volume 掛載在 `/app/output`。掛載後**資料在重啟時清空一次**（已發生過），之後跨重啟持久。
- 掛載後服務**無法零停機重啟**（重啟會先停後啟）。

## 4. 診斷端點

| 端點 | 用途 |
|------|------|
| `/health` | 狀態 + `boot` 時間戳 + git commit + LINE 設定布林值與 webhook 診斷計數（不回傳 token 或訊息內容） |
| `/files` | 列出已建立檔案 |
| `/diag-files` | 檔案 + UTF-8 hex |
| `/templates` | 管理員查看目前 workspace 的個人／共用範本 |
| `/diag-env` | 環境變數是否設定（不顯示值） |
| `/debug-log` | 最近 webhook 事件（received/handled/reply_error/source） |
| `/test-push` | 測 push（可 `?to=userId`） |
| `/test-zen` | 測 OpenCode Zen key、模型與 chat/completions |
| `/test-gemini` | 測 Gemini key + 列可用模型 |
| `/line-status` | LINE Bot 連線狀態 |
| `/create` `/command` | 管理員 API，需 `x-admin-token: <ADMIN_API_TOKEN>` |
| `/download/{file}` | 需訊息中的 workspace 簽名 `token` |

## 5. 已知問題 / 待辦

1. **測試殘留檔**：本機 `/app/output` 沒有殘留檔；截至 2026-08-18 對線上 `/files` 的唯讀讀回仍看到 `volume_test.docx`、`persist_test.docx`、`group_test.docx` 等測試檔。現行版本無 `/delete` API，需另行核准維護清理或先加入受保護的清理流程。
2. **Merge 在規則引擎中位置**：`merge_open` 會在沒有 data 時提示補 JSON，體驗可再優化。
3. **`replace_text` 目前只改第一段**（`/body/p[1]`），多段文件「改內容」語意待確認。
4. **自然語言規則可持續擴充**：測試集在 `C:\Users\User\AppData\Local\Temp\opencode\test_nl3.py`（本機驗證用，不入 git）。建議未來移入專案作為單元測試。
5. **Gemini 模型**：`gemini-2.0-flash` 已不存在（404），目前驗證可用 `gemini-2.5-flash`。
6. **LLM fallback 的 args 若含中文檔名**：需確認 quote 處理（`/download/` URL 已用 `urllib.parse.quote`，安全）。
7. **Word 內容抽取上限**：目前最多送出前 12,000 個字元；超過部分會標記截斷，且只讀正文 XML，不含圖片 OCR、頁首頁尾與註解。
8. **Zen 免費模型資料政策**：接入前確認可接受 OpenCode Zen 對免費模型的資料使用條款；不要讓文件包含密碼、Token 或敏感個資。
9. **多人上線前置條件**：部署前必須設定強隨機 `ADMIN_API_TOKEN` 與 `DOWNLOAD_SECRET`，並驗證不同 LINE user／group 的檔案、範本、下載 token 不會互通。管理 API 沒有 token 會回 403。
10. **範本版本治理**：目前已有個人／共用目錄與同名防覆蓋，但尚未建立完整 `template_id/version/status/fields_schema` registry；正式商用前需補上審核與版本清單。
11. **個人記憶**：目前尚未開放可編輯的長期 AI 記憶；`EVENT_LOG` 只作管理診斷，已標記 workspace，不能當作使用者記憶或跨 workspace 上下文。

## 6. 給 Codex 的建議工作流程

1. 讀 `AGENTS.md`（本檔）與 `app.py` 全貌。
2. 本地改動後，先 `python -m py_compile app.py` + 本機跑 `test_nl3.py` 驗證規則。
3. push master → Zeabur 自動部署 → 用 `/health` boot 時間戳確認新版。
4. 在 LINE 群組/個人聊天實測（bot `@486ddxkk`；或直接用 `/command`、`/create` API 測）。

## 7. 安全注意

- 本機 PowerShell 輸出中文會亂碼（編碼問題），驗證規則請寫入 UTF-8 檔再讀，勿看 console。
- 環境變數含 secrets，`/diag-env` 只回 bool 不洩值，勿改。
- LINE webhook 簽名驗證 `verify_line_signature()` 用 HMAC-SHA256，勿移除。
