# Kết nối agent với Agent Gateway

Tài liệu này mô tả cách một phiên AI coding (Claude Code, Codex, Cursor, OpenCode, Antigravity) kết
nối vào gateway, nhận việc từ Telegram hoặc từ agent khác, trả lời, báo cáo và hỏi operator. Mọi
lệnh, tham số và tên tool dưới đây lấy từ code (`app/cli.py`, `app/global_setup.py`,
`app/repo_setup.py`, `app/waiter.py`, `app/mcp_server.py`, `app/channel.py`, `app/agents_setup.py`).
Cấu hình `.env` và `config/` xem ở [CONFIGURATION.md](CONFIGURATION.md).

Gateway chỉ nghe trên `127.0.0.1`. Cổng theo môi trường: `prod` 8000, `dev` 8001, `uat` 8002
(`ENV_PORTS` trong `app/config.py`). Config của các agent luôn trỏ về prod; `dev`/`uat` không bao giờ
ghi lại chúng.

## 1. Cài đặt và kết nối

### 1.1 Lệnh `agw setup`

| Lệnh | Tác dụng |
|---|---|
| `uv run agw setup` | Tạo/bổ sung secret trong `.env` (`init-env`), cài global setup cho Claude Code, rồi gợi ý các agent khác đã phát hiện |
| `uv run agw setup --repo <path> [<path>...]` | Chỉ ghi file theo từng repo (CLAUDE.md, `.mcp.json`, `.claude/settings.local.json`) |
| `... --repo <path> --project "<Tên>"` | Đặt tên project (chỉ khi có đúng một repo; mặc định lấy từ README) |
| `... --repo <path> --kind product\|tool` | Ghi loại project vào registry của gateway (xem CONFIGURATION.md) |
| `... --repo <path> --template` | Repo template: chỉ khối CLAUDE.md với tên giữ chỗ `<GAME NAME> (<GAME CODE>)`, không ghi `.mcp.json`/settings |
| `uv run agw setup --agents opencode,codex,antigravity,cursor` | Kết nối agent khác; cũng nhận `detected`, `all`, `claude` |
| `uv run agw setup --agents <list> --remove` | Gỡ cấu hình của các agent đó |
| `uv run agw setup --agents <list> --print-rules` | In đoạn quy tắc gateway, không cài |
| `uv run agw setup --remove` | Gỡ global setup của Claude Code |
| `uv run agw --env dev\|uat setup` | Tạo `.env.<env>` riêng (không đụng prod và config agent) |

Cờ chung: `--host` (mặc định `127.0.0.1`), `--port` (mặc định theo môi trường). Các tên cũ
`init-env`, `mcp-setup`, `global-setup`, `repo-setup` vẫn chạy như alias.

Một repo được coi là project khi có `.git`, `AGENTS.md` hoặc `CLAUDE.md`. Tên project: dòng
"Game Identity" trong README dạng `Name (CODE)`, nếu không có thì tiêu đề README, cuối cùng là tên
thư mục.

### 1.2 Global setup (Claude Code, mọi repo dưới thư mục projects)

`agw setup` (prod) ghi vào tài khoản Claude Code (`$CLAUDE_CONFIG_DIR` hoặc `~/.claude`):

| Nơi | Nội dung |
|---|---|
| user MCP `agent-gateway` | Không token: stdio proxy `agw mcp-stdio --url <gateway>/mcp` (tự kết nối lại khi gateway khởi động lại). Có token (`MCP_REQUIRE_TOKEN=true`): HTTP với header `Authorization: Bearer ...`, không tự kết nối lại |
| user MCP `agent-gateway-channel` | stdio shim `agw channel` (đẩy việc vào phiên mở bằng `agw session`) |
| `settings.json` → `permissions.allow` | `mcp__agent-gateway__<tool>` cho mọi tool trong `TOOL_NAMES`, cộng `mcp__agent-gateway-channel__reply` |
| `settings.json` → `hooks` | `SessionStart` → `agw hook-session-start`; `UserPromptSubmit` → `agw hook-prompt`; `StopFailure` → `agw hook-failure`; `PreToolUse` (matcher `Edit\|Write\|MultiEdit\|NotebookEdit\|Bash`) → `agw hook-tool`; mỗi hook timeout 10 s |
| `skills/agent-gateway/SKILL.md` | Skill `agent-gateway` (cách báo cáo, nhận việc Telegram, art, review) |

