# Cấu hình Agent Gateway

Gateway đọc cấu hình từ ba nơi, theo thứ tự ưu tiên tăng dần: giá trị mặc định trong code < YAML trong
`config/` < biến môi trường (`.env`, class `Settings` trong `app/config.py`). Secret không bao giờ nằm
trong YAML: provider chỉ ghi *tên* biến môi trường (`api_key_env`). Trạng thái theo project (routing
mode, kind) nằm trong database của gateway, đổi bằng Telegram hoặc `agw setup`.

Cách kết nối agent xem ở [AGENT_CONNECT.md](AGENT_CONNECT.md).

## 1. File `.env`

- Tên biến = tên field trong `Settings` viết hoa (vd. `tool_parallel` → `TOOL_PARALLEL`). Biến không
  khớp field bị bỏ qua; giá trị rỗng được coi như chưa đặt.
- Môi trường: `prod` đọc `.env`, `dev`/`uat` đọc `.env.dev`/`.env.uat` (chọn bằng `agw --env <env>` hoặc
  `AGW_ENV`). `dev`/`uat` không được dùng chung `TELEGRAM_BOT_TOKEN` với prod.
- Tạo secret cục bộ: `uv run agw setup` (hoặc `agw init-env`). Mẫu: `.env.example`. Không commit `.env`.
- Không in giá trị secret; kiểm tra có đặt hay chưa bằng `grep -c '^NAME=.' .env`.

### 1.1 Cú pháp danh sách và JSON

| Kiểu field | Cú pháp trong `.env` | Field |
|---|---|---|
| Danh sách, nhận cả hai dạng | JSON (`["a","b"]`) hoặc phân tách dấu phẩy (`a,b`) | `PLUGINS`, `PLUGIN_PERMISSIONS`, `TOOL_PERMISSIONS`, `QA_ENV_PASSTHROUGH`, `OPENCODE_SHELL_ALLOWLIST`, `BID_PROJECTS` |
| Tập id số | Chỉ phân tách dấu phẩy (`123,456`) | `TELEGRAM_ALLOWED_USER_IDS` |
| Danh sách khác / dict | Chỉ JSON | `RETENTION_EXEMPT_DOMAINS`, `MCP_SERVERS`, `CORE_SKILLS`, `PLUGIN_CONFIG`, `QA_COMMANDS` |

### 1.2 Telegram

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | (trống) | Token bot (secret). Trống: tắt Telegram |
| `TELEGRAM_CONTROL_CHAT_ID` | (trống) | Nhóm điều khiển; bắt buộc khi có token |
| `TELEGRAM_REPORT_CHANNEL_ID` | (trống) | Nơi nhận báo cáo (channel, hoặc chính nhóm điều khiển); bắt buộc khi có token |
| `TELEGRAM_NOTICE_CHAT_ID` | (trống) | Kênh riêng cho thông báo hệ thống (phiên vào/ra, routing, gate, skill, lane đóng). Trống: gửi vào nhóm điều khiển |
| `TELEGRAM_ALLOWED_USER_IDS` | (trống) | User id được phép điều khiển; không được rỗng khi có token |
| `TELEGRAM_PROGRESS_UPDATES` | `false` | Gửi cập nhật tiến độ |
| `TELEGRAM_DISCUSSION_CHAT_ID` | (trống) | Discussion Group gắn với channel báo cáo; ghi đè `telegram.discussion_chat_id` trong `config/app.yaml` |
| `TELEGRAM_MODE` | `polling` | `polling` hoặc `webhook` |
| `TELEGRAM_WEBHOOK_URL` | (trống) | URL HTTPS công khai (gateway nhận ở `+ /telegram/webhook`). Trống ở chế độ webhook: `agw dev` mở tunnel cloudflared |
| `TELEGRAM_WEBHOOK_SECRET` | (trống) | Secret webhook, 16-256 ký tự `A-Z a-z 0-9 _ -`; bắt buộc khi `webhook` |
| `TELEGRAM_CONFIRM_PASSCODE` | (trống) | Passcode cho xác nhận quan trọng (release, deploy, commit/push, git pull) từ Telegram; sai 3 lần thì câu hỏi bị huỷ. Trống: những xác nhận đó chỉ có thể bị từ chối từ Telegram |

