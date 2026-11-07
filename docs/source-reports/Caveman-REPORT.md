# Báo cáo repository: `caveman`

**Ngày lập:** 14/10/2026
**Workspace:** `<workbench-root>`
**Phạm vi:** chức năng, cài đặt, cách chạy, cấu hình, kiểm chứng và phần còn thiếu theo report tổng.

### Chức năng

Caveman là bộ tối ưu coding-agent có các phần riêng bật/tắt được:

- Voice/skill: làm câu trả lời ngắn hơn nhưng giữ nguyên code, lệnh và dữ liệu chính xác.
- Read less: nén log/JSON/CSV/test output trước khi agent đọc.
- Auto: chọn model/effort theo task; cần account đăng nhập hoặc provider tương ứng.
- Blocks: lưu và tái dùng script thao tác repo.
- Browse: điều khiển browser và trả accessibility snapshot rút gọn.
- Waste fixes/CLI: phân tích sử dụng token, routing agent, MCP tools, middleware/SDK và Caveman Cloud.
- `extension/`: extension Chrome để chèn chỉ dẫn Caveman vào prompt trên ChatGPT, Claude, Gemini; README của extension nói xử lý tại browser và không gọi network.

### Đã cài

- `corepack pnpm@10.14.0 install --frozen-lockfile` cho 14 workspace package, cài 379 package.
- Build `packages/cli` v4.0.1 thành công: generated registry/skills và `packages/cli/dist/index.js`.
- Cài `@caveman-ai/cli@4.0.1` global vào user prefix `<user-home>/.local`; `caveman --version` gọi được từ PATH và báo `4.0.1`.
- CLI báo trạng thái `ready: false` trước khi có companion binaries. Đã chạy `node packages/cli/dist/index.js setup --install --json`; 6 binary cho `darwin/arm64` (`caveman-proxy`, `caveman-engine`, `caveman-mcp`, `cavemem`, `caveman-browse`, `caveman-shrink`) được cài vào `~/.caveman/bin`, checksum đều xác minh theo release `bin-v4.0.1`.
- Đã cài 22 skills Codex; SessionStart hook đã được review và trust riêng trong Codex `/hooks`.
- Đã đăng ký MCP recovery local bằng `caveman tools mcp install`; `codex mcp list` báo server `caveman` enabled.
- Đã chạy `caveman enable codex`; `caveman doctor codex` báo Codex integration `installed`, routing và recovery MCP được cấu hình. Codex CLI hiện yêu cầu trust các hook theo cơ chế host riêng; status cho thấy lifecycle hook/Core chưa active trong lần kiểm tra này. Không bypass cơ chế trust.
- Telemetry được đặt `off` và xác nhận đã lưu từ config.
- Go modules chưa tải được vì máy không có `go`. `go.mod` yêu cầu Go 1.26.9; phần native source build/benchmark cần cài Go phù hợp. Dùng CLI với companion binary phát hành không cần compiler Go.
- `extension/`: `npm ci` hoàn tất; Chromium Playwright đã tải. `npm audit` ghi nhận advisory ở trên. npm cũng bỏ qua lifecycle script của `fsevents`; đây là dependency tùy chọn cho file watching, không cần để load unpacked extension.

### Chạy và điều kiện cần biết

```sh
cd caveman/extension
# Chrome/Brave/Edge: mở chrome://extensions → Developer mode → Load unpacked
# chọn chính thư mục extension/

# Khi muốn tích hợp CLI/skills vào Codex, trước tiên xem installer sẽ nhận diện gì:
cd ..
node installer/install.js --list
node installer/install.js --dry-run --all
```

SessionStart hook từ bộ cài skill đã được review/trust riêng trong Codex `/hooks`; native integration mới của `caveman enable codex` còn cần Codex host trust riêng cho hook hashes mới và nạp ở phiên mới. MCP recovery local đã đăng ký và telemetry đang tắt. Skill install có một Socket alert cho `caveman-discover` và mức Med Risk cho `caveman-review`; các skill khác báo Low Risk/Safe theo installer. Agent-native Caveman Cloud/Auto cần login/account riêng và chưa được kích hoạt. Extension đã cài dependencies và tải Chromium nhưng chưa được load vào profile Chrome.

## Phần còn phụ thuộc credential/tùy chọn

1. Auto/Caveman Cloud cần đăng nhập tài khoản Caveman và các provider workflow cần key riêng. Chưa bật các dịch vụ này.
2. Extension dependency vẫn có advisory Playwright `GHSA-7mvr-c777-76hp`; chưa đổi pinned lockfile.
3. Go 1.26.9+ chỉ cần nếu muốn tự build binaries từ source; binaries phát hành đã cài. Chưa chạy test suite hoặc xác minh mọi integration end-to-end.