Lần sửa đầu tiên giữ bản sao `settings.json.agw-backup`. `--remove` gỡ đúng những gì đã thêm (giữ lại
user MCP `agent-gateway`).

Vai trò của các hook:

| Hook | Làm gì |
|---|---|
| `hook-session-start` | In gợi ý khởi động (session start hint) vào context: tên project, cron an toàn, lệnh `agw wait`. Im lặng ngoài repo đã kết nối |
| `hook-prompt` | Mỗi prompt: báo gateway phiên đang hoạt động lại; nếu waiter đã chết thì nhắc khởi động lại; nếu hội thoại là 👑 main riêng thì nhắc dùng đúng `session_id` của nó |
| `hook-failure` | Lượt kết thúc vì lỗi API (rate limit, billing...): việc Telegram chưa trả lời được chuyển cho operator |
| `hook-tool` | Chặn Edit/Write/Bash trong 👑 main (trừ lệnh `agw`), và chặn khi request yêu cầu skill/lệnh bắt buộc mà phiên chưa dùng |

### 1.3 Repo setup (`agw setup --repo`)

| File | Nội dung |
|---|---|
| `CLAUDE.md` | Khối quản lý giữa `<!-- agent-gateway:start ... -->` và `<!-- agent-gateway:end -->`; tạo mới (import `AGENTS.md` nếu có), cập nhật khối cũ, hoặc thay các mục viết tay cũ. Có thêm mục Deploy nếu repo có `4_delivery/deploy/deploy.sh` |
| `.mcp.json` | `agent-gateway-channel` (stdio, `agw channel --project <tên>`; thêm `env.AGW_URL` khi gateway không phải `http://127.0.0.1:8000`) và `agent-gateway` (stdio `agw mcp-stdio`, hoặc HTTP với `Bearer ${AGW_API_TOKEN}` khi bắt buộc token) |
| `.claude/settings.local.json` | allow các tool gateway, hook `SessionStart` (`agw hook-session-start --project <tên>`), `enabledMcpjsonServers` gồm hai server trên |

`.mcp.json` và `settings.local.json` chứa đường dẫn cục bộ: không copy sang máy khác. Lệnh cảnh báo
nếu `.claude/settings.local.json` chưa được git-ignore. File theo repo được ưu tiên hơn global setup
ở nơi chúng tồn tại.

### 1.4 Sau khi setup: khởi động lại phiên

Claude Code chỉ nạp MCP server, hook và CLAUDE.md lúc phiên bắt đầu. Nếu thiếu tool
`mcp__agent-gateway__*`: terminal `/exit` rồi `agw session --continue` (hoặc `claude --continue`);
desktop app mở phiên mới trong thư mục đó.

## 2. Vòng nhận việc (pull loop)

### 2.1 Khởi động phiên

Hook SessionStart yêu cầu Claude, trước mọi việc khác:

1. `CronList`, xoá (`CronDelete`) mọi job có prompt nhắc tới `get_messages` hoặc `/health`.
2. Tạo đúng một cron an toàn `7 */3 * * *` (3 giờ một lần, recurring) với prompt vòng nhận việc. Cron này chỉ để
   bắt trường hợp waiter chết mà không ai biết.
3. Chạy waiter một lần bằng Bash `run_in_background: true`:
   `"<venv>/bin/agw" wait --project '<project>' --cwd '<folder>' [--client app|cli]`.

Không bao giờ chạy hai waiter cho một phiên; waiter mới nhất thắng, waiter cũ tự thoát.

### 2.2 `agw wait`

| Đặc điểm | Chi tiết |
|---|---|
| Cơ chế | Long-poll `/api/v1/channel/wait` (timeout 60 s), không tốn lượt model khi rảnh |
| Thoát khi | Có request mới trong hàng đợi: in ra hướng dẫn (gọi `get_messages` với `session_id` cụ thể) rồi exit, đánh thức Claude |
| Nhắc lại | Request vẫn còn trong hàng đợi sau 600 s kể từ lần đánh thức thì đánh thức lại |
| Gateway tắt | Chờ im lặng, thử lại mỗi 30 s |
| `/close` của operator | In "do not restart the waiter" và thoát; hook prompt không khởi động lại |
| Trạng thái | `var/waiters/<session id>.json` |
| Cờ | `--project`, `--cwd` (mặc định `.`), `--client`, `--agent claude_code\|antigravity` |

