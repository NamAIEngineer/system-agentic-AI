"""Bounded, provider-neutral System Composer.

The composer designs and validates a small system from semantic requirements. It
does not execute modules or choose providers; compilation delegates to the
existing Composition Runtime contracts.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import re
import uuid
from pathlib import Path
from typing import Any, Callable

from modules.composition import contract, plan_goal, run_plan, validate_plan
from modules.pipeline_contracts import PipelinePlan
from modules.runtime import module_inventory


_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}\Z")


@dataclass(frozen=True)
class BlueprintStep:
    step_id: str
    capability: str
    module_id: str | None
    depends_on: tuple[str, ...] = ()
    inputs: tuple[dict[str, Any], ...] = ()
    outputs: tuple[dict[str, Any], ...] = ()
    verification: tuple[str, ...] = ()
    safety: dict[str, Any] = field(default_factory=dict)
    task: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.step_id, "capability": self.capability, "module": self.module_id,
            "depends_on": list(self.depends_on), "inputs": list(self.inputs),
            "outputs": list(self.outputs), "verification": list(self.verification),
            "safety": self.safety, "task": self.task,
        }


@dataclass(frozen=True)
class SystemBlueprint:
    system_id: str
    goal: str
    problem_summary: str
    required_capabilities: tuple[dict[str, Any], ...]
    steps: tuple[BlueprintStep, ...]
    verification_requirements: tuple[str, ...]
    safety_requirements: tuple[str, ...]
    unresolved_requirements: tuple[dict[str, Any], ...] = ()
    composition_required: bool = True
    blueprint_status: str = "VALID"
    workspace: str = ""
    execution_policy: str = "WORKSPACE_WRITE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_id": self.system_id, "goal": self.goal,
            "problem_summary": self.problem_summary,
            "required_capabilities": list(self.required_capabilities),
            "selected_capabilities": [
                {"capability": step.capability, "module": step.module_id}
                for step in self.steps
            ],
            "workflow_steps": [step.to_dict() for step in self.steps],
            "dependencies": [{"step": step.step_id, "depends_on": list(step.depends_on)} for step in self.steps],
            "input_contracts": [{"step": step.step_id, "inputs": list(step.inputs)} for step in self.steps],
            "output_contracts": [{"step": step.step_id, "outputs": list(step.outputs)} for step in self.steps],
            "artifact_handoff_requirements": [
                {"step": step.step_id, "inputs": list(step.inputs), "transformation": "none",
                 "byte_preserving": True} for step in self.steps if step.inputs
            ],
            "verification_requirements": list(self.verification_requirements),
            "safety_requirements": list(self.safety_requirements),
            "unresolved_requirements": list(self.unresolved_requirements),
            "composition_required": self.composition_required,
            "blueprint_status": self.blueprint_status,
            "workspace": self.workspace,
            "execution_policy": self.execution_policy,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SystemBlueprint":
        if not isinstance(value, dict):
            raise ValueError("System Blueprint must be an object")
        raw_steps = value.get("workflow_steps", [])
        if not isinstance(raw_steps, list):
            raise ValueError("System Blueprint workflow_steps must be an array")
        steps = tuple(BlueprintStep(
            step_id=item["id"], capability=item["capability"], module_id=item.get("module"),
            depends_on=tuple(item.get("depends_on", [])), inputs=tuple(item.get("inputs", [])),
            outputs=tuple(item.get("outputs", [])), verification=tuple(item.get("verification", [])),
            safety=item.get("safety", {}), task=item.get("task", {}),
        ) for item in raw_steps)
        return cls(
            system_id=value["system_id"], goal=value["goal"], problem_summary=value.get("problem_summary", value["goal"]),
            required_capabilities=tuple(value.get("required_capabilities", [])), steps=steps,
            verification_requirements=tuple(value.get("verification_requirements", [])),
            safety_requirements=tuple(value.get("safety_requirements", [])),
            unresolved_requirements=tuple(value.get("unresolved_requirements", [])),
            composition_required=bool(value.get("composition_required", len(steps) > 1)),
            blueprint_status=value.get("blueprint_status", "VALID"), workspace=value.get("workspace", ""),
            execution_policy=value.get("execution_policy", "WORKSPACE_WRITE"),
        )


def _requirements(goal: str) -> list[dict[str, Any]]:
    """Map problem semantics to capability requirements, without module names."""
    text = goal.casefold()
    found: list[dict[str, Any]] = []
    if re.search(r"(native desktop|desktop automation|desktop app|native app|accessibility tree|os window|menu bar|file dialog|ứng dụng native|cửa sổ desktop|giao diện native|thao tác native)", text):
        found.append({"id": "desktop-observation", "capability": "desktop.automation", "reason": "the problem requires a native desktop surface"})
    if re.search(r"(website|web site|browser|chrome|safari|firefox|dom|form trên web|trình duyệt|trang web)", text):
        found.append({"id": "browser-task", "capability": "browser.automation", "reason": "the problem requires a browser or DOM surface"})
    if re.search(r"(read source|run tests|coding task|codebase|source code|đọc source|chạy test|kiểm thử project|sửa lỗi trong project|fix the bug|repair the source|sửa lỗi)", text):
        found.append({"id": "coding-task", "capability": "software.coding", "reason": "the problem requires source editing and test tooling"})
    if re.search(r"(code review|review (?:the )?code|pre[- ]landing|review .*code|rà soát .*code|kiểm tra .*code)", text):
        found.append({"id": "review", "capability": "software.code-review", "reason": "the problem asks for software review"})
    if re.search(r"\b(export|session|rehearsal|teaching|lesson|luyện|phiên)\b", text):
        found.append({"id": "session-export", "capability": "pedagogy.session", "reason": "the problem requires a saved rehearsal/session artifact"})
    if re.search(r"\b(knowledge|knowledge pack|structured knowledge|verified knowledge|tri thức|kiểm chứng nguồn)\b", text):
        found.append({"id": "validated-knowledge", "capability": "knowledge.pack-compile", "reason": "the problem requires a structured, source-validated knowledge artifact"})
    if re.search(r"table|csv|tabular|rows?|columns?|category|categor(?:y|ies)|bảng|dòng dữ liệu", text) and re.search(r"summar|total|count|normalize|aggregate|tổng|thống kê", text):
        found.append({"id": "table-summary", "capability": "data.table.summarize", "reason": "the problem requires deterministic summary statistics for structured table rows"})
    if not found:
        found.append({"id": "unresolved", "capability": "structured.system.result", "reason": "no registered executable capability matches the stated problem"})
    # Preserve semantic order and avoid duplicate capability requirements.
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in found:
        if item["capability"] not in seen:
            unique.append(item); seen.add(item["capability"])
    return unique


def _ready_module(capability: str, inventory: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [m for m in inventory if capability in m.get("capabilities", [])]
    ready = [m for m in candidates if m.get("enabled") and m.get("adapter_status") == "implemented" and m.get("execution_ready")]
    return ready[0] if len(ready) == 1 else None


def analyze_goal(goal: str, *, inventory: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Resolve semantic requirements without designing or executing a system."""
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("System Composer requires a non-empty natural-language goal")
    requirements = _requirements(goal)
    # An unrecognized goal has no executable requirement. Return the explicit
    # capability gap without starting live MCP/provider health probes; planning
    # must stay bounded even when an optional analysis adapter is stalled.
    if inventory is None and len(requirements) == 1 and requirements[0]["capability"] == "structured.system.result":
        return {"required_capabilities": requirements, "resolved": [],
                "unresolved": [{**requirements[0], "status": "UNRESOLVED_CAPABILITY"}],
                "blueprint_status": "UNRESOLVED", "composition_required": False}
    inventory = module_inventory() if inventory is None else inventory
    unresolved: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    for requirement in requirements:
        module = _ready_module(requirement["capability"], inventory)
        if module is None:
            unresolved.append({**requirement, "status": "UNRESOLVED_CAPABILITY"})
        else:
            resolved.append({"requirement": requirement, "module": module})
    status = "UNRESOLVED" if unresolved else ("UNSUPPORTED_SCOPE" if len(resolved) > 3 else "VALID")
    return {"required_capabilities": requirements, "resolved": resolved, "unresolved": unresolved,
            "blueprint_status": status, "composition_required": len(resolved) > 1}


