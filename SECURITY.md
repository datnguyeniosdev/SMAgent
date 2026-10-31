# Chính sách bảo mật

## Báo cáo lỗ hổng

Vui lòng không mở issue công khai. Hãy báo riêng qua
[GitHub security advisories](https://github.com/datnguyeniosdev/SMAgent/security/advisories/new), kèm:

- phiên bản hoặc commit bị ảnh hưởng
- các bước tái hiện
- mức độ ảnh hưởng

Bạn sẽ nhận phản hồi trong vòng 7 ngày. Khi bản sửa được phát hành, bạn sẽ được ghi công, trừ khi bạn
không muốn.

## Phiên bản được hỗ trợ

Dự án đang ở giai đoạn alpha, nên chỉ bản mới nhất trên `main` được sửa lỗi.

## Phạm vi

Trong phạm vi:
- REST API của gateway và MCP server `/mcp`
- kiểm soát truy cập Telegram (danh sách cho phép, xác nhận bằng passcode, nhập key)
- cách xử lý bí mật
- các lệnh setup có thay đổi cấu hình của agent

Ngoài phạm vi: các giới hạn đã ghi trong [docs/SECURITY.md](docs/SECURITY.md), ví dụ runtime của agent
chạy bằng quyền người dùng mà không có sandbox.

Cách gateway được bảo vệ được mô tả trong [docs/SECURITY.md](docs/SECURITY.md).