Một bot chỉ phục vụ một gateway: hai gateway cùng poll một bot sẽ lấy mất tin của nhau.

### 1.3 Gateway, server và bảo mật

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `API_TOKEN` | (trống) | Token REST control API; trống thì các endpoint điều khiển bị tắt |
| `OPERATOR_TOKEN` | (trống) | Token cho quyết định của operator qua REST (duyệt, trả lời câu hỏi). Trống: chỉ qua Telegram |
| `MCP_REQUIRE_TOKEN` | `true` | `false`: `/mcp` không cần token nhưng chỉ phục vụ máy này (client loopback, Host cục bộ, không Origin lạ) |
| `PROJECTS_ROOT` | thư mục cha của gateway | Thư mục chứa các repo mà `/open` và global setup dùng |
| `DATA_DIR` | `var` | Thư mục dữ liệu |
| `STATE_DB` | `state.db` | File SQLite dưới `DATA_DIR` (`:memory:` tắt lưu bền) |
| `WORKSPACE_ROOT` | (trống) | Repo git mà workflow code được phép sửa (qua worktree) |
| `CONFIG_DIR` | `config` | Thư mục YAML; gateway chỉ nạp khi có `app.yaml` |
| `LOG_LEVEL` | `INFO` | Mức log |
| `AGW_SECRET_KEY` | (sinh bởi `agw setup`) | Không thuộc `Settings`; mã hoá các key lưu bằng Telegram `/keys` |
| `AGW_ENV` | `prod` | Chọn môi trường (`prod`, `dev`, `uat`) |

### 1.4 Routing và phiên

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `CHILD_MODEL` | `opus` | Model Claude của phiên con (làm việc). Đổi theo project bằng `/model`, theo task bằng `dispatch(model=)` |
| `ROUTER_MODEL` | `sonnet` | Model của phiên router tạm thời (chỉ định tuyến, báo cáo) |
| `GATEWAY_AGENT_PROJECT` | `Agent Gateway` | Project có main session là Gateway Agent dùng chung: nhận request mà `channel_rules` không xếp được (project ở routing mode `central`) |
| `GATE_CLI` | `false` | Gateway tự mở và giữ sống một phiên CLI làm Gateway Agent (thư mục `.agw-children/gate` của project trên); trong lúc chưa kết nối, request được xếp bằng lane mặc định |
| `TOOL_PARALLEL` | `3` | Số phiên tối đa chạy song song cho lane `task` của một project kind `tool` |
| `BID_PROJECTS` | `[]` | Project (kind `tool`) mà topic mới được *offer* cho các phiên rảnh trước; phiên nhận với nhiều context nhất thắng, không ai nhận thì mở phiên mới. Có thể tắt tạm bằng switch `bid` trong `/mcp` |
| `OFFER_WAIT_S` | `90` | Số giây offer còn mở |
| `OFFER_MAX` | `3` | Số phiên được hỏi tối đa (context tốt nhất trước) |
| `CORE_SKILLS` | `{}` | JSON `project → [skill...]`: skill bắt buộc cho mọi request của project qua gateway (skill đầu dẫn dắt). Hook `PreToolUse` chặn cho đến khi đã nạp. Vd. `{"UIUXAgent": ["uiux-art-director"]}` |
| `DATA_RETENTION_DAYS` | `30` | Task/tin nhắn/log đã xong cũ hơn số ngày này bị xoá hằng ngày; `0` = giữ hết |
| `RETENTION_EXEMPT_DOMAINS` | `["game_research"]` | Domain có task/artifact được giữ làm bằng chứng |