def design_system(goal: str, *, workspace: str, execution_policy: str = "WORKSPACE_WRITE", system_id: str | None = None, context: dict[str, Any] | None = None, inventory: list[dict[str, Any]] | None = None) -> SystemBlueprint:
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("System Composer requires a non-empty natural-language goal")
    root = Path(workspace).expanduser().resolve(strict=True)
    if execution_policy not in {"READ_ONLY", "WORKSPACE_WRITE"}:
        raise ValueError("System Composer refuses ELEVATED or unknown execution policy")
    inventory = module_inventory() if inventory is None else inventory
    context = context or {}
    # Allocate the identity before tasks are built so every derived output path,
    # trace, and persisted blueprint refers to the same system.
    system_id = system_id or f"system-{uuid.uuid4().hex[:12]}"
    analysis = analyze_goal(goal, inventory=inventory)
    requirements = analysis["required_capabilities"]
    unresolved = analysis["unresolved"]
    resolved = [(item["requirement"], item["module"]) for item in analysis["resolved"]]
    status = analysis["blueprint_status"]
    steps: list[BlueprintStep] = []
    requests: list[dict[str, Any]] = []
    previous: BlueprintStep | None = None
    for requirement, module in resolved:
        capability = requirement["capability"]
        inputs: tuple[dict[str, Any], ...] = ()
        bindings: list[dict[str, str]] = []
        depends: list[str] = []
        task: dict[str, Any] = {"context": {"data_classification": context.get("data_classification", "synthetic")}}
        if capability.startswith("desktop.") and isinstance(context.get("desktop"), dict):
            # Preserve only caller-supplied desktop context. The adapter owns
            # executable resolution, session defaults, policy, and lifecycle.
            task["context"]["desktop"] = dict(context["desktop"])
        expected: dict[str, Any] = {}
        if capability == "pedagogy.session":
            task["context"]["pedagogy_request"] = {"action": "export", "format": "md", **dict(context.get("pedagogy_request", {}))}
            task["context"]["pedagogy_request"]["action"] = "export"
            task["context"]["pedagogy_request"]["format"] = "md"
            # Export is a read operation; the enclosing workflow may still need
            # WORKSPACE_WRITE for downstream artifact transport.
            task["execution_policy"] = "READ_ONLY"
            expected["domain_outcome"] = "ARTIFACT_EXPORTED"
        elif capability == "knowledge.pack-compile":
            task["execution_policy"] = "WORKSPACE_WRITE"
            task["context"]["knowledge_output"] = f"artifacts/knowledge-packs/{system_id}"
            expected["validation_status"] = "VALID"
        # Contract context_match is evaluated against the task context.  This is
        # especially important for Pedagogy export, whose declared output exists
        # only for action=export and format=md.
        io = contract(module, capability, task.get("context", {}))
        outputs = tuple({"name": name, **descriptor} for name, descriptor in io.get("outputs", {}).items())
        if previous is not None:
            depends = [previous.step_id]
            if previous.outputs and io.get("inputs"):
                source = previous.outputs[0]
                target = next(iter(io["inputs"]), None)
                if target:
                    bindings = [{"from": f"{previous.step_id}:{source['name']}", "to": target}]
                    inputs = ({"from": bindings[0]["from"], "to": target, "type": source.get("type")},)
        step = BlueprintStep(
            requirement["id"], capability, module["id"], tuple(depends), inputs, outputs,
            ("execution_status=COMPLETED", *(["validation_status=VALID"] if capability == "knowledge.pack-compile" else []), "artifact exists", "provenance preserved"),
            {"execution_policy": task.get("execution_policy", execution_policy), "module_registry_authority": True}, task,
        )
        steps.append(step); previous = step
        requests.append({"id": step.step_id, "capability": capability, "dependsOn": depends, "inputBindings": bindings, "task": task, "expected": expected})
    if len(steps) == 1:
        composition_required = False
    else:
        composition_required = True
    return SystemBlueprint(system_id, goal, goal.strip(), tuple(requirements), tuple(steps), ("all module execution completed", "artifacts exist", "official validation passes where required", "provenance preserved"), ("no ELEVATED execution", "registry readiness and provider policy remain authoritative", "Tool Runtime owns artifact transport"), tuple(unresolved), composition_required, status, str(root), execution_policy)


