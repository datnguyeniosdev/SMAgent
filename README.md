# Agent Gateway

Agent Gateway là một gateway chạy cục bộ (FastAPI + asyncio) nhận việc từ Telegram của operator và
từ các agent khác, rồi chuyển việc đó tới các session AI coding (Claude Code, Codex, Cursor, OpenCode,
Antigravity) đang mở trên máy, trên nhiều project game/tool. Gateway có một MCP server cho agent, một
Telegram bot cho operator và lưu trạng thái trong SQLite.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)
![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)

## Tính năng chính

- **Điều khiển từ Telegram.** Gửi việc (`/send s#|p# <text>`), xem session và project (`/list`),
  hàng đợi (`/queue`), huỷ (`/cancel m#`), mở/đóng session (`/open`, `/close`), bật/tắt session
  (`/on`, `/off`), chọn main (`/main`), đổi model (`/model`), chế độ định tuyến (`/route`), deploy
  (`/deploy`). Chỉ user trong allowlist của group điều khiển mới dùng được.
- **Một bộ định tuyến.** Với project ở chế độ `central`, Gateway Agent (Gate) đọc từng yêu cầu mới và
  gọi `dispatch` để giao cho đúng lane. Với chế độ `main`, session main của chính project định tuyến.
- **Product và tool.** Project `product` có đúng hai session: worker (lane `dev`, làm tuần tự) và
  session deploy (lane `deploy`). Project `tool` (QA, UI/UX, PO, research…) chạy song song, mỗi chủ
  đề một session.
- **Lane trong bản sao riêng.** Mỗi lane mới làm việc trong một bản sao của project dưới
  `<project>/.agw-children/<lane>-xxxx` trên nhánh `agw/<lane>-xxxx`; rule và skill của project được
  chép theo.
- **Thức khi có việc, không polling bằng model.** Session pull chờ bằng `agw wait` (long-poll, không
  chạy model); session push nhận việc qua channel. Không model nào chạy khi không có việc.
- **Agent hỏi agent.** `ask_agent` gửi yêu cầu sang project/session khác; câu trả lời quay về đúng
  session đã hỏi, và session nhận được ghép cặp với người hỏi cho các câu hỏi tiếp theo.
- **Câu hỏi và phê duyệt.** `request_human_input` đưa câu hỏi có nút bấm lên Telegram, nhắc lại mỗi 30
  phút. Câu hỏi `secure=true` (release, deploy, commit, push) chỉ được duyệt từ Telegram bằng passcode.
  Có chế độ auto-accept (`/auto`, cần passcode).
- **Báo cáo.** Câu trả lời của session thành report trên Telegram; reply vào report quay lại đúng
  session đó.
- **Watchdog.** Lease, việc bị kẹt, việc không ai nhận, sức khoẻ của Gate và dọn dữ liệu cũ hằng ngày.
- **Workflow engine.** Ngoài phần session, gateway còn chạy workflow khai báo bằng YAML trong
  [`config/`](config/) qua REST API (plugin software, documents, data, media, unity, game_research).

## Yêu cầu

- macOS (gateway mở session lane trong Terminal.app qua `osascript`; bản sao lane dùng `cp -c`
  clone APFS, nếu không được thì `git worktree`).
