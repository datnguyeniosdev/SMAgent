# Quy trình release

Tài liệu này mô tả cách đưa thay đổi từ nhánh `develop` lên bản chạy thật (prod). Mỗi bước release
đều cần người vận hành duyệt trước.

## Môi trường

| | Thư mục | Nhánh | Cổng | Dữ liệu |
|---|---|---|---|---|
| Phát triển | repo chính (`local_agent_tool`) | `develop` | 8001 khi chạy `uv run agw --env dev dev` | `var/dev/` |
| Prod | worktree riêng (ví dụ `~/Working/.agw-prod`) | `main` | 8000 | `var/` của worktree prod |

Prod chạy trong một tab terminal riêng bằng `uv run agw dev` và được điều khiển qua Telegram.
File `.env` của prod nằm trong worktree prod, không commit.

## Duyệt trước khi làm

- Commit, merge, push, deploy, release và `git pull` đều phải được duyệt.
- Agent hỏi đồng thời ở hai nơi: trong session (câu hỏi kèm nút) và trên Telegram bằng
  `request_human_input(secure=true)`. Duyệt ở nơi nào trước thì tính nơi đó.
- Trên Telegram, chỉ câu trả lời đã nhập đúng passcode mới được tính là duyệt. Tin nhắn thường như
  "commit" hay "release" chỉ là yêu cầu, chưa phải duyệt. Câu trả lời của auto-accept cũng không
  tính là duyệt release.
- Nếu người vận hành trả lời trong session trước, agent gọi `close_question(request_id, answer)` để
  đóng bản trên Telegram.

## Các bước

1. **Commit trên `develop`.** Chỉ add đúng các file đã sửa (`git add <file>…`, không dùng `-A`). Lời
   commit nói rõ thay đổi gì và vì sao. Cuối lời commit có dòng
   `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
2. **Kiểm tra trên `develop`:**

   ```bash
   uv run pytest -q
   ```

   ```bash
   uv run ruff check app tests && uv run ruff format --check app tests
   ```

3. **Đưa lên worktree prod** (chỉ fast-forward), rồi chạy lại test ngay trong prod:

   ```bash
   git -C ~/Working/.agw-prod merge --ff-only develop
   ```

   ```bash
   cd ~/Working/.agw-prod && uv run pytest -q
   ```

4. **Xem việc đang dở.** Trong `var/state.db` của prod, đếm các task `running`/`queued` và các tin
   `channel_msg` ở trạng thái `pending`, `queued`, `delivered`, `stalled`. Tin nhắn được lưu trong DB
   nên vẫn còn sau khi khởi động lại. Task đang chạy sẽ bị ngắt và được báo lại cho người vận hành.
5. **Đổi cấu hình nếu cần.** Sửa `.env` của prod (ví dụ `BID_PROJECTS`), rồi kiểm tra giá trị đọc
   được bằng Settings, không in giá trị bí mật.
6. **Dừng prod:** Ctrl-C trong tab đang chạy, rồi chờ cổng 8000 trống.
7. **Chạy lại prod:**

   ```bash
   cd ~/Working/.agw-prod && uv run agw dev
   ```

8. **Chờ `/health`** trả lời. Sau đó chờ gate khoẻ lại (`gate.healthy` trong `/health`): gateway tự
   mở lại phiên CLI của Gateway Agent.
9. **Chạy setup khi quy tắc, công cụ MCP hoặc quyền thay đổi:**

   ```bash
   uv run agw setup
   ```

   ```bash
   uv run agw setup --agents detected
   ```

   Cập nhật từng repo dự án (ghi lại khối quy tắc trong `CLAUDE.md`, `.mcp.json`,
   `settings.local.json`):

   ```bash
   uv run agw setup --repo <repo> [<repo> …]
   ```

   Repo template thêm `--template`. Thư mục lane đã có (`<repo>/.agw-children/*`) chạy từng thư mục
   với `--project "<tên dự án>"`.
10. **Báo cáo** lên Telegram bằng `submit_report`, tiếng Việt, tối đa 500 ký tự: đã thay đổi gì, đã
    kiểm tra gì, chưa kiểm tra gì.

## Sau khi release

- Session Claude đang mở chỉ thấy công cụ MCP mới sau khi khởi động lại hoặc kết nối lại MCP.
  Trong lúc đó, offer vẫn trả lời được bằng `reply_message` ("YES …" hoặc "NO …").
- Xem `var/agw.log` có lỗi `ERROR` sau giờ khởi động không. Lỗi Telegram ngay trước giờ khởi động
  thường là của tiến trình cũ lúc tắt.
- Kiểm tra `/health`, danh sách session (`/api/v1/channel/sessions`) và các tin `stalled`.

## Quay lui

Chuyển worktree prod về commit trước (`git -C ~/Working/.agw-prod checkout <commit>` hoặc reset nhánh
`main` khi đã được duyệt), sau đó làm lại bước 6 đến bước 8. Dữ liệu trong `var/` không bị đụng tới.
Trước các thao tác dọn dữ liệu lớn, sao lưu `var/state.db` (bản sao lưu cũ hơn 3 ngày được tự xoá).