Sau khi trả lời, chạy lại waiter (kết quả `reply_message` cũng nhắc lệnh). Nếu app khởi động lại và
waiter mất, hook `UserPromptSubmit` nhắc chạy lại.

### 2.3 `get_messages` và trạng thái busy

`get_messages(project, session_id, cwd, limit=1, agent, client)`: `agent` là tool của bạn
(`claude_code`...), `client` là `app` (desktop/IDE) hoặc `cli` (terminal). Dùng lại `session_id` mà
lần gọi trước trả về. Các ghi chú có thể nhận:

| Ghi chú | Phải làm |
|---|---|
| `busy` | Phiên của thư mục đang làm request trong `in_progress_messages`. Request đó chỉ được hoàn tất bởi hội thoại đã nhận nó (kể cả trước khi restart hoặc tóm tắt). Nếu bạn chưa từng thấy nó, một hội thoại khác của thư mục đang làm: để yên, không làm gì |
| switched off | Operator đã `/off`; không làm gì (chờ `/on`) |
| closed | Operator đã `/close`: không chạy lại waiter, xoá cron `get_messages`, dừng poll |
| several sessions | Gọi lại kèm `cwd` và `agent` |
| no new requests | Không làm gì |

Mỗi lần chỉ một request: khi còn request chưa trả lời, gateway không giao việc mới.

### 2.4 Các loại tin nhắn (`meta.kind`)

| `meta.kind` | Nguồn | Cách trả lời |
|---|---|---|
| (không có) / `request` | Operator (Telegram `/send`, reply) hoặc agent khác (`ask_agent`) | Làm việc, rồi `reply_message(session_id, message_id, text)`. Trả lời cho agent khác đi về agent đó, không lên Telegram |
| `answer` | Trả lời cho `ask_agent`, `request_human_input`, `delegate_task`, `review_project`, `generate_art` của chính bạn | Dùng để tiếp tục; không reply |
| `offer` | Gateway hỏi bạn có nhận request không (chưa phải việc) | Không bắt đầu. Gọi ngay `bid(offer_id, accept, session_id, context, reason)`; không có tool `bid` thì `reply_message(message_id=<offer>, text='YES <context>' hoặc 'NO <lý do>')` |

`context` của bid: `high|medium|low|none` (bao nhiêu việc đã làm trong phiên mà request cần). Chỉ
accept khi đang rảnh. Nếu thắng, request đến qua `get_messages`.

## 3. Báo cáo

| Tool | Khi nào |
|---|---|
| `reply_message(text, session_id, message_id, image_paths)` | Trả lời một request đã nhận qua `get_messages`. Trả lời một lần, kèm kết quả: trả lời sớm kiểu "đang làm" sẽ đánh dấu xong |
| `submit_report(text, project, task_id?, session_id, agent_status, images?, image_paths?, handoff_id?)` | Kết thúc một task không đến qua `get_messages` (việc trong phiên, hoặc `delegate_task`). Không có `task_id`: gateway tạo task chỉ-ghi-nhận và trả `task_id` |
| `report_deployment(project, environment, status, ...)` | Sau mỗi lần chạy script deploy |

Quy ước nội dung (`reply_message`, `submit_report`): dòng tiêu đề, tối đa 5 gạch đầu dòng (đã đổi / đã
kiểm tra / chưa làm), tối đa 500 ký tự, không log, tiếng Việt lịch sự (mở đầu "Dạ,").
`agent_status`: `completed|partial|blocked|failed|in_progress`.

Ảnh chụp màn hình: `image_paths` là đường dẫn tuyệt đối tới file PNG/JPEG/WebP/GIF; `images` là
`[{"name": "...", "data_base64": "..."}]` khi không có file. Phản hồi Telegram cho báo cáo nằm trong
`get_task_result(task_id).feedback`.

`report_deployment`: `status` là `succeeded|failed|partial|dry_run|started|cancelled`, theo exit code
thật. Tuỳ chọn: `version`, `build_number`, `commit`, `platforms`, `targets`, `url`, `notes`, `error`,
`task_id`, `session_id`. Với build thật: `notes` là release note tiếng Việt, `url` là link build.

## 4. Hỏi operator

