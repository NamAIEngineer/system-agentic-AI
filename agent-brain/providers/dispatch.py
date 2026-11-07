"""Host-neutral task parsing and module/provider dispatch shared by CLI and composition."""
from __future__ import annotations
import os
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from providers.runtime import ProviderTask, ProviderUnavailable, select_provider
from modules.workspace_resolver import ModuleExecutionContext, ModuleRegistry, WorkspaceResolver
from modules.runtime import (prepare_module_task, execute_pedagogy_module,
    run_knowledge_builder_workflow, normalize_module_result, optimize_module_result)
from modules.runtime import _stage_knowledge_sources
from tool_runtime import ToolRuntime
import json
import hashlib


def dispatch_task(task: ProviderTask, *, module_id: str | None = None,
                  module_capability: str | None = None, wait_seconds: float = 600) -> dict:
    """Execute one step with the same safety and routing gates as the host CLI."""
    base = {"task_id": task.task_id, "module": module_id, "execution_policy": task.execution_policy}
    context = task.execution_context
    if (os.environ.get("SYSTEM_AGENTIC_EXECUTION_ROLE") == "worker"
            or context.get("host_role", "orchestrator") != "orchestrator"
            or type(context.get("dispatch_depth", 1)) is not int
            or context.get("dispatch_depth", 1) != 1
            or context.get("direct_or_dispatch", "DISPATCH") != "DISPATCH"):
        return {**base, "status": "REFUSED", "dispatched": False, "reason": "RECURSION_GUARD"}
    if task.execution_policy not in {"READ_ONLY", "WORKSPACE_WRITE"}:
        return {**base, "status": "REFUSED", "dispatched": False, "reason": "ELEVATED_REQUIRES_APPROVAL"}
    try:
        if module_id and module_id.startswith("generated-") and module_capability:
            from modules.runtime import get_module
            generated = get_module(module_id)
            if generated is None:
                raise ProviderUnavailable("generated module is not registered")
            package = Path(generated["workspace_path"]).resolve(strict=True)
            if generated.get("generated_origin") != "promoted" or not generated.get("promotion_id"):
                raise ProviderUnavailable("promoted capability lacks trusted provenance")
            expected_hash = generated.get("package_hash")
            candidate_path = package / "candidate.py"
            fixture_path = package / "fixture.json"
            if not candidate_path.is_file() or not fixture_path.is_file():
                raise ProviderUnavailable("promoted capability package is incomplete")
            actual_hash = hashlib.sha256(candidate_path.read_bytes() + fixture_path.read_bytes()).hexdigest()
            if actual_hash != expected_hash:
                raise ProviderUnavailable("promoted capability package integrity mismatch")
            expected_entrypoint_hash = generated.get("entrypoint_hash")
            if expected_entrypoint_hash and hashlib.sha256(candidate_path.read_bytes()).hexdigest() != expected_entrypoint_hash:
                raise ProviderUnavailable("promoted capability entrypoint integrity mismatch")
            if generated.get("entrypoint") not in {None, "candidate.py"}:
                raise ProviderUnavailable("promoted capability entrypoint is unsupported")
            runtime = ToolRuntime(package, "WORKSPACE_WRITE", worker_backend=__import__("tool_queue").WorkerExecutionBackend())
            fixture = task.context.get("fixture", "fixture.json")
            result = runtime.call("run_tests", {"argv": ["python3", "candidate.py", fixture], "timeout_seconds": 30}, trace_context={"caller": "promoted-capability-runtime"})
            output = json.loads(result.get("output", "").strip()) if result.get("output") else {}
            return {**base, "status": result["status"], "execution_status": result["execution_status"],
                    "provider": None, "module": module_id, "domain_outcome": "SUMMARY_VERIFIED" if result.get("test_outcome") == "PASSED" else "FAILED",
                    "result": output, "artifacts": [], "verification": {"test_outcome": result.get("test_outcome"), "runtime": result}}
        if module_id is not None:
            if not module_capability:
                raise ValueError("A module capability is required for module dispatch.")
            if module_id == "knowledge-builder" and module_capability == "knowledge.pack-compile":
                raw_output = task.context.get("knowledge_output") or f"artifacts/knowledge-packs/{task.task_id}"
                output_root = (task.workspace.expanduser().resolve(strict=True) / raw_output).resolve(strict=False)
                output_root.relative_to(task.workspace.expanduser().resolve(strict=True))
                task, staging = _stage_knowledge_sources(task, output_root)
            task = prepare_module_task(task, module_id, module_capability)
            if module_id == "knowledge-builder" and module_capability == "knowledge.pack-compile":
                execution_context = dict(task.execution_context)
                module_context = dict(execution_context.get("module", {}))
                workflow_context = dict(module_context.get("knowledge_workflow", {}))
                workflow_context["source_staging"] = staging
                module_context["knowledge_workflow"] = workflow_context
                execution_context["module"] = module_context
                task = replace(task, execution_context=execution_context)
        if module_id == "pedagogy":
            result = execute_pedagogy_module(task, module_capability).to_dict()
            trace = dict(task.execution_context)
            metadata = dict(trace.get("module", {}))
            workflow = dict(metadata.get("pedagogy_workflow", {}))
            runtime = result.get("runtime", {})
            if runtime.get("endpoint"):
                workflow.update(service_url=runtime["endpoint"], runtime=runtime)
                metadata["pedagogy_workflow"] = workflow
                trace["module"] = metadata
            return {**result, "status": result["execution_status"], "provider": None,
                    "execution_policy": task.execution_policy, "execution_context": trace,
                    "selection_reason": "deterministic local domain module; provider execution not required",
                    "dispatched": True}
        if module_id == "agent-desktop" and module_capability in {"desktop.automation", "desktop.snapshot", "desktop.action"}:
            from modules.agent_desktop_adapter import AgentDesktopError, execute_agent_desktop_module
            try:
                raw = execute_agent_desktop_module(task)
            except AgentDesktopError as exc:
                raw = {"execution_status": "BLOCKED" if exc.code in {"AUTH_REQUIRED", "POLICY_BLOCKED", "STALE_REF", "AMBIGUOUS_TARGET"} else "FAILED",
                       "domain_outcome": "UNVERIFIED", "summary": str(exc), "errors": [{"code": exc.code, "message": str(exc)}],
                       "evidence": [{"type": "adapter", "code": exc.code}], "artifacts": []}
            return {**base, "status": raw["execution_status"], "execution_status": raw["execution_status"],
                    "provider": None, "account": None, "role": task.role, "required_capabilities": list(task.required_capabilities),
                    "selection_reason": "deterministic local agent-desktop MCP adapter; provider queue not required",
                    "execution_context": task.execution_context, "result": raw, "evidence": raw.get("evidence", []),
                    "errors": raw.get("errors", []), "artifacts": raw.get("artifacts", []), "dispatched": True}
        if module_id == "browser-agent" and module_capability == "browser.automation":
            from modules.browser_adapter import BrowserAdapterError, execute_browser_module
            try:
                raw = execute_browser_module(task)
            except BrowserAdapterError as exc:
                raw = {"execution_status": "FAILED", "domain_outcome": "UNVERIFIED",
                       "summary": str(exc), "errors": [{"code": "BROWSER_EXECUTION_FAILED", "message": str(exc)}], "artifacts": []}
            return {**base, "status": raw["execution_status"], "execution_status": raw["execution_status"],
                    "provider": None, "role": task.role, "required_capabilities": list(task.required_capabilities),
                    "selection_reason": "deterministic local Playwright browser adapter",
                    "execution_context": task.execution_context, "result": raw,
                    "evidence": raw.get("evidence", []), "errors": raw.get("errors", []),
                    "artifacts": raw.get("artifacts", []), "dispatched": True}
        if module_id == "coding-agent" and module_capability == "software.coding":
            from modules.coding_adapter import CodingAdapterError, execute_coding_module
            try:
                raw = execute_coding_module(task)
            except CodingAdapterError as exc:
                raw = {"execution_status": "FAILED", "domain_outcome": "UNVERIFIED",
                       "summary": str(exc), "errors": [{"code": "CODING_EXECUTION_FAILED", "message": str(exc)}], "artifacts": []}
            return {**base, "status": raw["execution_status"], "execution_status": raw["execution_status"],
                    "provider": None, "role": task.role, "required_capabilities": list(task.required_capabilities),
                    "selection_reason": "deterministic workspace-bounded coding adapter",
                    "execution_context": task.execution_context, "result": raw,
                    "evidence": raw.get("evidence", []), "errors": raw.get("errors", []),
                    "artifacts": raw.get("artifacts", []), "dispatched": True}
        adapter, selection = select_provider(task)
    except ProviderUnavailable as exc:
        return {**base, "status": "UNAVAILABLE" if module_id == "pedagogy" else "REFUSED",
                "execution_status": "UNAVAILABLE", "provider": None, "reason": str(exc),
                "execution_context": task.execution_context,
                "provider_requirements": list(task.required_capabilities),
                "provider_policy": task.provider_policy,
                "dispatched": False, "artifacts": [], "errors": [{"code": "MODULE_UNAVAILABLE", "message": str(exc)}]}
    try:
        result = adapter.execute(task, wait_timeout_seconds=wait_seconds)
        if module_id is not None:
            result = run_knowledge_builder_workflow(result, include_index=True)
            result = normalize_module_result(result)
            result = optimize_module_result(result)
    except (OSError, RuntimeError, ValueError) as exc:
        return {**base, "status": "FAILED", "execution_status": "FAILED",
                "execution_context": task.execution_context, "provider": selection.provider_id,
                "account": selection.account_id, "role": task.role, "queue_job_id": task.task_id,
                "required_capabilities": list(task.required_capabilities), "selection_reason": selection.reason,
                "artifacts": [], "evidence": [], "summary": None,
                "errors": [{"code": "PROVIDER_EXECUTION_ERROR", "message": str(exc)}]}
    payload = asdict(result)
    payload["provider"] = payload.pop("provider_id")
    payload["account"] = payload.pop("account_id")
    payload["candidates"] = list(selection.candidates)
    payload["execution_status"] = result.status
    payload["module"] = module_id
    return payload


