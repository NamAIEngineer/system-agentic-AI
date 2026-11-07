# Báo cáo repository: `gstack`

**Ngày lập:** 14/10/2026
**Workspace:** `<workbench-root>`
**Phạm vi:** chức năng, cài đặt, cách chạy, cấu hình, kiểm chứng và phần còn thiếu theo report tổng.

### Chức năng

Kho skill Markdown biến coding agent thành các vai trò workflow: product/CEO review (`/office-hours`, `/plan-ceo-review`), architecture/engineering (`/plan-eng-review`), design, debugging, code review (`/review`), QA/browser (`/qa`, `/browse`), security (`/cso`), release (`/ship`, `/land-and-deploy`) và ghi nhớ workflow. Repo cũng có CLI/browser binaries và công cụ build. Đây là bộ điều khiển agent, không phải một sản phẩm web độc lập.

### Đã cài

- Bun `1.4.3` được gọi qua `npm exec --yes --package=bun@1.4.3`; `bun install --frozen-lockfile` đã cài 233 package.
- `bun run build` đã tạo các skill đã biên dịch, digest cho agent và binaries/browser bundle.
- Không cài Aside. README yêu cầu macOS 15+ cho tích hợp Aside; nếu không dùng Aside, browser fallback dùng bundled Chromium khi cần.

### Dùng

`./setup --host codex` đã đăng ký global; dùng các skill như `/gstack-office-hours`, `/gstack-review`, `/gstack-qa` và `/gstack-ship`. `setup --status` báo global install current và 25/25 section links hợp lệ. Nó đồng thời phát hiện bản generated trong `.agents/skills` của checkout này như project install chưa đăng ký; bản global là bản đã đăng ký để sử dụng. `/cso` có chức năng cần native toolchain; tích hợp provider thứ hai cần cài/auth CLI tương ứng.

### Phiên chạy thật

Browse daemon đang chạy ở chế độ `launched`, báo `healthy`, 1 tab và đã mở `https://github.com/jangtrinh/design-os-system-one` (HTTP 200). Đây là daemon tương tác tạm thời của phiên máy hiện tại, tự dừng theo giới hạn runtime; khởi động lại bằng `/browse` hoặc `npm exec --yes --package=bun@1.4.3 -- bun run browse/src/cli.ts` từ thư mục repo. Các workflow skill còn lại chạy theo từng yêu cầu trong Codex, không phải tiến trình nền. Không chạy test suite.