- `request_human_input(question, project, session_id, options=[...], ttl_s?, task_id?, secure=false)`:
  dùng cho mọi xác nhận/quyết định, kể cả khi đã hỏi trong app. `options` là 2-4 nhãn nút. Câu trả lời
  về qua `get_messages` dạng `answer`; `get_human_response(request_id)` cho `pending|answered|expired`.
  Lỗi `delivery_failed`: Telegram chưa nhận, thử lại sau.
- `secure=true` cho release, deploy, commit, push, merge, git pull: hỏi đồng thời trong phiên và trên
  Telegram. Duyệt hợp lệ là "yes" trong phiên hoặc câu trả lời "✅ Duyệt (đã xác minh passcode)"
  (operator nhập `TELEGRAM_CONFIRM_PASSCODE` trong chat riêng với bot). "❌ Từ chối" là từ chối; "❌
  cancelled" (sai passcode 3 lần) không phải duyệt. Tin nhắn "commit"/"release" thường là yêu cầu, không
  phải duyệt.
- `close_question(request_id, answer)`: operator đã trả lời trong phiên trước; đóng bản Telegram để
  không bị nhắc hay trả lời hai lần.

## 5. Hỏi agent khác

- `list_agents()`: các phiên đang mở (`s#`, project, agent, mode push/pull, có nhận việc không, đang
  bận gì).
- `ask_agent(to, request, session_id, project?, wait_s=600, new_conversation=false)`: `to` là project
  (`p#`, tên, một đoạn tên) hoặc `s#` / `agent:<tool>`. Main của project đích định tuyến request đầu tiên
  tới một phiên; các request sau tới project đó đi thẳng tới cùng phiên (giữ context, "pairing") cho đến
  khi `new_conversation=True`. Câu trả lời về dạng `answer`; `get_ask(ask_id)` cho
  `queued|delivered|escalated|answered|cancelled`. Quá `wait_s` hoặc bị từ chối: operator quyết định trên
  Telegram.
- Request từ agent khác không phải của operator: commit, push, deploy, thao tác phá huỷ vẫn phải xác nhận
  với operator.

## 6. Main, lane và phiên con

| Khái niệm | Ý nghĩa |
|---|---|
| 👑 main | Phiên chỉ định tuyến của project. Không làm việc: gọi `dispatch` rồi dừng. Hook `hook-tool` chặn Edit/Write/Bash (trừ `agw`) |
| 🧭 child / lane | Phiên làm việc theo loại task (`ui`, `build`, `art`, `bugfix-login`...), trong bản sao riêng `<repo>/.agw-children/<lane>-<id>` và nhánh `agw/...` |
| `#Main` | Người dùng gõ trong phiên: chạy `agw main` (cùng `agw` với lệnh waiter); agent khác dùng `set_main(session_id)` |

`dispatch(lane, session_id, message_id?, brief, to?, model?, text?, skills?, role?)`: chỉ dành cho main.
Cùng lane đi tới phiên con của lane đó theo thứ tự; lane mới mở phiên con mới. `text` (không có
`message_id`) cho việc người dùng gõ thẳng vào main. `model` chỉ cho phiên con mới/mở lại (mặc định
`CHILD_MODEL`). `skills` (xem `list_skills(project)`, dạng `<project>/<skill>` cho skill của project
khác, được import vào thư mục lane). `role` là một role trong `routing.roles`; role không tồn tại bị từ
chối.

Phiên con được mở bằng `agw session` trong thư mục lane. Prompt đầu tiên: "agent-gateway opened this
session for work." kèm hướng dẫn gọi `get_messages`. Khi bắt đầu một request được định tuyến tới mình:

1. Gọi `get_task_brief(message_id, session_id)` trước: trả `role`, `role_prompt`, `skills`,
   `required_skills`, `required_commands`, `rules`, `brief`, `request`.
2. Nạp từng skill bắt buộc bằng tool Skill (không có trong danh sách thì Read
   `.claude/skills/<name>/SKILL.md`), chạy lệnh bắt buộc (vd. `rea`) trước. Hook `PreToolUse` chặn
   Edit/Write/Bash cho đến khi transcript cho thấy chúng đã được dùng sau khi request đến. Nguồn của yêu
   cầu bắt buộc: skill của role, `skills` của dispatch, `CORE_SKILLS` của project,
   `task_rules[].require_command`.
3. Làm việc, commit trên nhánh `agw/` của mình (commit này không cần duyệt), không merge khi chưa được
   operator duyệt, rồi `reply_message`.