def validate_blueprint(blueprint: SystemBlueprint, *, inventory: list[dict[str, Any]] | None = None) -> None:
    if blueprint.blueprint_status != "VALID":
        raise ValueError(f"Blueprint is not executable: {blueprint.blueprint_status}")
    if not 1 <= len(blueprint.steps) <= 3:
        raise ValueError("System Composer supports one through three steps")
    inventory = module_inventory() if inventory is None else inventory
    modules = {m["id"]: m for m in inventory}
    seen: set[str] = set()
    output_types: dict[str, str] = {}
    for index, step in enumerate(blueprint.steps):
        module = modules.get(step.module_id)
        if module is None or not module.get("execution_ready"):
            raise ValueError(f"Step {step.step_id} has no execution-ready module")
        if not step.verification:
            raise ValueError(f"Step {step.step_id} has no verification requirements")
        if index == 0 and step.depends_on:
            raise ValueError("First step cannot have dependencies")
        if index > 0 and tuple(step.depends_on) != (blueprint.steps[index - 1].step_id,):
            raise ValueError("System Composer only supports sequential dependencies")
        if any(dep not in seen for dep in step.depends_on):
            raise ValueError(f"Step {step.step_id} skips a dependency")
        for binding in step.inputs:
            source_ref = binding.get("from") if isinstance(binding, dict) else None
            target = binding.get("to") if isinstance(binding, dict) else None
            if not isinstance(source_ref, str) or not isinstance(target, str):
                raise ValueError("Artifact binding requires from/to references")
            source_type = output_types.get(source_ref)
            accepted = contract(module, step.capability, step.task.get("context", {})).get("inputs", {}).get(target, [])
            declared_type = binding.get("type")
            if source_type is None or (declared_type is not None and declared_type != source_type) or source_type not in accepted:
                raise ValueError("Incompatible artifact handoff")
        seen.add(step.step_id)
        for output in step.outputs:
            output_types[f"{step.step_id}:{output['name']}"] = output.get("type", "")
    # Delegate contract compatibility, safety, and cycle checks to the existing plan validator.
    requests = []
    for step in blueprint.steps:
        task = dict(step.task)
        requests.append({"id": step.step_id, "capability": step.capability, "dependsOn": list(step.depends_on), "inputBindings": [{"from": i["from"], "to": i["to"]} for i in step.inputs], "task": task, "expected": {"execution_status": "COMPLETED"}})
    plan_goal(blueprint.goal, requests, workspace=blueprint.workspace, execution_policy=blueprint.execution_policy, workflow_id=blueprint.system_id, inventory=inventory)


