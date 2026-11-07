# Báo cáo repository: `design-os-knowledge-builder`

**Ngày lập:** 14/10/2026
**Workspace:** `<workbench-root>`
**Phạm vi:** chức năng, cài đặt, cách chạy, cấu hình, kiểm chứng và phần còn thiếu theo report tổng.

### Chức năng

CLI/agent skill biên dịch tài liệu web, PDF, codebase và video thành knowledge packs theo UKMC.v1. Mỗi knowledge node hướng tới YAML frontmatter có nguồn, URI, SHA-256, license và trust tier; nguồn ngoài được đánh dấu untrusted/quarantine. Sáu validator kiểm tra schema và tính toàn vẹn. Đầu ra gồm `SKILL.md` điều hướng theo progressive disclosure, nội dung Markdown/YAML/Mermaid và index/manifest cho agent.

Đây là compiler và governance gate cho tri thức, không phải hệ thống RAG host sẵn. README tuyên bố xử lý local/zero telemetry; việc đưa nguồn vào vẫn tùy thuộc vào công cụ và nguồn người dùng chọn.

### Đã cài

- Host Python 3.9.6 không đạt điều kiện `requires-python >=3.10` của project.
- Cài uv `0.13.1`, uv-managed Python `3.12.15`, tạo `.venv` và cài project editable bằng `uv sync`.
- Khai báo dependencies runtime rỗng; extra `dev` mới thêm pytest, extras `audio`/`ocr` chưa khai báo package.

```sh
cd design-os-knowledge-builder
.venv/bin/knowledge-builder --help
.venv/bin/python scripts/self_check.py
```

Self-check đã chạy thành công. CLI đã ingest thật sáu URL GitHub được yêu cầu; cả sáu trả về modality `web` cùng SHA-256 nguồn:

| Nguồn | SHA-256 |
|---|---|
| `jangtrinh/design-os-system-one` | `72fd789f043cb8bd02e71102d1e1747e193bac66d25898da0cd94feeff64e4ec` |
| `jangtrinh/orchestration-system-by-jang` | `d8b1b6fe48d74ddf882cb01e2f6b6a1c620168e35789bb19080040828bb88feb` |
| `garrytan/gstack` | `6f7953b1877220d52a0fa62eea88f47c33603044b1a4f083a4004ec7f0900e0b` |
| `jangtrinh/design-os-pedagogy` | `61e0b0c3483be246d121587c93f5c2b085f79e6d75689aec7e65a2f9e819338e` |
| `jangtrinh/design-os-knowledge-builder` | `3ebbbf4427d63eaeb5300b36816aa07cf531f4c58e65344988df4b471baa7ade` |
| `juliusbrussee/caveman` | `65e23f57101d746c98de00b42e90207f291039044eb3e26fce468f35a56a4c5b` |

Ingest là trích xuất/ghi nhận nguồn của CLI, không tự tạo knowledge pack. Không tạo pack đầu ra vì chưa có yêu cầu về tên, schema/domain hoặc nơi lưu; repo không chạy như daemon. CLI có lệnh `pack` và cổng `lint`/`verify` để xây và kiểm tra pack sau khi xác định đầu ra mong muốn. Không chạy test suite.