- git, [uv](https://docs.astral.sh/uv/) (installer tự cài nếu thiếu; uv mang theo Python 3.12).
- Claude Code CLI (`claude`) cho Gate và các session lane.
- Một Telegram bot và group điều khiển (không bắt buộc, nhưng cần để điều khiển từ xa).

## Bắt đầu nhanh

Cài đặt (idempotent, chạy lại được):

```bash
scripts/install.sh --agents detected
```

hoặc từng bước:

```bash
uv run agw setup
```

`agw setup` tạo `.env` với secret cục bộ và cài phần global cho Claude Code (MCP server
`agent-gateway`, skill, hook). Muốn có Telegram, export `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CONTROL_CHAT_ID`, `TELEGRAM_ALLOWED_USER_IDS` (và `TELEGRAM_REPORT_CHANNEL_ID`) trước khi chạy
installer. Kết nối thêm agent khác: `uv run agw setup --agents opencode,codex,antigravity,cursor`.

Chạy gateway:

```bash
uv run agw dev
```

```bash
curl -s http://127.0.0.1:8000/health
```

Kết nối một repo (viết khối gateway vào `CLAUDE.md` và `.mcp.json` của repo):

```bash
uv run agw setup --repo <path> [--kind product|tool]
```

Sau đó khởi động lại agent để nạp MCP server và hook, mở thư mục project và gửi `/menu` cho bot.

| Lệnh | Mục đích |
|---|---|
| `uv run agw setup [--agents LIST\|detected\|all] [--remove]` | secret cục bộ + cài đặt global |
| `uv run agw setup --repo PATH… [--kind product\|tool]` | cài file theo repo |
| `uv run agw dev` | chạy gateway (và OpenCode nếu bật) |
| `uv run agw stop` | dừng gateway |
| `uv run agw doctor` | xem cấu hình và những gì truy cập được |
| `uv run agw validate` | kiểm tra `config/` |
| `agw session [--agent …] [--model …]` | (trong repo) mở Claude Code nhận việc qua push channel |
| `agw wait` | (agent tự chạy) chờ việc Telegram của thư mục này |
| `agw main` | (hook `#Main`) biến conversation này thành main được ghim của project |
| `agw mcp-stdio` | proxy MCP qua stdio cho Claude Code, tự nối lại sau khi gateway khởi động lại |

Môi trường `prod`, `dev`, `uat` chạy song song (cổng 8000/8001/8002, file `.env`, `.env.dev`,
`.env.uat`), chọn bằng `--env` hoặc `AGW_ENV`.

## Cấu trúc repo

| Đường dẫn | Nội dung |
|---|---|
| `app/` | mã nguồn gateway |
| `config/` | cấu hình YAML: agents, capabilities, providers, routing, workflows, policies, mcp_servers |
| `docs/` | tài liệu |
| `scripts/` | `install.sh`, smoke test, demo |
| `tests/` | test pytest (`tests/integration` là opt-in) |
| `examples/` | dữ liệu mẫu |
| `var/` | dữ liệu chạy: `state.db`, log, waiter, artifact (không commit) |

Các module chính trong `app/`:

| Module | Vai trò |
|---|---|
| `main.py` | app FastAPI, endpoint REST/channel, MCP tại `/mcp`, watchdog 30 giây |
| `channel.py` | `ChannelHub`: session, pull/push, định tuyến (main/central), Gate, lane và child, dispatch, offer/bid |
| `project_registry.py` | project với id ổn định, alias, `routing_mode`, `kind`, thư mục gốc |
| `asks.py` | yêu cầu giữa các agent (`ask_agent`), ghép cặp, leo thang lên operator |
| `collaboration.py` | report, feedback, câu hỏi, passcode, auto-accept |
| `orchestrator.py` | task, workflow engine, dọn dữ liệu (retention) |
| `mcp_server.py` | các tool MCP (`TOOL_NAMES`) |
| `mcp_proxy.py` | proxy stdio `agw mcp-stdio` |
| `waiter.py` | `agw wait`: long-poll chờ việc cho session pull |
| `channel_shim.py` | MCP stdio "channel" đẩy việc vào session `agw session` |
| `cli.py` | lệnh `agw` và các hook (SessionStart, UserPromptSubmit, PreToolUse, StopFailure) |
| `telegram_bot.py` | bot Telegram (aiogram): lệnh, nút, câu hỏi, thông báo |
| `skills.py` | danh mục skill (project, global, project khác) và import vào lane |
| `store.py` | SQLite: `records`, `events`, `idempotency` |
| `launcher.py` | mở session trong cửa sổ Terminal |
| `codex_relay.py` | đẩy việc vào thread Codex desktop (chỉ prod) |
| `deploys.py`, `reviews.py` | `/deploy` và `review_project` |

## Tài liệu

| Chủ đề | Tài liệu |
|---|---|
| Kiến trúc và luồng định tuyến | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Vận hành qua Telegram | [docs/OPERATOR_GUIDE.md](docs/OPERATOR_GUIDE.md) |
| REST API | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#rest-api) |
| Kết nối agent | [docs/AGENT_CONNECT.md](docs/AGENT_CONNECT.md) |
| Cấu hình | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| Plugin | [docs/PLUGINS.md](docs/PLUGINS.md) |
| Workflow | [docs/WORKFLOWS.md](docs/WORKFLOWS.md) |
| Mô hình bảo mật | [docs/SECURITY.md](docs/SECURITY.md) |
| Phát hành | [docs/RELEASE.md](docs/RELEASE.md) |
| Lịch sử thay đổi | [CHANGELOG.md](CHANGELOG.md) |
| Đóng góp | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Báo lỗ hổng bảo mật | [SECURITY.md](SECURITY.md) |

## Phát triển

```bash
uv sync
```

```bash
uv run pytest -q
```

```bash
uv run ruff check app tests
```

Test không gọi mạng hay dịch vụ trả phí. Test tích hợp thật là opt-in:

```bash
RUN_INTEGRATION=1 uv run pytest tests/integration -rs
```

## Giới hạn

- Một người dùng, một máy: một allowlist, một tiến trình.
- Agent chạy với quyền của bạn trên máy; chỉ trỏ gateway vào repo bạn tin tưởng.
- Alpha: giao diện và cấu hình có thể đổi giữa các phiên bản (xem [CHANGELOG.md](CHANGELOG.md)).

## Giấy phép

[MIT](LICENSE) © 2026 Darren Nguyen
