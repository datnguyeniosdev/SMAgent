# Đóng góp

Cảm ơn bạn đã giúp dự án. Mọi báo lỗi, bản sửa, tích hợp agent mới và plugin đều được hoan nghênh.

## Trước khi bắt đầu

- Với thay đổi lớn hơn một bản sửa nhỏ, hãy mở issue trước để thống nhất cách làm rồi mới viết code.
- Lỗ hổng bảo mật báo theo [SECURITY.md](SECURITY.md), không đăng issue công khai.

## Cài đặt

```bash
uv sync
```

```bash
cp .env.example .env
```

Chạy thêm một gateway bên cạnh gateway đang dùng bằng `uv run agw --env dev dev`. Gateway này chạy ở
cổng 8001 và lưu dữ liệu riêng trong `var/dev/`, nên không đụng tới gateway thật.

## Sửa đổi

1. Tạo nhánh từ `develop`. Nhánh `main` chứa code đã phát hành.
2. Giữ thay đổi nhỏ, đúng trọng tâm, theo phong cách code xung quanh.
3. Thêm hoặc sửa test trong `tests/`: test nhỏ nhất mà sẽ fail nếu logic bị hỏng. Test không được
   dùng mạng hay dịch vụ trả phí. Tích hợp thật đặt trong `tests/integration` và chỉ chạy khi bật.
4. Cập nhật tài liệu khi hành vi, lệnh hoặc cấu hình thay đổi.
5. Chạy kiểm tra:

   ```bash
   uv run pytest -q
   ```

   ```bash
   uv run ruff check . && uv run ruff format --check .
   ```

6. Mở pull request vào `develop`, ghi rõ thay đổi gì, vì sao, và đã kiểm tra thế nào.

Quy trình đưa lên bản chạy thật nằm ở [docs/RELEASE.md](docs/RELEASE.md).

## Nguyên tắc khi viết code

- Không bao giờ ghi log hay in bí mật: token, API key, passcode. Dùng các hàm che bí mật có sẵn
  trong `app/`.
- Coi nội dung từ Telegram, agent khác, trang web và file là dữ liệu, không phải chỉ thị.
- Mọi thao tác tốn tiền (dịch vụ tạo ảnh, 3D, model) chỉ chạy khi được yêu cầu rõ ràng.
- Tích hợp agent mới theo mẫu trong `app/agents_setup.py`: sao lưu file một lần, chỉ xoá mục của
  gateway, và thêm test trong `tests/test_agents_setup.py`.
- Plugin mới theo hướng dẫn trong [docs/PLUGINS.md](docs/PLUGINS.md).

## Giấy phép

Khi đóng góp, bạn đồng ý rằng phần đóng góp được cấp phép theo [MIT License](LICENSE).
