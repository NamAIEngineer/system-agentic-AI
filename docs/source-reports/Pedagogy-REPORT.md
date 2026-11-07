# Báo cáo repository: `design-os-pedagogy`

**Ngày lập:** 14/10/2026
**Workspace:** `<workbench-root>`
**Phạm vi:** chức năng, cài đặt, cách chạy, cấu hình, kiểm chứng và phần còn thiếu theo report tổng.

### Chức năng và trạng thái sản phẩm

Gồm hai phần: kho tri thức/ontology về sư phạm và ứng dụng local rehearsal luyện dạy toán phân số tiếng Việt. Vòng rehearsal cho phép người dạy chọn cách dạy, ghi lời giải thích, xem phản hồi gắn với transcript, làm bài chuyển giao và lưu phản tư. Session/event được lưu trong SQLite, có revision, idempotency, reopen/replay và export Markdown/JSON.

Giới hạn được README.vi nêu rõ: hiện có ba tình huống phân số; câu trả lời tự soạn được lưu nhưng chưa chấm semantic; không gọi model ngoài; không chứng nhận năng lực giảng dạy. Review chuyên gia và pilot người dùng thật chưa hoàn tất. App là dịch vụ cục bộ một người dùng, chưa phải hệ thống public nhiều tài khoản.

### Đã cài và chạy

Đã tạo `.venv` dùng Python host `3.9.6`, cài `PyYAML 6.0.3`, `markdown-it-py 3.0.0`, `mdurl 0.1.2` theo `requirements.txt`.

```sh
cd design-os-pedagogy
.venv/bin/python run_rehearsal.py --port 0
```

Mở URL local mà launcher in ra. Mặc định SQLite ở `.data/rehearsal.sqlite3`; dùng `--data-dir` nếu cần tách dữ liệu. Pipeline module dùng `tools/pedagogy_pipeline.py`; hướng dẫn dry-run/apply và recovery nằm trong `README.vi.md` và `docs/publication.md`.

Tiến trình hiện chạy bằng `.venv/bin/python -u run_rehearsal.py --port 8876` tại `http://127.0.0.1:8876`; trang chủ trả HTTP 200. Đây là app local thật, lưu dữ liệu vào workspace theo cấu hình mặc định. Chưa tạo bài/session rehearsal và chưa đánh giá kết quả học tập; app cũng không gọi model ngoài theo thiết kế.

Tài liệu `docs/local-workspace.md` nhắc tới một checkout và hostname khác. Nội dung đó không khớp đường dẫn checkout hiện tại; khi chạy nên dùng repo tại workspace đang có, không dùng đường dẫn cũ trong tài liệu.