### 1.5 Giới hạn

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `MAX_ATTACHMENT_BYTES` | `20971520` | Kích thước file đính kèm tối đa (giới hạn getFile của Bot API) |
| `TASK_TIMEOUT_S` | `1800` | Timeout một task |
| `MAX_CONCURRENT_TASKS` | `2` | Task chạy đồng thời |
| `ACTION_TTL_S` | `3600` | Thời hạn của action chờ duyệt |
| `MAX_RETRIES` | `2` | Số lần thử lại |
| `MAX_FIX_ROUNDS` | `1` | Số vòng sửa |

### 1.6 Plugin, tool và MCP hạ nguồn

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `PLUGINS` | `software,documents,data,media,unity,game_research` | Module dưới `app.plugins` (hoặc đường dẫn dotted) |
| `PLUGIN_PERMISSIONS` | `workspace:read,workspace:write,process:execute,artifact:write,data:read,provider:image` | Quyền tối đa mà tool plugin được giữ |
| `TOOL_PERMISSIONS` | như trên | Quyền cấp cho bước workflow lúc chạy; `mcp:execute` không được cấp mặc định |
| `PLUGIN_CONFIG` | `{}` | Tuỳ chọn theo plugin, vd. `{"unity": {"asset_dir": "Assets/Generated"}}` |
| `MCP_SERVERS` | `[]` | Danh sách MCP server hạ nguồn (xem `app.mcp_client.McpServerConfig`); chỉ tool trong allowlist được đăng ký |

### 1.7 Runtime và agent

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `CLAUDE_CODE_ENABLED` | `false` | Bật runtime Claude Code CLI |
| `CLAUDE_CODE_BIN` | `claude` | Đường dẫn binary |
| `CLAUDE_CODE_MODEL` | (trống) | Model |
| `CLAUDE_CODE_ALLOWED_TOOLS` | (trống) | `--allowedTools` bổ sung cho implementer |
| `CLAUDE_CODE_MCP_CONFIG` | (trống) | File MCP config truyền qua `--mcp-config` |
| `CLAUDE_CODE_MAX_BUDGET_USD` | (trống) | Ngân sách tối đa |
| `OPENCODE_ENABLED` | `false` | Bật runtime OpenCode (server chạy riêng bằng `opencode serve`) |
| `OPENCODE_URL` | `http://127.0.0.1:4096` | URL server |
| `OPENCODE_USERNAME` | `opencode` | Username |
| `OPENCODE_PASSWORD` | (trống) | Secret, cùng giá trị `OPENCODE_SERVER_PASSWORD` |
| `OPENCODE_MODEL` | (trống) | `providerID/modelID`; trống = model mặc định của server |
| `OPENCODE_SHELL_ALLOWLIST` | `python3 -m pytest,pytest,git status,git diff` | Lệnh shell OpenCode được chạy (khớp tiền tố) |
| `IMPLEMENTER_PREFERENCE` | `claude_code` | Thứ tự connector cho vai implementer |
| `DESIGNER_PREFERENCE` | `openai,claude_code` | Cho vai designer |
| `REVIEWER_PREFERENCE` | `claude_code,openai` | Cho vai reviewer |

### 1.8 QA và kiểm tra asset

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `QA_COMMANDS` | `{}` | JSON `tên → lệnh` (chạy không qua shell) |
| `QA_TIMEOUT_S` | `900` | Timeout lệnh QA |
| `QA_MAX_OUTPUT_BYTES` | `64000` | Output tối đa giữ lại |
| `QA_SANDBOX` | `none` | `none` (subprocess thường, không phải sandbox) hoặc `docker` |
| `QA_DOCKER_IMAGE` | (trống) | Bắt buộc khi `QA_SANDBOX=docker` |
| `QA_ENV_PASSTHROUGH` | `[]` | Biến môi trường bổ sung cho lệnh QA |
| `ASSET_REQUIRE_ALPHA` | `true` | Asset phải có kênh alpha |
| `ASSET_MAX_DIMENSION` | `4096` | Cạnh tối đa |
| `ASSET_POWER_OF_TWO` | `false` | Bắt buộc kích thước luỹ thừa 2 |

