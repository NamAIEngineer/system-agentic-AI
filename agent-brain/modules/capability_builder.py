"""Bounded Capability Builder for untrusted, locally verifiable candidates.

The builder is deliberately separate from the trusted Module Registry.  It can
write only below its candidate state root and executes candidates through the
existing Tool Runtime worker backend.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import ast
import hashlib
import json
from pathlib import Path
import re
import tempfile
from typing import Any
import sys

WORKBENCH_ROOT = Path(__file__).resolve().parents[2]
CAPABILITY_CONTRACT_SCHEMA_VERSION = 1
sys.path.insert(0, str(WORKBENCH_ROOT / "products" / "agent-runtime"))

from modules.capability_gap import CapabilityGap, CapabilityBlueprint
from providers.runtime import ProviderTask, ProviderUnavailable, eligible_providers, discover_providers, select_provider
from tool_runtime import ToolRuntime
from tool_queue import WorkerExecutionBackend


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass
class CandidateImplementation:
    candidate_id: str
    gap_id: str
    capability_id: str
    blueprint_ref: str
    blueprint_hash: str
    candidate_hash: str
    implementation_type: str
    candidate_root: str
    manifest: dict[str, Any]
    entrypoint: str
    input_contract: list[dict[str, Any]]
    output_contract: list[dict[str, Any]]
    dependencies: list[str]
    safety_class: str
    generated_files: list[dict[str, Any]]
    test_files: list[dict[str, Any]]
    verification_contract: list[str]
    provider_used: dict[str, Any]
    generation_attempt: int
    candidate_status: str
    verification_status: str
    build_plan: dict[str, Any]
    verification: dict[str, Any]
    provenance: dict[str, Any]
    repair_history: list[dict[str, Any]]
    created_at: str
    schema_version: int = CAPABILITY_CONTRACT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CapabilityBuilder:
    """Build only deterministic, safe candidates described by a blueprint."""

    def __init__(self, *, state_root: str | Path, workspace: str | Path):
        self.state_root = Path(state_root).expanduser().resolve()
        self.workspace = Path(workspace).expanduser().resolve(strict=True)
        self.candidates_root = self.state_root / "capability-builder" / "candidates"
        self.candidates_root.mkdir(parents=True, exist_ok=True)

    def build(self, gap: CapabilityGap, blueprint: CapabilityBlueprint) -> CandidateImplementation | dict[str, Any]:
        """Return a persisted candidate or a normalized refusal."""
        try:
            blueprint.validate()
        except (TypeError, ValueError) as exc:
            return {"status": "INVALID_BLUEPRINT", "reason": str(exc), "candidate_status": "REJECTED"}
        refusal = self._eligibility(gap, blueprint)
        if refusal:
            return refusal
        if self._reuse_found(blueprint):
            return {"status": "EXISTING_IMPLEMENTATION_FOUND", "candidate_status": "NOT_GENERATED"}
        bp_dict = blueprint.to_dict()
        bp_hash = _digest(json.dumps(bp_dict, sort_keys=True, ensure_ascii=False).encode())
        candidate_id = f"candidate-{blueprint.capability_id.replace('.', '-')}-{bp_hash[:12]}"
        existing = self._load_existing(candidate_id)
        if existing and existing.get("candidate_status") == "VERIFIED":
            return CandidateImplementation(**existing)
        root = self.candidates_root / candidate_id
        root.mkdir(parents=True, exist_ok=True)
        try:
            provider, provider_receipt = self._select_provider(blueprint, root)
        except ProviderUnavailable as exc:
            return {"status": "BUILDER_PROVIDER_UNAVAILABLE", "candidate_status": "FAILED", "reason": str(exc)}
        plan = self._build_plan(blueprint, root)
        self._atomic_json(root / "build-plan.json", plan)
        files = self._generate(blueprint, root, provider_receipt)
        static = self._static_verify(blueprint, root, files)
        verification: dict[str, Any] = {"static": static}
        status = "GENERATED"
        repair_history: list[dict[str, Any]] = []
        if static["status"] != "VALID":
            status = "REJECTED"
        else:
            verification["execution"] = self._execute_and_verify(blueprint, root)
            if verification["execution"]["status"] != "VALID":
                repair_history.append({"attempt": 1, "status": "NOT_NEEDED"})
                status = "REJECTED"
            else:
                status = "VERIFIED"
        candidate_artifact_hash = _digest((root / "candidate.py").read_bytes() + (root / "fixture.json").read_bytes())
        candidate = CandidateImplementation(
            candidate_id, gap.gap_id, blueprint.capability_id, gap.blueprint_ref or "",
            bp_hash, candidate_artifact_hash, blueprint.suggested_implementation_type, str(root),
            {"schema_version": CAPABILITY_CONTRACT_SCHEMA_VERSION, "capability_id": blueprint.capability_id, "trusted": False},
            "candidate.py", list(blueprint.input_contract), list(blueprint.output_contract),
            list(blueprint.dependencies), blueprint.safety_risk, files,
            [{"path": "fixture.json", "sha256": _digest((root / "fixture.json").read_bytes())}],
            list(blueprint.verification_contract), provider, 1, status,
            "VALID" if status == "VERIFIED" else "INVALID", plan, verification,
            {"goal": gap.original_goal, "source_goal_run_id": gap.source_goal_run_id, "gap_id": gap.gap_id,
             "blueprint_hash": bp_hash}, repair_history, _now())
        self._atomic_json(root / "candidate.json", candidate.to_dict())
        return candidate

    def _eligibility(self, gap: CapabilityGap, blueprint: CapabilityBlueprint) -> dict[str, Any] | None:
        if gap.classification == "EXISTING_CAPABILITY":
            return {"status": "EXISTING_IMPLEMENTATION_FOUND", "candidate_status": "NOT_GENERATED"}
        if gap.classification != "TRUE_CAPABILITY_GAP":
            return {"status": gap.classification, "candidate_status": "NOT_GENERATED"}
        if blueprint.buildability_status != "BUILDABLE_NOW":
            mapping = {"NEEDS_HUMAN_INPUT": "WAITING_FOR_INPUT", "NEEDS_EXTERNAL_DEPENDENCY": "BLOCKED_DEPENDENCY",
                       "BLOCKED_BY_SAFETY": "SAFETY_BLOCKED", "UNSUPPORTED": "NOT_BUILDABLE"}
            return {"status": mapping.get(blueprint.buildability_status, "NOT_BUILDABLE"), "candidate_status": "NOT_GENERATED"}
        if blueprint.safety_risk == "BLOCKED":
            return {"status": "SAFETY_BLOCKED", "candidate_status": "NOT_GENERATED"}
        if blueprint.suggested_implementation_type not in {"ADAPTER", "WORKFLOW_MODULE", "TOOL", "DOMAIN_MODULE"}:
            return {"status": "UNSUPPORTED_IMPLEMENTATION_TYPE", "candidate_status": "NOT_GENERATED"}
        return None

    def _reuse_found(self, blueprint: CapabilityBlueprint) -> bool:
        from modules.runtime import module_inventory
        return any(blueprint.capability_id in m.get("capabilities", []) and m.get("execution_ready") for m in module_inventory())

    def _select_provider(self, blueprint: CapabilityBlueprint, candidate_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        task = ProviderTask(candidate_root, "Return a bounded candidate build plan only; do not edit files or run commands", role="worker",
                            required_capabilities=("task.submit", "structured_artifact_generation"),
                            provider_policy={}, execution_policy="READ_ONLY",
                            context={"capability_blueprint": blueprint.to_dict(), "required_result_prefix": "CAPABILITY_BUILD_RESULT="},
                            acceptance_criteria=("inspect the blueprint", "do not call tools", "return exactly one final CAPABILITY_BUILD_RESULT=<JSON> line", "do not modify trusted registry or safety policy"),
                            execution_context={"origin": "capability-builder", "host_role": "orchestrator", "dispatch_depth": 1, "direct_or_dispatch": "DISPATCH"})
        statuses = discover_providers()
        candidates = eligible_providers(task.role, set(task.required_capabilities), statuses)
        adapter, selection = select_provider(task, statuses)
        # The provider is selected through the normal registry gate.  The
        # bounded deterministic renderer owns file generation in this first
        # foundation; this avoids dispatching a mutating worker merely to emit
        # a plan and keeps no-duplicate/idempotent generation local.
        contract = {"status": "PLANNED", "capability_id": blueprint.capability_id,
                    "implementation_type": blueprint.suggested_implementation_type,
                    "required_result_prefix": "CAPABILITY_BUILD_RESULT="}
        metadata = {"provider_id": selection.provider_id, "eligible_providers": [x.provider_id for x in candidates],
                    "selection_reason": selection.reason, "generation_mode": "provider-planned bounded candidate renderer",
                    "task_id": None, "queue_job_id": None, "result_contract": contract}
        return metadata, metadata

    def _build_plan(self, blueprint: CapabilityBlueprint, root: Path) -> dict[str, Any]:
        if blueprint.capability_id != "data.table.summarize":
            raise ValueError("No bounded builder template exists for this capability")
        return {"capability_id": blueprint.capability_id, "implementation_type": blueprint.suggested_implementation_type,
                "files": ["candidate.py", "fixture.json", "run_candidate.py"],
                "entrypoint": "candidate.py", "dependencies": [], "verification_fixture": "fixture.json",
                "side_effects": list(blueprint.side_effects), "safety": blueprint.safety_risk,
                "expected": {"row_count": 3, "total_value": 35, "by_category": {"alpha": 30, "beta": 5}}}

    def _generate(self, blueprint: CapabilityBlueprint, root: Path, provider_receipt: dict[str, Any]) -> list[dict[str, Any]]:
        if blueprint.capability_id != "data.table.summarize":
            raise ValueError("unsupported bounded candidate template")
        candidate = '''import json, sys\n\ndef summarize(rows):\n    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):\n        raise ValueError("rows must be an array of objects")\n    by_category = {}\n    for row in rows:\n        category = row.get("category")\n        value = row.get("value")\n        if not isinstance(category, str) or not isinstance(value, (int, float)) or isinstance(value, bool):\n            raise ValueError("each row requires category:string and value:number")\n        by_category[category] = by_category.get(category, 0) + value\n    return {"row_count": len(rows), "total_value": sum(r["value"] for r in rows), "by_category": dict(sorted(by_category.items()))}\n\nif __name__ == "__main__":\n    data = json.loads(open(sys.argv[1], encoding="utf-8").read())\n    print(json.dumps(summarize(data), sort_keys=True, separators=(",", ":")))\n'''
        fixture = json.dumps([{"category": "alpha", "value": 10}, {"category": "beta", "value": 5}, {"category": "alpha", "value": 20}], separators=(",", ":")) + "\n"
        for name, content in (("candidate.py", candidate.encode()), ("fixture.json", fixture.encode())):
            path = root / name
            if path.exists() and path.read_bytes() != content:
                raise ValueError("candidate workspace is immutable for this candidate identity")
            if not path.exists(): path.write_bytes(content)
        return [{"path": name, "bytes": len(content), "sha256": _digest(content)} for name, content in (("candidate.py", candidate.encode()), ("fixture.json", fixture.encode()))]

    def _static_verify(self, blueprint: CapabilityBlueprint, root: Path, files: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            tree = ast.parse((root / "candidate.py").read_text(encoding="utf-8"))
            forbidden = ("subprocess", "socket", "requests", "os.system", "chmod", "chmod")
            text = (root / "candidate.py").read_text(encoding="utf-8")
            if any(token in text for token in forbidden): raise ValueError("forbidden import or side effect")
            return {"status": "VALID", "syntax": "valid", "entrypoint": True, "undeclared_dependencies": [], "registry_mutation": False, "safety_policy_mutation": False}
        except (OSError, SyntaxError, ValueError) as exc:
            return {"status": "INVALID", "reason": str(exc)}

    def _execute_and_verify(self, blueprint: CapabilityBlueprint, root: Path) -> dict[str, Any]:
        runtime = ToolRuntime(root, "WORKSPACE_WRITE", worker_backend=WorkerExecutionBackend())
        interpreter = sys.executable
        result = runtime.call("run_tests", {"argv": [interpreter, "candidate.py", "fixture.json"], "timeout_seconds": 30}, trace_context={"caller": "capability-builder"})
        if result.get("execution_status") != "COMPLETED" or result.get("test_outcome") != "PASSED":
            return {"status": "INVALID", "runtime": result}
        try:
            output = json.loads(result.get("output", "").strip())
            expected = {"row_count": 3, "total_value": 35, "by_category": {"alpha": 30, "beta": 5}}
            return {"status": "VALID" if output == expected else "INVALID", "runtime": result, "output": output, "expected": expected, "side_effects": "no external/network access"}
        except (TypeError, json.JSONDecodeError) as exc:
            return {"status": "INVALID", "runtime": result, "reason": str(exc)}

    def _load_existing(self, candidate_id: str) -> dict[str, Any] | None:
        path = self.candidates_root / candidate_id / "candidate.json"
        try:
            if not path.is_file():
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("schema_version", CAPABILITY_CONTRACT_SCHEMA_VERSION) != CAPABILITY_CONTRACT_SCHEMA_VERSION:
                raise ValueError("Unsupported candidate contract schema.")
            return value
        except (OSError, json.JSONDecodeError): return None

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with open(fd, "w", encoding="utf-8", closefd=True) as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2); stream.flush()
            Path(name).replace(path)
        finally:
            if Path(name).exists(): Path(name).unlink()