Việc dài: gọi `still_working(session_id, note)` mỗi 15 phút. Không có nó, request chưa trả lời sau 60
phút bị coi là kẹt, và phiên con trông rảnh 30 phút bị đóng.

## 7. Tham chiếu tool MCP (`TOOL_NAMES`)

Operator có thể tắt từng tool bằng Telegram `/mcp`; khi đó tool trả lỗi "switched off". Gọi thiếu
`session_id` thì gateway dùng session_id mà kết nối MCP đó dùng lần trước.

| Tool | Mục đích | Tham số chính |
|---|---|---|
| `get_messages` | Lấy request/answer/offer của project | `project`, `session_id`, `cwd`, `agent`, `client`, `limit` |
| `reply_message` | Trả lời một request (Telegram hoặc agent hỏi) | `text`, `session_id`, `message_id`, `image_paths` |
| `submit_report` | Báo cáo kết thúc task lên Telegram | `text`, `project`, `task_id`, `agent_status`, `images`, `image_paths` |
| `report_deployment` | Thông báo deploy | `project`, `environment`, `status`, `url`, `notes`, ... |
| `request_human_input` | Hỏi operator, có nút | `question`, `options`, `project`, `session_id`, `secure`, `ttl_s` |
| `get_human_response` | Trạng thái câu hỏi | `request_id` |
| `close_question` | Đóng bản Telegram khi đã trả lời trong phiên | `request_id`, `answer` |
| `ask_agent` | Nhờ agent/project khác | `to`, `request`, `session_id`, `wait_s`, `new_conversation` |
| `get_ask` | Trạng thái một ask | `ask_id` |
| `list_agents` | Liệt kê phiên đang mở | — |
| `dispatch` | Main định tuyến việc tới lane | `lane`, `session_id`, `message_id`, `brief`, `text`, `to`, `model`, `skills`, `role` |
| `get_task_brief` | Role, skill, rule, brief của request được định tuyến | `message_id`, `session_id` |
| `list_skills` | Skill có thể giao cho request của project | `project` |
| `bid` | Trả lời offer | `offer_id`, `accept`, `session_id`, `context`, `reason` |
| `still_working` | Báo vẫn đang làm việc dài | `session_id`, `note` |
| `set_main` | Biến phiên thành 👑 main (chỉ khi người dùng yêu cầu `#Main`) | `session_id` |
| `delegate_task` | Giao task cho workflow của gateway | `objective`, `task_type`, `domain`, `workflow_id`, `data`, `project`, `session_id` |
| `get_task_status` | Trạng thái task và các bước | `task_id` |
| `get_task_result` | Kết quả, handoff, report, `feedback`, `images`/`models` | `task_id` |
| `cancel_task` | Huỷ task (task do operator mở chỉ operator huỷ được bằng `/stop`) | `task_id` |
| `list_capabilities` | Capability đã cấu hình và trạng thái | — |
| `get_capability_details` | Schema, executor đủ điều kiện của một capability | `capability_id` |
| `generate_art` | Tạo ảnh (fal.ai) hoặc model 3D (`output="model3d"`, Meshy); trả phí, operator duyệt rồi review | `prompt`, `purpose`, `requirements`, `width`, `height`, `transparent`, `project`, `session_id`, `output`, `image_path`, `model_format`, `texture` |
| `review_project` | Review/research độc lập bằng skill game-review | `project`, `mode` (benchmark\|parity\|playtest\|ideation), `role` (qa\|po\|both), `agent` (claude\|droid\|codex\|gemini\|opencode\|all), `model`, `evidence`, `ref`, `session_id` |
| `get_review` | Trạng thái review: running, done (`files`), failed (`problems`) | `review_id` |

Server `agent-gateway-channel` có thêm tool `reply` cho tin nhắn push
(`<channel source="agent-gateway-channel">`), truyền `message_id` của event.

`generate_art`: `width`/`height` 16-3840; chỉ dùng ảnh khi review là accepted; file ở
`get_task_result(...).images[].path` hoặc `.models[].path` (3D). Lỗi "no image provider is on" nghĩa là
fal tắt trong `config/providers/models.yaml` hoặc thiếu `FAL_KEY`.

## 8. Agent khác Claude Code