### 1.9 Provider model, art và media (chỉ tên biến)

Bật khi key được đặt. Key cũng có thể đặt qua Telegram `/keys` (lưu mã hoá bằng `AGW_SECRET_KEY`,
thắng giá trị trong `.env`; xoá thì quay về `.env`).

| Biến | Đọc ở | Ghi chú |
|---|---|---|
| `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_IMAGE_MODEL`, `OPENAI_BASE_URL` | `Settings`, `config/providers/models.yaml` | Base URL mặc định `https://api.openai.com/v1` |
| `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_IMAGE_INPUT` | `Settings`, `models.yaml` | Model mặc định `deepseek-flash`, base `https://api.deepseek.com`, image input `false` |
| `HTTP_TIMEOUT_S` | `Settings` | Mặc định `120` |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | `models.yaml` | Provider `gemini` |
| `FAL_KEY`, `FAL_MODEL` | `models.yaml` (provider `fal`) | Ảnh của `generate_art`; `FAL_MODEL` mặc định `fal-ai/flux/schnell`. Trạng thái ban đầu `enabled: false`, bật live bằng `/mcp` |
| `MESHY_API_KEY`, `MESHY_IMAGE_MODEL` | `models.yaml` (provider `meshy`), orchestrator | Model 3D (`generate_art(output="model3d")`) |

Phía agent (không đặt trong `.env` của gateway): `AGW_API_TOKEN` (header Bearer khi
`MCP_REQUIRE_TOKEN=true`), `AGW_URL` (shim channel trỏ tới gateway dev/uat).

## 2. Thư mục `config/`

Lệnh kiểm tra: `uv run agw validate [config_dir]`. Mỗi file phải có `schema_version: 1`; cấu hình sai
làm gateway dừng lúc khởi động kèm chẩn đoán. Giá trị trông như secret trong YAML bị báo lỗi.

| File | Khoá gốc | Nội dung |
|---|---|---|
| `config/app.yaml` (bắt buộc) | `app`, `telegram` | Tên/môi trường; `telegram.discussion_chat_id`, `feedback_requires_reply`, `max_report_photos` (0-10) |
| `config/agents/*.yaml` | `agents: [...]` | Agent logic khai báo capability, `executor_preference` |
| `config/capabilities/*.yaml` | `capabilities: [...]` | Capability |
| `config/providers/*.yaml` | `providers: [...]` | Provider model/ảnh/3D, `api_key_env`, `model_env`, `enabled` |
| `config/mcp_servers/*.yaml` | `mcp_servers: [...]` | MCP hạ nguồn: `command`/`url`, `allowed_tools`, `read_only_tools` (mặc định đều `enabled: false`) |
| `config/routing/*.yaml` (đúng một file) | `routing: {...}` | `roles`, `task_rules`, `channel_rules`, `routes`, `feedback_routes`, `default_workflow` |
| `config/workflows/*.yaml` | `workflows: [...]` | Workflow |
| `config/policies/*.yaml` (đúng một file) | `policies: {...}` | `approvals`, `provider_allowlist`, `tool_allowlist`, `mcp_url_hosts`, `artifact_retention_days` |

### 2.1 `config/routing/routing.yaml`: phần phiên sống

**`roles`**: role mà Gateway Agent/main giao bằng `dispatch(role=...)`. Prompt và skill đi kèm request,
skill bị bắt buộc (hook `PreToolUse`). Skill có thể là của project, dùng chung (`~/.claude/skills`), hoặc
`<project>/<skill>` (được import vào thư mục lane).

| Trường | Kiểu | Ý nghĩa |
|---|---|---|
| `description` | chuỗi (bắt buộc) | Mô tả, dùng để chọn role |
| `skills` | danh sách | Skill bắt buộc |
| `prompt` | chuỗi | Prompt của role |