def reconcile_task(task: ProviderTask, *, provider_id: str, module_id: str | None = None,
                   module_capability: str | None = None) -> dict:
    """Read one existing provider job; never submits or creates a new job."""
    adapter = __import__("providers.runtime", fromlist=["adapter_for"]).adapter_for(provider_id)
    result = adapter.status(task, selection_reason="reconciled existing provider job")
    if module_id is not None:
        result = run_knowledge_builder_workflow(result, include_index=True)
        result = normalize_module_result(result)
        result = optimize_module_result(result)
    payload = asdict(result)
    payload["provider"] = payload.pop("provider_id")
    payload["account"] = payload.pop("account_id")
    payload["execution_status"] = result.status
    payload["module"] = module_id
    return payload

def task_from_spec(spec: object) -> ProviderTask:
    if not isinstance(spec, dict):
        raise ValueError("Task spec must be a JSON object.")
    def string(name: str, *, required: bool = False, default: str = "") -> str:
        value = spec.get(name, default)
        if not isinstance(value, str) or (required and not value.strip()):
            raise ValueError(f"Task field {name} must be a non-empty string.")
        return value
    def string_list(name: str, default: list[str]) -> tuple[str, ...]:
        value = spec.get(name, default)
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError(f"Task field {name} must be an array of non-empty strings.")
        return tuple(value)
    context = spec.get("context", {})
    policy = spec.get("provider_policy", {})
    execution_context = spec.get("execution_context", {
        "origin": "manual-cli",
        "host_role": "orchestrator",
        "dispatch_depth": 1,
        "direct_or_dispatch": "DISPATCH",
        "framing_result": "Explicit provider dispatch request",
    })
    execution_policy = string("execution_policy", default="READ_ONLY")
    if not isinstance(context, dict) or not isinstance(policy, dict) or not isinstance(execution_context, dict):
        raise ValueError("Task context, execution_context, and provider_policy must be JSON objects.")
    if execution_policy not in {"READ_ONLY", "WORKSPACE_WRITE", "ELEVATED"}:
        raise ValueError("execution_policy must be READ_ONLY, WORKSPACE_WRITE, or ELEVATED.")
    if execution_context.get("host_role", "orchestrator") != "orchestrator":
        raise ValueError("Only a host orchestrator may submit a worker task.")
    if type(execution_context.get("dispatch_depth", 1)) is not int or execution_context.get("dispatch_depth", 1) != 1:
        raise ValueError("Only host-to-worker dispatch at depth 1 is supported.")
    if execution_context.get("direct_or_dispatch", "DISPATCH") != "DISPATCH":
        raise ValueError("A DIRECT task must remain on the host and must not be dispatched.")
    current_workspace = Path(string("workspace", required=True)).expanduser()
    context = dict(context)
    workspace = current_workspace
    target_module_id = spec.get("targetModuleId")
    requested_capability = spec.get("requestedCapability")
    supplied_resolved_workspace = spec.get("resolvedWorkspace")
    if target_module_id is not None:
        if not isinstance(target_module_id, str) or not target_module_id.strip():
            raise ValueError("targetModuleId must be a non-empty registered module ID.")
        if not isinstance(requested_capability, str) or not requested_capability.strip():
            raise ValueError("requestedCapability is required when targetModuleId is set.")
        registry = ModuleRegistry()
        resolver = WorkspaceResolver(registry, current_workspace)
        module_context = ModuleExecutionContext.resolve(
            target_module_id, requested_capability, resolver=resolver,
            pipeline_id=context.get("pipelineId", spec.get("pipelineId")),
            step_id=context.get("stepId", spec.get("stepId")),
            product_id=context.get("productId", spec.get("productId")))
        resolved = Path(module_context.resolved_workspace)
        if supplied_resolved_workspace is not None:
            if not isinstance(supplied_resolved_workspace, str) or Path(supplied_resolved_workspace).expanduser().resolve(strict=False) != resolved:
                raise ValueError("resolvedWorkspace does not match the canonical registered module workspace.")
        for key, value in (("targetModuleId", target_module_id), ("resolvedWorkspace", str(resolved)), ("requestedCapability", requested_capability)):
            if key in context and context[key] != value:
                raise ValueError(f"Task context {key} does not match canonical module resolution.")
            context[key] = value
        context["moduleExecutionContext"] = module_context.to_dict()
        execution_context = dict(execution_context)
        if "moduleExecutionContext" in execution_context and execution_context["moduleExecutionContext"] != module_context.to_dict():
            raise ValueError("execution_context moduleExecutionContext does not match canonical module resolution.")
        execution_context["moduleExecutionContext"] = module_context.to_dict()
        # Desktop is a host capability. Keep the caller's project workspace as
        # the task/artifact boundary while retaining the canonical registry
        # resolution in moduleExecutionContext.
        if target_module_id not in {"agent-desktop", "browser-agent", "coding-agent"}:
            workspace = resolved
    return ProviderTask(
        task_id=string("task_id", default=str(uuid.uuid4())),
        objective=string("objective", required=True),
        role=string("role", default="worker"),
        required_capabilities=string_list("required_capabilities", ["task.submit"]),
        workspace=workspace,
        context=context,
        constraints=string_list("constraints", []),
        acceptance_criteria=string_list("acceptance_criteria", []),
        provider_policy=policy,
        title=string("title", default="") or None,
        execution_policy=execution_policy,
        execution_context=execution_context,
    )