def compile_blueprint(blueprint: SystemBlueprint) -> PipelinePlan:
    validate_blueprint(blueprint)
    requests = []
    for step in blueprint.steps:
        requests.append({"id": step.step_id, "capability": step.capability, "dependsOn": list(step.depends_on), "inputBindings": [{"from": i["from"], "to": i["to"]} for i in step.inputs], "task": step.task, "expected": {"execution_status": "COMPLETED", **({"validation_status": "VALID"} if step.capability == "knowledge.pack-compile" else {})}})
    return plan_goal(blueprint.goal, requests, workspace=blueprint.workspace, execution_policy=blueprint.execution_policy, workflow_id=blueprint.system_id)


def run_system(blueprint: SystemBlueprint, *, provider_policy: dict[str, Any] | None = None,
               on_step_started: Callable[[Any, Any], None] | None = None) -> dict[str, Any]:
    root = Path(blueprint.workspace).expanduser().resolve(strict=True)
    persist_root = root / ".system-agentic-systems" / blueprint.system_id
    persist_root.mkdir(parents=True, exist_ok=True)
    (persist_root / "blueprint.json").write_text(json.dumps(blueprint.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    # Unresolved and unsupported designs are inspectable outcomes, never runs.
    # Keep blueprint validity separate from execution status as required by the
    # system contract.
    if blueprint.blueprint_status != "VALID":
        system = {
            "system_id": blueprint.system_id,
            "goal": blueprint.goal,
            "blueprint_status": blueprint.blueprint_status,
            "system_execution_status": "NOT_RUN",
            "acceptance_passed": False,
            "blueprint": blueprint.to_dict(),
            "compiled_plan": None,
            "composition": None,
            "failure_reason": blueprint.blueprint_status,
            "persistence": {
                "durable_resume": "NOT_SUPPORTED",
                "blueprint_path": str(persist_root / "blueprint.json"),
                "compiled_plan_path": None,
            },
        }
        (persist_root / "system-result.json").write_text(json.dumps(system, ensure_ascii=False, indent=2), encoding="utf-8")
        return system
    try:
        validate_blueprint(blueprint)
        plan = compile_blueprint(blueprint)
    except (ValueError, OSError, RuntimeError) as exc:
        invalid_blueprint = replace(blueprint, blueprint_status="INVALID")
        system = {
            "system_id": blueprint.system_id,
            "goal": blueprint.goal,
            "blueprint_status": "INVALID",
            "system_execution_status": "NOT_RUN",
            "acceptance_passed": False,
            "blueprint": invalid_blueprint.to_dict(),
            "compiled_plan": None,
            "composition": None,
            "failure_reason": f"BLUEPRINT_INVALID: {type(exc).__name__}: {exc}",
            "persistence": {
                "durable_resume": "NOT_SUPPORTED",
                "blueprint_path": str(persist_root / "blueprint.json"),
                "compiled_plan_path": None,
            },
        }
        (persist_root / "system-result.json").write_text(json.dumps(system, ensure_ascii=False, indent=2), encoding="utf-8")
        return system
    (persist_root / "compiled-plan.json").write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        result = run_plan(plan, provider_policy=provider_policy, on_step_started=on_step_started)
    except (ValueError, OSError, RuntimeError) as exc:
        result = {
            "workflow_status": "FAILED",
            "acceptance_passed": False,
            "failure_reason": f"COMPOSITION_RUNTIME_FAILED: {type(exc).__name__}: {exc}",
            "plan": plan.to_dict(),
        }
    workflow_status = result.get("workflow_status")
    system = {"system_id": blueprint.system_id, "goal": blueprint.goal,
              "blueprint_status": blueprint.blueprint_status,
              "system_execution_status": "COMPLETED" if workflow_status in {"COMPLETED", "NOT_REQUIRED"} else "FAILED",
              "acceptance_passed": bool(result.get("acceptance_passed", workflow_status in {"COMPLETED", "NOT_REQUIRED"})),
              "blueprint": blueprint.to_dict(), "compiled_plan": plan.to_dict(),
              "composition": result,
              "persistence": {"durable_resume": "NOT_SUPPORTED", "trace_path": result.get("trace_path"), "blueprint_path": str(persist_root / "blueprint.json"), "compiled_plan_path": str(persist_root / "compiled-plan.json")}}
    (persist_root / "system-result.json").write_text(json.dumps(system, ensure_ascii=False, indent=2), encoding="utf-8")
    return system
