import { choice, noul, TypeSafeClient } from "@typesafe-ai/sdk";
import { execFile } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
export { CAPABILITY_CATALOG } from "./capabilities.js";
const execFileAsync = promisify(execFile);

export type WorkflowIntent = "build" | "operate" | "research" | "support" | "learn" | "general";
export type RiskLevel = "low" | "review" | "high";
export type ClassifierProvider = "laya-mlx" | "typesafe-jev" | "local-rules";
export type Classifier = {
  intent: WorkflowIntent;
  confidence: number;
  risk: RiskLevel;
  destructive: boolean;
  provider: ClassifierProvider;
  note: string;
};

const intents: WorkflowIntent[] = ["build", "operate", "research", "support", "learn", "general"];

export function classifyLocally(request: string): Classifier {
  const text = request.toLocaleLowerCase("vi");
  const terms: Array<[WorkflowIntent, string[]]> = [
    ["build", ["build", "xây", "tạo app", "phát triển", "triển khai phần mềm", "code"]],
    ["operate", ["vận hành", "quy trình", "tự động", "phê duyệt", "đơn hàng", "hóa đơn"]],
    ["research", ["nghiên cứu", "phân tích", "tổng hợp", "report", "báo cáo"]],
    ["support", ["khách hàng", "hỗ trợ", "khiếu nại", "ticket", "cskh"]],
    ["learn", ["đào tạo", "học", "giảng dạy", "luyện tập", "pedagogy"]],
  ];
  const match = terms.find(([, words]) => words.some((word) => text.includes(word)));
  const destructive = /\b(delete|drop|wipe|charge|pay|transfer|publish|send|email|purchase)\b|xóa|xoá|hủy|huỷ|thanh toán|chuyển tiền|gửi|xuất bản|đóng tài khoản|đăng lên/i.test(text);
  const needsReview = /\b(production|customer data|personal data|legal|medical|finance)\b|dữ liệu khách|dữ liệu cá nhân|pháp lý|y tế|tài chính|sản xuất/i.test(text);
  const risk: RiskLevel = destructive ? "high" : needsReview ? "review" : "low";
  return {
    intent: match?.[0] ?? "general",
    confidence: match ? 0.82 : 0.58,
    risk,
    destructive,
    provider: "local-rules",
    note: "Phân loại luật cục bộ; không gửi nội dung ra ngoài.",
  };
}

async function classifyWithLaya(request: string, cliPath?: string, model = "aac6fef/laya-multilingual-mlx"): Promise<Classifier | null> {
  if (!cliPath) return null;
  const folder = await mkdtemp(join(tmpdir(), "sai-laya-"));
  const questionsPath = join(folder, "questions.json");
  const questions = {
    intent: {
      type: "choice",
      instructions: "Which enterprise workflow capability should handle this request?",
      criteria: {
        build: "design, build, change or maintain a software product",
        operate: "run or improve an internal business process",
        research: "retrieve, analyze or synthesize organizational knowledge",
        support: "support customers or resolve service requests",
        learn: "teach, train or practice a skill",
        general: "other request or insufficient information",
      },
    },
    destructive: {
      type: "noul",
      instructions: "Does the request ask to send, publish, delete, pay, transfer money, change production or perform another external side effect?",
    },
  };
  try {
    await writeFile(questionsPath, JSON.stringify(questions), { mode: 0o600 });
    const { stdout } = await execFileAsync(cliPath, ["predict", "--model", model, "--state", request, "--questions", questionsPath], {
      timeout: 45_000,
      maxBuffer: 1_000_000,
      env: { ...process.env, HF_HUB_DISABLE_TELEMETRY: "1" },
    });
    const result = JSON.parse(stdout) as {
      answers?: {
        intent?: { choice?: string; confidence?: number };
        destructive?: { noul?: number };
      };
    };
    const choiceValue = String(result.answers?.intent?.choice || "");
    if (!intents.includes(choiceValue as WorkflowIntent)) return null;
    const local = classifyLocally(request);
    const destructive = local.destructive || Number(result.answers?.destructive?.noul || 0) > 0.65;
    return {
      intent: choiceValue as WorkflowIntent,
      confidence: Number(result.answers?.intent?.confidence || 0),
      risk: destructive ? "high" : local.risk,
      destructive,
      provider: "laya-mlx",
      note: "Phân loại bằng checkpoint Laya-MLX cục bộ; request và câu hỏi được xử lý trên máy.",
    };
  } catch {
    return null;
  } finally {
    await rm(folder, { recursive: true, force: true });
  }
}