| Agent | File được ghi bởi `agw setup --agents <agent>` | Cách được đánh thức |
|---|---|---|
| OpenCode | `~/.config/opencode/opencode.json` (`mcp.agent-gateway`, type remote), `~/.config/opencode/AGENTS.md` | `agw session --agent opencode` trong repo: mỗi phút hỏi `/api/v1/channel/peek`, chỉ chạy `opencode run` khi có việc |
| Codex | `~/.codex/config.toml` (khối `[mcp_servers.agent-gateway]` có đánh dấu, `default_tools_approval_mode = "approve"`), `~/.codex/AGENTS.md`, symlink skill vào `~/.codex/skills`, `~/.codex/hooks.json` | Desktop: hook SessionStart/SessionEnd ghi lại thread mới nhất của thư mục, gateway đẩy việc vào thread bằng `codex queue`; hook Stop chuyển câu hỏi cuối lượt lên Telegram. CLI: `agw session --agent codex` (`codex exec`) |
| Antigravity | `~/.gemini/config/mcp_config.json` (`serverUrl`), `~/.gemini/GEMINI.md` | Chạy `agw wait --agent antigravity` một lần trong terminal của agent: nó tự tách nền và đánh thức hội thoại bằng `agentapi send-message`, chạy mãi (dừng sau 3 lần đánh thức thất bại). Cần `ANTIGRAVITY_CONVERSATION_ID` |
| Cursor | `~/.cursor/mcp.json`, `~/.cursor/hooks.json` (`sessionStart`, `afterAgentResponse`, `stop` → `agw cursor-hook`) | `sessionStart` đưa quy tắc gateway vào context; `stop` long-poll tới ~3300 s, khi có việc trả `followup_message` để Cursor tự làm tiếp. Duyệt hook mới nếu Cursor hỏi |

Mỗi file sửa lần đầu có bản sao `*.agw-backup`. Khi `MCP_REQUIRE_TOKEN=true`, các file chỉ ghi tên biến
`AGW_API_TOKEN`; agent phải có biến đó trong môi trường. Telegram `/open p# <agent>` mở phiên của agent
khác. Sau khi setup, khởi động lại agent.

## 9. Xử lý sự cố

| Triệu chứng | Cách xử lý |
|---|---|
| Không có tool `mcp__agent-gateway__*` | Khởi động lại phiên (mục 1.4). Kiểm tra `claude mcp list`; chạy lại `uv run agw setup` |
| Waiter thông báo "get_messages tool is missing or fails" | MCP chưa kết nối: báo người dùng một dòng, khởi động lại phiên |
| Tool mất sau khi gateway restart (HTTP MCP) | HTTP không tự kết nối lại; dùng `/mcp` trong Claude Code hoặc mở lại phiên. Không token thì setup dùng `agw mcp-stdio` (tự kết nối lại, chờ tối đa 120 s) |
| `401` / token required từ `/mcp` | `MCP_REQUIRE_TOKEN=true`: export `AGW_API_TOKEN` cho agent, hoặc đặt `false` và restart gateway |
| Waiter không chạy (app khởi động lại) | Hook prompt nhắc; chạy lại lệnh `agw wait` trong nền. Chỉ một waiter mỗi phiên |
| Waiter in "Replaced by a newer waiter" | Bình thường, không chạy lại cái đó |
| Waiter in "The operator closed this session" | Không chạy lại; chờ operator mở lại |
| Hỏi quyền cho một tool gateway sau khi cập nhật bản mới | Danh sách allow được sinh từ `TOOL_NAMES` lúc setup; tool mới chưa có trong đó. Chạy lại `uv run agw setup` (hoặc `--repo` với repo dùng file riêng) rồi khởi động lại phiên |
| `get_messages` báo busy | Hoàn tất và trả lời request trong `in_progress_messages` nếu chính hội thoại này đã nhận; nếu không, để yên |
| `get_messages` báo switched off | Operator đã `/off`; chờ `/on` |
| Tool trả "switched off by the operator (Telegram /mcp)" | Làm tiếp không có nó hoặc nhờ operator bật lại |
| Bị chặn "this conversation is its project's 👑 main" | Dùng `dispatch`, không làm việc trong main |
| Bị chặn "this request requires ..." | Nạp skill/chạy lệnh bắt buộc đã nêu, rồi làm tiếp |
| `agw session`: "has no channels" | Cần Claude Code >= 2.1.80 |
| Kiểm tra tổng thể | `uv run agw doctor`; `curl http://127.0.0.1:8000/health` |
