# Báo cáo repository: `orchestration-system-by-jang`

**Ngày lập:** 14/10/2026
**Workspace:** `<workbench-root>`
**Phạm vi:** chức năng, cài đặt, cách chạy, cấu hình, kiểm chứng và phần còn thiếu theo report tổng.

### Chức năng

Đây là bộ operating procedure cho việc giao việc và giám sát AI agent, không có app/server hoặc package runtime. Luồng được định nghĩa là `FRAME → ACCEPTANCE → ROUTE → DISPATCH → EXECUTE → VERIFY → REPORT`; có dispatch contract, worker preamble, adapter cho Claude/Codex/Cursor/Gemini và các invariant về no-op/refusal, xác minh độc lập, quản lý trạng thái và cleanup.

### Cài đặt và bật sử dụng

Không có `package.json`, `requirements.txt` hoặc runtime dependency cần cài. Đã cài skill vào `<user-home>/.codex/skills/orchestration-system-by-jang/`, tạo `orchestration-config.md` theo cấu hình Codex hiện tại và thêm pointer vào `<user-home>/.codex/AGENTS.md`.

Thiết lập Codex hiện tại dùng một model tier `gpt-6-luna`, reasoning effort `high`, Git/file snapshot pin, plans/reports theo project và approval gates mặc định. Setup report với dry-run task spec và acceptance IDs A1/C1/V1 nằm cạnh config. Skill và config đã cài để dùng trong Codex; đây không phải tiến trình nền. Chưa có task cụ thể nào được giao qua workflow này trong lượt chạy, nên chưa phát sinh worker hay execution report.

### Cách dùng và kiểm tra

```sh
cat <user-home>/.codex/skills/orchestration-system-by-jang/SKILL.md
cat <user-home>/.codex/skills/orchestration-system-by-jang/orchestration-config.md
```

Skill được nạp theo chỉ dẫn trong context của Codex; mở phiên mới nếu chưa nhận skill/config mới. Khi dùng, mô tả task, phạm vi, acceptance criteria và điều kiện xác minh. Quy trình yêu cầu kiểm tra độc lập worker output trước khi coi task hoàn tất.

### Trạng thái và giới hạn

- Đây là quy trình vận hành agent, không có app/server để chạy.
- Cài đặt user-level đã hoàn tất; model/quota còn tùy quyền truy cập Codex hiện tại.
- Chưa dispatch task/worker hay kiểm chứng end-to-end cho một task; không có workload nền để chạy liên tục.
- Adapter được thiết kế cho nhiều harness, nhưng kết quả thực thi tùy agent/model và tích hợp có sẵn trên máy.
