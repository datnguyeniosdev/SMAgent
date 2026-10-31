# Nhật ký thay đổi

Ghi lại các thay đổi đáng chú ý. Định dạng theo
[Keep a Changelog](https://keepachangelog.com/vi/1.1.0/), phiên bản theo
[Semantic Versioning](https://semver.org/lang/vi/).

## [Chưa phát hành]

### Định tuyến và session
- Một router duy nhất cho mọi dự án: Gateway Agent là phiên Claude CLI do gateway tự mở và giữ
  sống (`GATE_CLI`). Tình trạng của gate được theo dõi qua PID trong `/health`.
- Dự án có hai kiểu định tuyến: `central` (gateway định tuyến) và `main` (session chính của dự án
  định tuyến). Mỗi dự án có id ổn định, giữ được tên cũ làm bí danh.
- Loại dự án: `product` (mặc định) chỉ có một worker (`dev`) và một session deploy dùng lại. `tool`
  (QA, UI/UX, PO, research…) chạy nhiều session, mỗi chủ đề một lane.
- Câu hỏi tiếp theo về cùng chủ đề quay về đúng session đang làm việc đó. Yêu cầu giữa các agent
  được ghép cặp với session nhận ngay khi giao.
- Mỗi lane chỉ được mở một lần: hết cảnh một yêu cầu mở ra nhiều session khi tạo worktree chậm.
- Offer/bid (`BID_PROJECTS`, chỉ cho dự án tool): chủ đề mới được hỏi các session rảnh trước. Session
  đồng ý và có nhiều ngữ cảnh nhất sẽ nhận việc.
- Session con nhận lời nhắc đầu tiên để lấy việc, được mang theo quy tắc và skill của dự án, và nhận
  role (`uiux`, `art`, `qa`, `po`, `gamedev`) cùng `get_task_brief`.
- Skill và lệnh bắt buộc được hook `PreToolUse` kiểm tra. Mọi tác vụ dịch ngược mã nguồn bắt buộc
  dùng `rea`.
- `#Main`: một cuộc hội thoại có thể trở thành session chính của dự án cho tới khi ngắt kết nối.
- Session chính chỉ định tuyến; việc gõ trực tiếp vào đó được chuyển đi.
- Claude Code kết nối tới gateway qua proxy stdio (`agw mcp-stdio`), tự kết nối lại sau khi gateway
  khởi động lại.

### Telegram
- Kênh thông báo hệ thống riêng (`TELEGRAM_NOTICE_CHAT_ID`). Task, ask, báo cáo và câu hỏi vẫn ở
  nhóm điều khiển.
- Câu hỏi cần passcode vẫn giữ đủ các lựa chọn của agent. Câu trả lời ghi rõ lựa chọn đã chọn, và
  lựa chọn kiểu "Chưa…", "Không…", "Để sau" không cần passcode.
- Auto-accept (nút 🌙, lệnh `/auto`): bật bằng passcode, tự trả lời câu hỏi khi người vận hành vắng.
- Câu hỏi đã trả lời sẽ mất nút trên Telegram. Câu hỏi đã giải quyết trong session không bị gửi lại.
- Ảnh chụp màn hình được giữ lại cùng câu trả lời của agent.

### Dữ liệu
- Dọn dữ liệu hằng ngày (`DATA_RETENTION_DAYS`), không xoá task của session đang sống. Bản sao lưu cũ
  hơn 3 ngày được tự xoá.
- Game Intelligence: bằng chứng, quan sát và nhận định có kiểu, không bị dọn theo hạn giữ dữ liệu.

### Tích hợp
- Codex, Cursor và Antigravity: câu hỏi chỉ hỏi trong chat cũng được gửi lên Telegram, và câu trả lời
  đánh thức session.
- `agw wait`: chờ việc bằng long-poll phía server, nên session rảnh không tốn gì.
- Việc bị kẹt (giới hạn tần suất, lỗi API, quá hạn) được chuyển cho người vận hành quyết định:
  chuyển session, chờ, hoặc huỷ.

### Tài liệu
- Viết lại toàn bộ tài liệu bằng tiếng Việt. Xoá các audit, kế hoạch theo phase và bằng chứng demo
  cũ.

## [0.1.0] - 2026-10-20

### Thêm mới
- Phiên bản đầu: gateway với REST API, điều khiển qua Telegram, MCP server và workflow engine, kèm
  plugin phần mềm, tài liệu, dữ liệu, media và Unity.