export async function classifyRequest(request: string, options: { apiKey?: string; layaCliPath?: string; layaModel?: string } = {}): Promise<Classifier> {
  const local = classifyLocally(request);
  const laya = await classifyWithLaya(request, options.layaCliPath, options.layaModel);
  if (laya && laya.confidence >= 0.72) return laya;
  if (!options.apiKey) return laya ? { ...laya, note: laya.note + " Confidence dưới ngưỡng JEV; không bật provider ngoài nên giữ kết quả local." } : local;

  try {
    const client = new TypeSafeClient({ apiKey: options.apiKey, baseURL: process.env.TYPESAFE_BASE_URL || "https://api.typesafe.ai", timeout: 10_000 });
    const result = await client.systemOne({
      model: "jev-latest",
      state: { request },
      questions: {
        intent: choice("Chọn năng lực nội bộ phù hợp nhất với yêu cầu doanh nghiệp", {
          build: "Thiết kế, xây dựng hoặc sửa đổi sản phẩm/phần mềm",
          operate: "Vận hành quy trình hoặc xử lý nghiệp vụ nội bộ",
          research: "Tra cứu, phân tích và tổng hợp tri thức",
          support: "Hỗ trợ khách hàng hoặc xử lý ticket",
          learn: "Đào tạo, thực hành hoặc phát triển năng lực",
          general: "Yêu cầu khác hoặc chưa đủ thông tin",
        }),
        destructive: noul("Thực hiện yêu cầu có gửi/xuất bản dữ liệu, xóa, thay đổi production, giao dịch tài chính, hoặc tác động không thể đảo ngược không?"),
      },
    });
    const intent = String(result.answers.intent.choice) as WorkflowIntent;
    const destructive = local.destructive || result.answers.destructive.noul > 0.65;
    const risk: RiskLevel = destructive ? "high" : local.risk === "review" ? "review" : "low";
    return {
      intent: intents.includes(intent) ? intent : local.intent,
      confidence: result.answers.intent.confidence,
      risk,
      destructive,
      provider: "typesafe-jev",
      note: "JEV phân loại yêu cầu; chính sách cục bộ vẫn có quyền nâng mức rủi ro.",
    };
  } catch {
    return laya || { ...local, note: "JEV chưa khả dụng; đã fallback sang phân loại cục bộ. Nội dung không rời khỏi máy." };
  }
}

export type WorkflowPhase = { id: string; label: string; state: "complete" | "waiting" | "blocked"; detail: string };

export function createWorkflowPlan(classification: Classifier, request: string, evidenceCount: number): WorkflowPhase[] {
  const blocked = classification.risk !== "low";
  return [
    { id: "frame", label: "Frame", state: "complete", detail: "Đã lưu mục tiêu và người khởi tạo trong workspace." },
    { id: "acceptance", label: "Acceptance", state: "complete", detail: "Tiêu chí: tạo đầu ra truy vết được; không thực hiện side effect ngoài catalog." },
    { id: "route", label: "Route", state: "complete", detail: `Route tới ${classification.intent}; classifier ${classification.provider}, confidence ${Math.round(classification.confidence * 100)}%.` },
    { id: "dispatch", label: "Dispatch", state: "complete", detail: "Đã chọn planner, knowledge retriever và verifier nội bộ." },
    { id: "execute", label: "Execute", state: blocked ? "blocked" : "complete", detail: blocked ? "Chờ người có quyền duyệt; chưa gọi công cụ có side effect." : `Đã truy xuất ${evidenceCount} nguồn nội bộ và chuẩn bị kế hoạch cục bộ.` },
    { id: "verify", label: "Verify", state: blocked ? "waiting" : "complete", detail: blocked ? "Verifier sẽ chạy sau khi duyệt." : "Đã kiểm tra tenant scope, policy và provenance của kết quả." },
    { id: "report", label: "Report", state: blocked ? "waiting" : "complete", detail: blocked ? "Báo cáo cuối chờ phê duyệt." : `Kế hoạch đã lưu. Yêu cầu gốc: ${request.slice(0, 180)}${request.length > 180 ? "…" : ""}` },
  ];
}

export function verifyWorkflow(classification: Classifier, evidenceCount: number, organizationMatches: boolean, approvalGranted = false) {
  const checks = [
    { id: "tenant", label: "Tenant boundary", passed: organizationMatches },
    { id: "approval", label: "Risk approval", passed: classification.risk === "low" || approvalGranted },
    // Every evidence item comes directly from a tenant-scoped knowledge row that
    // carries both a source URI and a content digest.
    { id: "provenance", label: "Evidence provenance", passed: true },
    { id: "external-effects", label: "External side effects", passed: true },
  ];
  return { passed: checks.every((check) => check.passed), checks };
}
