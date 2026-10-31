# Mô hình bảo mật

Tài liệu này gộp `docs/SECURITY.md` và `docs/SECURITY_MODEL.md` cũ, đã đối chiếu với code
(`app/config.py`, `app/main.py`, `app/mcp_server.py`, `app/telegram_bot.py`, `app/collaboration.py`,
`app/switches.py`, `app/keys.py`, `app/logs.py`, `app/orchestrator.py`, `app/workspace.py`,
`app/channel.py`). Bảo mật riêng của workflow xem [WORKFLOWS.md](WORKFLOWS.md#9-bảo-mật-của-workflow),
trần quyền của plugin xem [PLUGINS.md](PLUGINS.md). Cách báo lỗ hổng: [SECURITY.md ở thư mục gốc](../SECURITY.md).

Nguyên tắc trung tâm: mọi thao tác (lệnh từ giao diện, operation của plugin, tool call do model sinh ra,
tool MCP) đi qua cùng một chuỗi:

```
xác thực giao diện → validate TaskRequest → planner validate (registry + policy) →
engine (approval, giới hạn) → ToolExecutor: schema → ToolPolicyEngine → handler → kiểm output → record/event
```

## 1. Ranh giới tin cậy

| Ranh giới | Ai được làm gì | Thực thi trong code |
|---|---|---|
| Telegram, lệnh | User thuộc `TELEGRAM_ALLOWED_USER_IDS` **và** chat là control group (`TELEGRAM_CONTROL_CHAT_ID`) | `TelegramGateway.authorized()`; tin không hợp lệ bị bỏ qua và log `unauthorized message` |
| Telegram, nút bấm | User trong allowlist, chat là control group hoặc report channel (`TELEGRAM_REPORT_CHANNEL_ID`) | kiểm tra trong handler callback |
| Telegram, chat riêng | Chỉ dành cho giá trị `/keys` và passcode; user phải trong allowlist và đang có yêu cầu chờ; tin bị xoá ngay | `on_key_value`, luồng passcode |
| Telegram, Discussion group | Feedback trên báo cáo, chỉ từ user trong allowlist; không cấp quyền điều khiển | handler feedback |
| Notice chat (`TELEGRAM_NOTICE_CHAT_ID`) | Chỉ nhận thông báo hệ thống (session đến/đi, routing, lane đóng), gửi im lặng; không nhận lệnh. Không đặt thì thông báo vào control group | `send_notice()` |
| Telegram webhook | Chỉ request mang header `X-Telegram-Bot-Api-Secret-Token` khớp `TELEGRAM_WEBHOOK_SECRET` (16–256 ký tự); URL phải `https://`; body ≤ 1 MB | `telegram_webhook()` |
| REST `/api/v1/*` | `Authorization: Bearer API_TOKEN` (so sánh hằng thời gian). Không có `API_TOKEN` → 503 | `require_token` |
| Quyết định của operator qua REST | Duyệt approval, thực thi action, trả lời human input cần `OPERATOR_TOKEN`; không đặt → 403, chỉ làm trên Telegram | `require_operator` |
| MCP `/mcp` | Mặc định Bearer `API_TOKEN`. `MCP_REQUIRE_TOKEN=false`: không cần token nhưng chỉ nhận request cùng máy (client loopback, `Host` là localhost/127.0.0.1/::1, không có `Origin` lạ — chặn DNS rebinding/CSRF). Probe OAuth nhận 404 | `_McpEndpoint`, `_local_request` |
| Endpoint channel (`/api/v1/channel/*`) | Không token, chỉ request cục bộ | `_local_only` |
| Quick Actions | Token ngẫu nhiên 12 ký tự, dùng một lần, có TTL, kiểm trạng thái task; Duyệt/Từ chối tiêu thụ theo nhóm | `app/actions.py` |

Gateway mặc định chỉ bind `127.0.0.1`. Khi `MCP_REQUIRE_TOKEN=false`, **mọi** process cục bộ đều dùng được
gateway.

### Yêu cầu từ agent là không tin cậy

- Agent (Claude Code, Codex, OpenCode, Cursor...) giữ `API_TOKEN`, nên token này **không** đủ để ra quyết
  định của operator. Duyệt, thực thi action, trả lời câu hỏi qua REST cần `OPERATOR_TOKEN`.
- Không tool MCP nào duyệt được approval của workflow. `close_question` chỉ đóng bản sao Telegram của câu hỏi
  khi người dùng đã trả lời trong session; câu trả lời được ghi là `(trả lời trong session) ...`, không phải
  câu "đã xác minh passcode".
- Tool call do model sinh ra được đối chiếu với registry (không có tool tự chế), validate schema, kiểm
  policy và chỉ chạy một lần mỗi call id.
- Nội dung agent gửi lên (báo cáo, ảnh, file) chỉ được lưu và hiển thị; không được thực thi.
- Operator có thể tắt từng tool MCP mà agent gọi bằng `/mcp` trên Telegram (`app/switches.py`). Switch lưu
  trong store, có hiệu lực ở lần gọi kế tiếp, không cần restart; tool bị tắt trả lỗi
  `switched off by the operator`. `generate_art` mặc định theo `enabled` của provider fal trong
  `config/providers/models.yaml`; các tool khác mặc định bật. Agent API có switch riêng (`/agents`).

### Câu hỏi bảo mật (secure) và passcode

- Câu hỏi `request_human_input(secure=true)` (release, deploy, commit/push, git pull) chỉ được **duyệt** từ
  Telegram khi có `TELEGRAM_CONFIRM_PASSCODE`. Người dùng bấm ✅ rồi gửi passcode trong chat riêng với bot
  (chờ tối đa 300 s); tin chứa passcode bị xoá ngay.
- Không cần passcode để **từ chối** (❌ Từ chối hoặc một lựa chọn kiểu "Chưa", "Không", "Để sau").
- Sai 3 lần: câu hỏi bị huỷ, không bao giờ được duyệt, agent được báo không được làm bước đó.
- Không có passcode trong `.env`: câu hỏi secure chỉ có thể bị từ chối từ Telegram, duyệt phải trên máy.
- Passcode so sánh bằng `hmac.compare_digest`, không bao giờ được lưu hay log; chỉ sự kiện
  `human_input.passcode_ok/passcode_wrong` được ghi.
- Câu hỏi agent chỉ hỏi trong chat của chính nó (hook cuối lượt) được chuyển lên Telegram; nếu chứa
  commit/push/merge/release/deploy/git pull thì tự động thành secure.

### Auto-accept

`/auto` bật chế độ tự trả lời khi operator vắng: câu hỏi thường nhận lựa chọn đầu tiên (hoặc "tự quyết theo
đề xuất tốt nhất"), câu hỏi secure được **duyệt** với lựa chọn đầu tiên không phải từ chối. Bật auto-accept
cần passcode; tắt thì có hiệu lực ngay. Auto-accept chỉ áp dụng cho câu hỏi human input của agent, không
duyệt các step approval của workflow. Hãy coi việc bật auto-accept là uỷ quyền trước cho mọi thao tác secure
trong thời gian đó.

## 2. Bí mật (secrets)

| Nơi | Nội dung |
|---|---|
| `.env` (git-ignored; `.env.dev`, `.env.uat` cho môi trường khác) | `TELEGRAM_BOT_TOKEN`, `API_TOKEN`, `OPERATOR_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `TELEGRAM_CONFIRM_PASSCODE`, `AGW_SECRET_KEY`, key provider (`OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `GEMINI_API_KEY`, `FAL_KEY`, `MESHY_API_KEY`...), `OPENCODE_PASSWORD` |
| Database gateway | Key/model đặt qua `/keys`: chỉ các biến mà cấu hình provider nêu tên (`api_key_env`, `model_env`), mã hoá Fernet bằng `AGW_SECRET_KEY`; giá trị này thắng `.env`, xoá thì quay về `.env`. Trạng thái chỉ hiện 4 ký tự cuối |
| YAML trong `config/` | **Không** được chứa bí mật: khoá có dạng `api_key|token|secret|password` hoặc giá trị trông như bí mật bị từ chối khi validate; provider tham chiếu biến `*_env` |

Bảo vệ:

- Formatter log JSON che: các giá trị bí mật đã đăng ký (`Settings.secret_values()`), mẫu bot token Telegram,
  key `sk-...`, giá trị sau `Bearer`. Log của `httpx` bị nâng lên WARNING vì URL chứa bot token.
- Giá trị `/keys` và passcode chỉ gửi trong chat riêng, bị xoá ngay, không log, không lặp lại.
- Môi trường dev/uat không được dùng chung bot token với prod (gateway từ chối khởi động).
- Báo cáo Telegram chỉ chứa tóm tắt và artifact id, không có biến môi trường hay log thô ngoài đoạn cuối QA có giới hạn.
- Process con nhận môi trường tối giản: QA chạy với `PATH, HOME, LANG, LC_ALL, TMPDIR` + `QA_ENV_PASSTHROUGH`;
  MCP stdio server nhận `PATH, HOME, LANG, LC_ALL, TMPDIR, USER` + `env_passthrough` của server. Runtime
  Claude Code (`claude -p`) bị gỡ `TELEGRAM_BOT_TOKEN`, `API_TOKEN`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`
  (và `CLAUDECODE`); các biến khác của process vẫn được kế thừa (xem mục Giới hạn).

Không commit: `.env`, `.env.*` (trừ `.env.example`), `var/` (state DB, media, artifacts, worktrees,
scratch), `.venv/`, `*.log`.

## 3. Quyền và allowlist cho agent

| Cơ chế | Mặc định | Ý nghĩa |
|---|---|---|
| `PLUGIN_PERMISSIONS` | `workspace:read, workspace:write, process:execute, artifact:write, data:read, provider:image` | Trần quyền mọi tool của plugin; giao với `manifest.permissions` |
| `TOOL_PERMISSIONS` | giống trên | Quyền cấp cho step tool lúc chạy (giao với `step.permissions` nếu step thu hẹp). `mcp:execute` **không** được cấp mặc định |
| `policies.trusted_plugin_modules` | 6 plugin có sẵn | Module plugin được phép tải khi có cấu hình YAML |
| `policies.tool_allowlist` / `provider_allowlist` | `null` (tất cả) | Mẫu fnmatch giới hạn tool/provider được chọn làm executor |
| `policies.approvals` | runtime ghi file, `workspace.write_file`, `media.generate` | Luật thêm approval theo executor id |
| `policies.mcp_url_hosts` | `127.0.0.1, localhost, ::1` | Host được phép cho MCP server qua HTTP (chống SSRF) |
| MCP server `allowed_tools` / `read_only_tools` | – | Chỉ tool được allowlist mới đăng ký (`mcp.<server>.<tool>`); tool không read-only cần `mcp:execute` + approval; tham số dạng đường dẫn bị giới hạn trong workspace |
| `QA_COMMANDS` | rỗng | Lệnh QA chỉ lấy từ cấu hình, chạy không qua shell, có timeout (`QA_TIMEOUT_S`) và giới hạn output. `QA_SANDBOX=docker` thêm `--network none` và chỉ mount worktree; `none` **không** phải sandbox |
| `QA_ENV_PASSTHROUGH` | rỗng | Biến môi trường thêm cho lệnh QA |
| `OPENCODE_SHELL_ALLOWLIST` | `python3 -m pytest`, `pytest`, `git status`, `git diff` | Yêu cầu chạy shell của OpenCode chỉ được duyệt khi argv khớp tiền tố trong danh sách; lệnh có `;`, `&&`, `|`, `>`, `` ` ``, `$(`... bị từ chối |
| `CLAUDE_CODE_ALLOWED_TOOLS` | rỗng | `--allowedTools` thêm cho runtime Claude Code khi implement (role chỉ đọc dùng `Read,Grep,Glob`) |
| `routing.task_rules[].require_command` | rule `rea` cho reverse engineering | Hook PreToolUse chặn sửa file và lệnh khác cho tới khi session chạy lệnh bắt buộc |

## 4. Cách ly workspace

- **Tool workspace** (`app/builtin_tools.py`): đường dẫn được chuẩn hoá và resolve (bắt cả thoát qua symlink),
  không được ra ngoài workspace của task, chặn `.git/`. Đọc và ghi tối đa 200 000 byte; ghi là create-only.
- **Worktree của task** (`app/workspace.py`): khi có `WORKSPACE_ROOT` (phải là top-level của repo git), mỗi
  task ghi vào `DATA_DIR/worktrees/<task_id>` trên branch `agw/<task_id>` tạo từ HEAD. Mỗi worktree chỉ một
  writer; worktree tạo một lần dưới lock. Không có `WORKSPACE_ROOT` thì ghi vào thư mục scratch của task.
  Checkout chính không bao giờ bị sửa, commit hay merge. Gỡ worktree không dùng `--force` nên giữ lại thay
  đổi chưa commit.
- **Session con (child lane)** (`app/channel.py`): mỗi lane có bản sao riêng tại
  `<repo>/.agw-children/<lane>-<id>` trên branch `agw/<lane>-<id>`: ưu tiên clone copy-on-write APFS, sau đó
  git worktree, thư mục không phải repo git thì là bản copy không branch. Thư mục `.agw-children` có
  `.gitignore` là `*`. Branch được `git fetch` về repo gốc để review; file của thư mục project không bị đụng.
  `/children prune` từ chối xoá bản sao có thay đổi chưa commit; branch luôn được giữ.
- Nhắc lại: worktree là thư mục làm việc, **không** phải ranh giới hệ điều hành.

## 5. Lưu giữ dữ liệu và sao lưu

- `DATA_RETENTION_DAYS` (mặc định 30; `0` = giữ mãi): mỗi 24 giờ (lần đầu khoảng 1 giờ sau khi start) gateway
  xoá task đã kết thúc cũ hơn ngưỡng cùng mọi thứ thuộc về nó (artifact trước, rồi step, run, báo cáo,
  feedback, sự kiện), tin nhắn/ask/câu hỏi/approval đã xong, liên kết Telegram cũ và sự kiện cũ.
- Không bao giờ xoá: task đang chạy, task có cha hoặc con đang chạy, task của session còn sống, artifact
  được task đang chạy tham chiếu, và task/artifact thuộc domain trong `RETENTION_EXEMPT_DOMAINS` (mặc định
  `game_research`, vì là evidence).
- `POST /api/v1/maintenance/cleanup-artifacts` dọn artifact thủ công; không truyền `retention_days` thì dùng
  `policies.artifact_retention_days` (mặc định 30).
- Gateway **không** tự tạo bản sao lưu database. Bản sao do operator tạo trong `DATA_DIR` theo tên
  `<tên>.bak-YYYYMMDD-HHMMSS` bị xoá ở lượt retention khi cũ hơn 3 ngày (theo thời gian trong tên); file
  không đúng mẫu tên được để nguyên.
- Lệnh setup sửa cấu hình agent giữ một bản `.agw-backup` của file trước lần sửa đầu tiên.

<a id="known-limitations"></a>
## 6. Giới hạn đã biết

1. `QA_SANDBOX=none` (mặc định), runtime Claude Code/OpenCode và các session agent chạy trên máy với quyền
   của bạn, **không** sandbox. `claude -p` còn nạp hook và `.mcp.json` của repo đích, nên chỉ trỏ
   `WORKSPACE_ROOT` vào repo tin cậy.
2. Process con (runtime Claude Code, `agw session`, Codex relay, OpenCode server, review, deploy, lệnh QA)
   nhận môi trường đã gỡ bí mật của gateway (`config.child_env`): mọi trường `SecretStr` của `Settings`,
   `AGW_SECRET_KEY`, `OPENCODE_SERVER_PASSWORD`, key provider trong config và key đặt qua `/keys`. Key
   riêng của agent (ví dụ `ANTHROPIC_API_KEY`, `AGW_API_TOKEN`) vẫn giữ. Agent cần key trùng tên key
   provider của gateway (ví dụ OpenCode dùng `OPENAI_API_KEY`) phải tự đăng nhập (`opencode auth login`).
   `env_passthrough` của MCP server vẫn chuyển đúng biến được khai báo, kể cả bí mật.
3. Prompt xin quyền từng tool của Claude Code không được chuyển lên Telegram; approval của gateway bao trùm
   cả một lượt chạy agent.
4. Plugin là code Python chạy trong process. Trần quyền chỉ kiểm soát **tool** đi qua policy engine; plugin
   độc hại vẫn gọi được API Python trực tiếp. Chỉ bật plugin tin cậy.
5. MCP server là process cục bộ với cwd là workspace. Chỉ allowlist tool đã xem xét.
6. Đơn người dùng: một allowlist, không quota, không cách ly tenant. Không có rate limit cho REST; giữ API ở
   localhost hoặc sau proxy có xác thực.
7. Quick Action token nằm trong bộ nhớ: sau restart nút cũ hết hiệu lực, approval được hỏi lại.
8. Auto-accept duyệt cả câu hỏi secure khi đang bật.
9. Attachment, diff và tài liệu được gửi tới provider bên ngoài xử lý step tương ứng (OpenAI, DeepSeek,
   Gemini, fal, Meshy, Anthropic qua Claude Code).

## 7. Báo cáo lỗ hổng

Không mở issue công khai. Làm theo [SECURITY.md ở thư mục gốc](../SECURITY.md) (GitHub security advisory,
kèm version/commit, cách tái hiện, mức ảnh hưởng). Phạm vi và các phiên bản được hỗ trợ cũng nêu ở đó.
