"""Bounded sequential composition; callers supply goal interpretation, registry owns compatibility.

No shell, provider-specific planning, scheduler or domain lifecycle logic lives here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from modules.pipeline_contracts import PipelinePlan, ProductArtifact
from modules.runtime import module_inventory
from providers.dispatch import dispatch_task, reconcile_task, task_from_spec
from tool_runtime import ToolRuntime, MAX_FILE_BYTES

_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}\Z")


def field(value: dict, pointer: str) -> Any:
    for part in pointer.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def contract(module: dict, capability: str, context: dict) -> dict:
    value = module.get("composition_contracts", {}).get(capability, {})
    if any(field(context, key) != expected for key, expected in value.get("context_match", {}).items()):
        return {}
    return value


def validate_plan(plan: PipelinePlan, inventory: list[dict] | None = None) -> dict[str, dict]:
    if os.environ.get("SYSTEM_AGENTIC_EXECUTION_ROLE") == "worker":
        raise ValueError("RECURSION_GUARD: workers cannot execute compositions")
    if not isinstance(plan.goal, str) or not plan.goal.strip() or not _ID.fullmatch(plan.pipeline_id):
        raise ValueError("Plan requires goal and a safe workflow ID")
    if len(plan.steps) not in {1, 2, 3}:
        raise ValueError("Only one through three sequential steps are supported")
    if plan.concurrency.max_global_concurrency != 1 or plan.failure_policy != "FAIL_FAST":
        raise ValueError("Composition foundation only supports sequential FAIL_FAST plans")
    if plan.execution_policy not in {"READ_ONLY", "WORKSPACE_WRITE"}:
        raise ValueError("ELEVATED_REQUIRES_APPROVAL")
    root = Path(plan.workspace).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("Workflow workspace must be an existing absolute directory")
    modules = {item["id"]: item for item in (module_inventory() if inventory is None else inventory)}
    seen: dict[str, Any] = {}
    outputs: dict[str, dict] = {}
    for index, step in enumerate(plan.steps):
        if not _ID.fullmatch(step.step_id) or step.step_id in seen:
            raise ValueError("Step ID must be unique and path-safe")
        module = modules.get(step.module_id)
        if not module or not module.get("enabled") or not module.get("execution_ready") or step.capability not in module.get("capabilities", []):
            raise ValueError(f"Unavailable registered capability: {step.module_id}/{step.capability}")
        if set(step.task) - {"context", "execution_policy", "objective", "constraints"}:
            raise ValueError("Step cannot override workspace, IDs, provider routing or execution context")
        policy = step.task.get("execution_policy", "READ_ONLY")
        if policy not in {"READ_ONLY", "WORKSPACE_WRITE"} or (policy == "WORKSPACE_WRITE" and plan.execution_policy != "WORKSPACE_WRITE"):
            raise ValueError("Step policy cannot exceed workflow authorization")
        if not isinstance(step.task.get("context", {}), dict):
            raise ValueError("Step context must be an object")
        if set(step.expected) - {"execution_status", "domain_outcome", "validation_status", "review_outcome"}:
            raise ValueError("Unsupported outcome prerequisite")
        if step.expected.get("execution_status", "COMPLETED") != "COMPLETED":
            raise ValueError("Required execution health must be COMPLETED")
        if index == 0 and (step.depends_on or step.input_bindings):
            raise ValueError("First step cannot depend on future inputs")
        if index > 0 and (step.depends_on != (plan.steps[index - 1].step_id,) or not step.input_bindings or step.module_id == plan.steps[index - 1].module_id):
            raise ValueError("Every subsequent module must consume the previous step's output sequentially")
        io = contract(module, step.capability, step.task.get("context", {}))
        declared = io.get("outputs", {})
        if any(ref not in declared for ref in step.output_refs):
            raise ValueError("Output reference is not declared by the capability contract")
        if len(plan.steps) == 2 and not step.output_refs:
            raise ValueError("Composition steps must declare required outputs")
        bound_targets = set()
        for binding in step.input_bindings:
            if set(binding) != {"from", "to"} or not isinstance(binding["from"], str) or not isinstance(binding["to"], str):
                raise ValueError("Input binding requires only from/to references")
            source = outputs.get(binding["from"])
            target = binding["to"]
            if source is None or source["type"] not in io.get("inputs", {}).get(target, []):
                raise ValueError("Incompatible artifact handoff")
            if target in bound_targets or target in step.task.get("context", {}):
                raise ValueError("Input binding must not overwrite an input")
            bound_targets.add(target)
        if tuple(b["from"] for b in step.input_bindings) != step.input_refs:
            raise ValueError("inputRefs must exactly identify the bound inputs")
        for name in step.output_refs:
            outputs[f"{step.step_id}:{name}"] = declared[name]
        seen[step.step_id] = step
    return modules


def plan_goal(goal: str, requests: list[dict], *, workspace: str, execution_policy: str = "READ_ONLY",
              workflow_id: str | None = None, inventory: list[dict] | None = None) -> PipelinePlan:
    """Host/planner supplies requested capabilities, never a hard-coded keyword chain."""
    modules = module_inventory() if inventory is None else inventory
    steps = []
    for index, request in enumerate(requests):
        capability = request["capability"]
        candidates = [m for m in modules if capability in m.get("capabilities", []) and m.get("enabled") and m.get("execution_ready")]
        if len(candidates) != 1:
            raise ValueError(f"Capability {capability!r} needs one ready registry candidate; found {len(candidates)}")
        module = candidates[0]
        task = request.get("task", {})
        io = contract(module, capability, task.get("context", {}))
        bindings = request.get("inputBindings", [])
        steps.append({"stepId": request.get("id", f"step-{index + 1}"), "moduleId": module["id"],
                      "capability": capability, "dependsOn": request.get("dependsOn", []),
                      "inputRefs": [b["from"] for b in bindings], "inputBindings": bindings,
                      "outputRefs": list(io.get("outputs", {})), "task": task,
                      "executionMode": "READ_ONLY" if task.get("execution_policy", "READ_ONLY") == "READ_ONLY" else "MUTATING",
                      "expected": {"execution_status": "COMPLETED", **request.get("expected", {})}})
    plan = PipelinePlan.from_dict({"pipelineId": workflow_id or str(uuid.uuid4()), "goal": goal,
                                  "workspace": workspace, "execution_policy": execution_policy, "steps": steps})
    validate_plan(plan, modules)
    return plan


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest(root: Path, value: str) -> dict[str, str]:
    path = Path(value)
    path = path if path.is_absolute() else root / path
    path.resolve(strict=True).relative_to(root)
    if any((root / Path(*path.relative_to(root).parts[:i])).is_symlink() for i in range(1, len(path.relative_to(root).parts) + 1)):
        raise ValueError("Artifact symlink is not allowed")
    files = [path] if path.is_file() else sorted(path.rglob("*"))
    if len(files) > 1000:
        raise ValueError("Artifact manifest exceeds bounded file count")
    result = {}
    total = 0
    for item in files:
        if item.is_symlink():
            raise ValueError("Artifact contains a symlink")
        if item.is_file():
            total += item.stat().st_size
            if total > 8_000_000:
                raise ValueError("Artifact exceeds bounded verification size")
            result[str(item.relative_to(root))] = _digest(item.read_bytes())
    if not result:
        raise ValueError("Required artifact is empty")
    return result


def _outcomes(result: dict, io: dict) -> dict:
    return {"execution_status": result.get("execution_status", result.get("status")),
            "domain_outcome": result.get("domain_outcome"),
            **{name: field(result, pointer) for name, pointer in io.get("outcome_fields", {}).items()}}


def _collect(step, result: dict, io: dict, tools: ToolRuntime, relative: str, pipeline_id: str) -> list[dict]:
    collected = []
    for name in step.output_refs:
        descriptor = io["outputs"][name]
        metadata = {"producer_task_id": result["task_id"], "capability": step.capability}
        if descriptor.get("artifact_type"):
            matches = [a for a in result.get("artifacts", []) if a.get("type") == descriptor["artifact_type"] and a.get("format") == "md"]
            if len(matches) != 1 or not isinstance(matches[0].get("content"), str):
                raise ValueError("Required Markdown export missing from official module result")
            raw = matches[0]["content"].encode("utf-8")
            stored = tools.materialize_artifact(f"{relative}/{step.step_id}-{name}.md", raw)
            path = stored["path"]
            metadata.update({**stored, "session_id": matches[0].get("session_id"), "service": result.get("service"),
                             "transformation": "UTF-8 serialization of official Markdown export; content unchanged"})
        else:
            path = field(result, descriptor["path_field"])
            if not isinstance(path, str):
                raise ValueError("Required artifact path absent from module result")
        manifest = _manifest(tools.workspace, path)
        metadata["manifest"] = manifest
        artifact = ProductArtifact(f"{step.step_id}:{name}", step.module_id, descriptor["type"], (path,),
                                   metadata=metadata, created_at=datetime.now(timezone.utc).isoformat(),
                                   producer_step_id=step.step_id, pipeline_id=pipeline_id)
        collected.append(artifact.to_dict())
    return collected


def _verify_artifacts(root: Path, artifacts: list[dict]) -> bool:
    return all(_manifest(root, a["files"][0]) == a["metadata"]["manifest"] for a in artifacts)


def _verify_consumption(root: Path, io: dict, result: dict, inputs: list[dict], artifacts: list[dict]) -> bool:
    if not inputs:
        return True
    sources = field(result, io.get("source_evidence_field", ""))
    if not isinstance(sources, list):
        return False
    for incoming in inputs:
        digest = incoming["metadata"].get("sha256")
        matched = False
        for source in sources:
            if not digest or source.get("sha256") != digest or not source.get("provenance") or not source.get("trust_tier"):
                continue
            for output in artifacts:
                pack = Path(output["files"][0])
                uri = source.get("uri")
                if not isinstance(uri, str) or Path(uri).is_absolute():
                    continue
                # Result URIs can be workspace-relative while UKMC attribution is
                # pack-relative. Both must resolve to actual evidence inside this pack.
                for candidate in (root / uri, pack / uri):
                    try:
                        candidate.resolve(strict=True).relative_to(pack.resolve(strict=True))
                        if _manifest(root, str(candidate)).get(str(candidate.relative_to(root))) == digest:
                            matched = True
                    except (OSError, ValueError):
                        pass
        if not matched:
            return False
    return True


def run_plan(plan: PipelinePlan, *, provider_policy: dict | None = None,
             on_step_started: Callable[[Any, Any], None] | None = None) -> dict:
    modules = validate_plan(plan)
    if len(plan.steps) == 1:
        return {"composition_required": False, "workflow_status": "NOT_REQUIRED", "plan": plan.to_dict(),
                "next_action": "Use existing single-module dispatch"}
    if plan.execution_policy != "WORKSPACE_WRITE":
        raise ValueError("Composition artifact/trace writes require explicit WORKSPACE_WRITE")
    tools = ToolRuntime(plan.workspace, plan.execution_policy)
    root = tools.workspace
    relative = f".system-agentic-workflows/{plan.pipeline_id}"
    trace_path = root / relative / "trace.json"
    serialized = json.dumps(plan.to_dict(), sort_keys=True, ensure_ascii=False)
    signature = _digest(serialized.encode())
    saved_trace: dict[str, Any] | None = None
    if trace_path.exists():
        # A RUNNING trace is replayed with the same stable step/task IDs. The
        # queue's durable idempotency key makes this a reconciliation replay,
        # never a second provider execution.
        _manifest(root, str(trace_path))
        saved = json.loads(trace_path.read_text())
        saved_trace = saved
        if saved.get("plan_sha256") != signature:
            raise ValueError("Workflow ID already belongs to another plan")
        if saved.get("workflow_status") == "COMPLETED" and _verify_artifacts(root, saved["artifacts"]):
            return {**saved, "reused": True}
        if saved.get("workflow_status") not in {"RUNNING", "BLOCKED"}:
            raise ValueError("Workflow already attempted; inspect/reconcile existing task IDs, durable resume is not supported")
    trace = saved_trace or {"workflow_id": plan.pipeline_id, "goal": plan.goal, "plan": plan.to_dict(), "plan_sha256": signature,
             "workflow_status": "RUNNING", "steps": [], "handoff": [], "artifacts": [], "verification": {},
             "reused": False, "durable_resume": "BOUNDED_REPLAY", "trace_path": str(trace_path)}
    if saved_trace is None:
        tools.materialize_artifact(f"{relative}/trace.json", json.dumps(trace).encode())

    def persist():
        # The first exclusive write claims this workflow. No other caller may resume a running trace.
        temporary = trace_path.with_name("trace.tmp")
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(trace, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, trace_path)

    completed = {step.get("step_id") for step in trace.get("steps", []) if step.get("verification_passed")}
    by_ref = {artifact.get("artifactId"): artifact for artifact in trace.get("artifacts", []) if artifact.get("artifactId")}
    for step in plan.steps:
        if step.step_id in completed:
            continue
        entry = next((item for item in trace.get("steps", []) if item.get("task_id") == f"{plan.pipeline_id}-{step.step_id}"), None)
        replay_existing = bool(entry and entry.get("execution_status") == "RUNNING")
        if entry is None:
            entry = {"step_id": step.step_id, "module": step.module_id, "capability": step.capability,
                 "dependencies": list(step.depends_on), "task_id": f"{plan.pipeline_id}-{step.step_id}",
                 "execution_status": "NOT_RUN", "verification_passed": False}
            trace["steps"].append(entry)
        else:
            entry.update({"module": step.module_id, "capability": step.capability,
                          "dependencies": list(step.depends_on), "execution_status": "NOT_RUN",
                          "verification_passed": False})
        if not set(step.depends_on).issubset(completed):
            entry.update(execution_status="BLOCKED", reason="DEPENDENCY_PREREQUISITE_NOT_MET")
            persist()
            continue
        context = dict(step.task.get("context", {}))
        inputs = []
        try:
            # Revalidate source hashes immediately before handing references to the consumer.
            for binding in step.input_bindings:
                artifact = by_ref[binding["from"]]
                if not _verify_artifacts(root, [artifact]):
                    raise ValueError("Handoff source changed after producer verification")
                context[binding["to"]] = [str(Path(p).relative_to(root)) for p in artifact["files"]]
                inputs.append(artifact)
                trace["handoff"].append({"producer": binding["from"].split(":")[0], "consumer": step.step_id,
                                         "binding": binding, "artifact": artifact})
            context["composition_inputs"] = inputs
            spec = {"task_id": entry["task_id"], "workspace": str(root), "objective": step.task.get("objective", plan.goal),
                    "context": context, "constraints": step.task.get("constraints", []),
                    "execution_policy": step.task.get("execution_policy", "READ_ONLY"),
                    "provider_policy": provider_policy or {},
                    "execution_context": {"origin": "composition-runtime", "host_role": "orchestrator", "dispatch_depth": 1,
                                          "direct_or_dispatch": "DISPATCH", "workflow_id": plan.pipeline_id,
                                          "workflow_step": step.step_id, "parent_task_id": plan.pipeline_id,
                                          "logical_task_id": entry["task_id"],
                                          "execution_attempt_id": f"{entry['task_id']}-attempt-1",
                                          "idempotency_key": f"{plan.pipeline_id}:{entry['task_id']}:1"}}
            task = task_from_spec(spec)
            if on_step_started is not None:
                on_step_started(step, task)
            entry["execution_status"] = "RUNNING"
            persist()
            if replay_existing and step.module_id == "knowledge-builder":
                # A service crash can leave the worker result and output pack
                # durable while the trace still says RUNNING. Reconcile the
                # same provider job; do not stage/submit or overwrite output.
                result = reconcile_task(task, provider_id="local-qwen",
                                        module_id=step.module_id, module_capability=step.capability)
            else:
                result = dispatch_task(task, module_id=step.module_id, module_capability=step.capability)
            io = contract(modules[step.module_id], step.capability, context)
            outcomes = _outcomes(result, io)
            entry.update(outcomes)
            entry.update(result=result, provider=result.get("provider"), queue_job_id=result.get("queue_job_id"))
            if outcomes["execution_status"] == "COMPLETED":
                artifacts = _collect(step, result, io, tools, relative, plan.pipeline_id)
                trace["artifacts"].extend(artifacts)
                expected_ok = all(outcomes.get(key) == expected for key, expected in step.expected.items())
                consumption_ok = _verify_consumption(root, io, result, inputs, artifacts)
                entry["verification"] = {"expected_outcomes": expected_ok, "artifacts_exist": True, "provenance_preserved": consumption_ok}
                entry["verification_passed"] = expected_ok and consumption_ok
                if entry["verification_passed"]:
                    completed.add(step.step_id)
                    by_ref.update({a["artifactId"]: a for a in artifacts})
        except (ValueError, OSError, RuntimeError, KeyError) as exc:
            # A failed prerequisite does not relabel a successfully executed domain operation.
            if entry["execution_status"] in {"RUNNING", "NOT_RUN"}:
                entry["execution_status"] = "FAILED"
            entry["verification_error"] = f"{type(exc).__name__}: {exc}"
        persist()
    try:
        hashes_ok = _verify_artifacts(root, trace["artifacts"])
    except (ValueError, OSError):
        hashes_ok = False
    trace["verification"] = {"required_steps": len(completed) == len(plan.steps), "artifact_hashes": hashes_ok,
                              "dependencies_satisfied": all(s["verification_passed"] for s in trace["steps"]),
                              "provenance_preserved": all(s.get("verification", {}).get("provenance_preserved", False) for s in trace["steps"])}
    trace["acceptance_passed"] = all(trace["verification"].values())
    trace["workflow_status"] = "COMPLETED" if trace["acceptance_passed"] else "BLOCKED"
    persist()
    return trace
