# Báo cáo repository: `design-os-system-one`

**Ngày lập:** 14/10/2026
**Workspace:** `<workbench-root>`
**Phạm vi:** chức năng, cài đặt, cách chạy, cấu hình, kiểm chứng và phần còn thiếu theo report tổng.

### Chức năng và cấu trúc

Đây là monorepo cho “System 1” — xử lý các quyết định nhỏ, có cấu trúc (chọn intent, chấm điểm, xác suất yes/no, chọn phần tử giao diện) bằng mô hình phân loại thay vì gọi LLM sinh token cho từng quyết định. Tài liệu mô tả kiến trúc hai tầng: Laya-MLX cục bộ trên Apple Silicon, sau đó mới chuyển trường hợp thiếu tự tin sang TypeSafe JEV cloud API. Các mốc độ trễ trong README là số liệu do dự án công bố; chưa đo lại trên máy này.

- `packages/jev-shared-contract`: TypeScript schema và logic chia sẻ như router, guardrail, shortlist, memory compactor, stop oracle và estimator.
- `packages/laya-mlx`: runtime inference MLX cho Apple Silicon, Python 3.11+, macOS 14+; hỗ trợ `choice`, `score`, `noul`.
- `projects/jev-browser-cli`: CLI tự động hóa browser qua Chrome DevTools Protocol, mặc định dùng browser đang mở trên cổng CDP 9222.
- `projects/voice-browser-agent`: web app điều khiển Chromium bằng giọng nói, dùng Web Speech API và JEV; README có heuristic mock để trải nghiệm không cần API key.
- `projects/laya-jev-lab`: thử nghiệm cascade và benchmark.
- `skills/`, `docs/`: skill cho agent và tài liệu kiến trúc/API.

### Đã cài

- Workspace Node bằng `corepack pnpm install --frozen-lockfile`; ba lifecycle scripts của dependency `esbuild`, `protobufjs`, `sharp` ban đầu bị pnpm chặn. Sau khi cấp phép đúng ba dependency này, cài đặt hoàn tất; các binary native đã được tải/xây dựng.
- Chromium và FFmpeg cho Playwright của `voice-browser-agent`.
- `packages/laya-mlx/.venv` với Python 3.12.15, `laya-mlx`, `mlx`, `mlx-metal`, `numpy`, `huggingface-hub`, `tokenizers` và các dependency runtime còn lại.
- Cấu hình `pnpm-workspace.yaml` có thêm danh sách `allowBuilds` cho ba package nêu trên để pnpm cho phép build script trong lần cài tiếp theo.

### Chạy

```sh
# Voice browser agent; server được khóa trên loopback
cd projects/voice-browser-agent
corepack pnpm start

# JEV browser CLI; cần Chrome/Chromium chạy với remote debugging port 9222
cd projects/jev-browser-cli
corepack pnpm start

# Dùng mô hình MLX local từ Python 3.12 environment
cd packages/laya-mlx
.venv/bin/python -c 'import laya_mlx; print("laya_mlx ready")'
```

Voice agent dùng Chromium do Playwright quản lý. API key TypeSafe nằm tại `projects/jev-browser-cli/.env` và `projects/voice-browser-agent/.env`; file mode `0600`, Git ignore. JEV CLI và Voice Agent đều đã gửi request thật tới TypeSafe `jev-latest`, nhận quyết định và điều khiển Chromium tới trang GitHub repo; `/api/health` trả HTTP 200 và báo key đã nạp. Quota/billing chưa được kiểm tra. Server hiện chạy tại `127.0.0.1:3000`; bind loopback đã được thêm vì endpoint điều khiển browser không có auth. Chrome CDP chạy tại `127.0.0.1:9222`.

Đã tải ba checkpoint Laya (English, multilingual, typed-decisions) vào Hugging Face cache, cài extras `rich` và `Pillow`, rồi chạy inference local thật trên English quickstart và câu hỏi tiếng Việt multilingual; cả hai trả nhãn `billing`, lần VN confidence `0.9892`. Đây là hai yêu cầu inference đơn lẻ, không đại diện đánh giá độ chính xác. Không chạy `scripts/setup-laya.sh`: script đó clone thêm upstream Laya ngoài workspace. Không chạy test suite.