```yaml
routing:
  roles:
    qa:
      description: testing a build, feature, release candidate, regression, IP or asset check
      skills: [QAIPAgent/qa-agent]
      prompt: You are the QA worker for this request; follow qa-agent.
    gamedev:
      description: code work on a product (features, fixes, refactors, tuning, integration)
      skills: [game-dev]
      prompt: You are the developer on this product's dev lane; follow game-dev.
```

Role có sẵn: `uiux`, `art`, `qa`, `po`, `gamedev`.

**`task_rules`**: quy tắc bắt buộc theo nội dung request, áp dụng cho mọi project và mọi cách định
tuyến. Text quy tắc được nối vào request ("MANDATORY rule: ..."); `require_command` khiến hook chặn
Edit/Write và lệnh khác cho đến khi phiên đã chạy lệnh Bash bắt đầu bằng lệnh đó cho request này.

| Trường | Kiểu | Ý nghĩa |
|---|---|---|
| `match` | regex | Áp lên text request |
| `rule` | chuỗi | Quy tắc thêm vào request |
| `require_command` | chuỗi (mặc định rỗng) | Lệnh bắt buộc chạy trước |

```yaml
  task_rules:
    - match: '(?i)reverse[- ]?engineer|decompil|\bapk\b|jadx|ghidra'
      rule: Reverse engineering: always use rea first (`rea capabilities`, `rea analyze <target>`).
      require_command: rea
```

**`channel_rules`**: chỉ cho project ở routing mode `central`. Rule đầu tiên khớp (glob project và regex
text) quyết định lane; gateway giao thẳng cho phiên con của lane, không cần LLM. Không khớp: Gateway
Agent đọc và gọi `dispatch`; Gateway Agent offline thì gateway tự chọn lane mặc định.

| Trường | Kiểu | Ý nghĩa |
|---|---|---|
| `project` | glob (mặc định `*`) | So với tên project đã chuẩn hoá |
| `match` | regex | Áp lên text request |
| `lane` | chuỗi | Lane đích |

```yaml
  channel_rules:
    - {project: "*", match: "(?i)^\\s*\\[qa\\]", lane: qa}   # "[qa] retest level 3"
```

Các khoá còn lại (`routes`, `feedback_routes`, `default_workflow`, `plugin_fallback`,
`dynamic_planning`) điều khiển workflow của `delegate_task`/Telegram; `dynamic_planning.enabled: true`
không được hỗ trợ.

## 3. Project registry

Mỗi project có id ổn định (`prj_...`); tên hiển thị đổi được (tên cũ giữ làm alias). Lưu trong
database của gateway (`DATA_DIR/STATE_DB`), đăng ký khi gặp lần đầu.

| Trường | Giá trị | Mặc định | Ý nghĩa |
|---|---|---|---|
| `routing_mode` | `main` \| `central` | `main` | `main`: 👑 main của project định tuyến. `central`: gateway định tuyến (`channel_rules`, rồi Gateway Agent) |
| `kind` | `product` \| `tool` | `product` | `product`: repo game/app, một worker trên code (lane dev, theo thứ tự), deploy và QA ở lane riêng. `tool`: project agent (UIUX, PO, QA, research), request chạy song song tới `TOOL_PARALLEL` phiên |
| `root` | đường dẫn | (từ phiên) | Thư mục project; lane được copy từ đây |

Cách đổi:

| Thao tác | Lệnh |
|---|---|
| Xem routing mode và kind của mọi project, trạng thái Gateway Agent | Telegram `/route` |
| Đổi routing mode | Telegram `/route p# central` hoặc `/route p# main` |
| Đổi kind | Telegram `/route p# tool` hoặc `/route p# product` |
| Đặt kind khi setup repo | `uv run agw setup --repo <path> --kind product\|tool` (không có `--kind` thì giữ nguyên; project mới là `product`) |
