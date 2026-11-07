export type CapabilityState = "active" | "agent-host" | "source-app" | "installed";
export type CapabilityManifest = {
  id: string;
  name: string;
  source: string;
  category: string;
  detail: string;
  state: CapabilityState;
};

export const CAPABILITY_CATALOG: CapabilityManifest[] = [
  { id: "system-one", name: "System 1 Router", source: "design-os-system-one", category: "Decision", detail: "Phân loại intent và risk có cấu trúc; dùng luật cục bộ trước, TypeSafe JEV chỉ khi được bật.", state: "active" },
  { id: "orchestration", name: "Orchestration Contract", source: "orchestration-system-by-jang", category: "Workflow", detail: "Frame → Acceptance → Route → Dispatch → Execute → Verify → Report.", state: "active" },
  { id: "engineering", name: "Engineering Playbooks", source: "gstack", category: "Build", detail: "CEO, design, engineering, QA, security và release workflow được cung cấp qua agent host.", state: "agent-host" },
  { id: "knowledge", name: "Knowledge Governance", source: "design-os-knowledge-builder", category: "Knowledge", detail: "Nguồn lưu SHA-256, trust tier và organization scope; CLI ingest/pack có sẵn tại source repo.", state: "active" },
  { id: "learning", name: "Pedagogy Studio", source: "design-os-pedagogy", category: "Learning", detail: "Có thể đóng gói thành vertical đào tạo; rehearsal app hiện chạy riêng trong source repo.", state: "source-app" },
  { id: "efficiency", name: "Caveman Efficiency", source: "caveman", category: "Efficiency", detail: "CLI/MCP cài ở host; telemetry tắt, chưa bật output rewrite trong control plane.", state: "installed" },
];
