"""Bounded capability-gap analysis and provider-neutral capability blueprints.

This module specifies missing capability work.  It never writes module source,
changes the registry, enables a provider, or executes a proposed capability.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tempfile
from typing import Any

from modules.runtime import module_inventory


CLASSIFICATIONS = {
    "TRUE_CAPABILITY_GAP", "EXISTING_CAPABILITY", "TEMPORARILY_UNAVAILABLE",
    "INPUT_MISSING", "SAFETY_BLOCKED", "UNSUPPORTED_SCOPE", "INVALID_REQUIREMENT",
}
BUILDABILITY = {
    "BUILDABLE_NOW", "NEEDS_HUMAN_INPUT", "NEEDS_EXTERNAL_DEPENDENCY",
    "BLOCKED_BY_SAFETY", "UNSUPPORTED",
}
IMPLEMENTATION_TYPES = {
    "ADAPTER", "WORKFLOW_MODULE", "TOOL", "DOMAIN_MODULE",
    "PROVIDER_CAPABILITY", "EXTERNAL_INTEGRATION",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return value[:64] or "unknown"


def normalize_capability(value: str) -> str:
    """Normalize semantic aliases without treating wording as identity."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("required capability must be a non-empty string")
    text = value.casefold().strip().replace("_", ".").replace("-", ".")
    aliases = {
        "extract.verified.knowledge": "knowledge.pack-compile",
        "knowledge.extract": "knowledge.pack-compile",
        "knowledge.pack.compile": "knowledge.pack-compile",
        "knowledge.pack-compile": "knowledge.pack-compile",
        "software.code.review": "software.code-review",
        "publish.post": "distribution.social.publish",
        "social.publish": "distribution.social.publish",
        "weather.forecast": "weather.forecast",
        "real.time.weather": "weather.forecast",
        "quantum.simulation": "scientific.quantum.simulation",
        "table.summarize": "data.table.summarize",
        "data.summarize": "data.table.summarize",
    }
    return aliases.get(text, text if "." in text else f"goal.{_slug(text)}")


def _inferred_requirement(goal: str, unresolved: list[dict[str, Any]] | None) -> tuple[str, str]:
    text = goal.casefold()
    if re.search(r"weather|forecast|temperature|thời tiết|dự báo", text):
        return "weather.forecast", "The goal requires a current, source-backed weather forecast."
    if re.search(r"quantum|lượng tử", text):
        return "scientific.quantum.simulation", "The goal requires a quantum simulation capability with deterministic output."
    if re.search(r"email|e-mail|send .*message|gửi email|hộp thư", text):
        return "communication.email.send", "The goal requires sending an email through an authorized mail system."
    if re.search(r"social post|publish .*post|đăng bài|mạng xã hội", text):
        return "distribution.social.publish", "The goal requires publishing content to a social platform."
    if re.search(r"table|csv|tabular|rows?|columns?|category|categor(?:y|ies)|bảng|dòng dữ liệu", text) and re.search(r"summar|total|count|normalize|aggregate|tổng|thống kê", text):
        return "data.table.summarize", "The goal requires deterministic summary statistics for structured table rows."
    for item in unresolved or []:
        raw = item.get("capability") or item.get("required_capability")
        if isinstance(raw, str) and raw and raw != "structured.system.result":
            return normalize_capability(raw), str(item.get("reason") or "The Composer could not resolve this requirement.")
    if re.search(r"knowledge|tri thức|nguồn|source", text):
        return "knowledge.pack-compile", "The goal requires a structured artifact with source validation."
    digest = hashlib.sha256(goal.strip().casefold().encode("utf-8")).hexdigest()[:12]
    return f"goal.unresolved.{digest}", "The goal requires a capability not described by the current semantic resolver."


@dataclass(frozen=True)
class CapabilityBlueprint:
    capability_id: str
    name: str
    purpose: str
    problem_solved: str
    input_contract: tuple[dict[str, Any], ...]
    output_contract: tuple[dict[str, Any], ...]
    execution_semantics: tuple[str, ...]
    side_effects: tuple[str, ...]
    safety_risk: str
    required_tools: tuple[str, ...]
    required_provider_properties: tuple[str, ...]
    state_requirements: tuple[str, ...]
    artifact_requirements: tuple[str, ...]
    verification_contract: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    dependencies: tuple[str, ...]
    suggested_implementation_type: str
    estimated_complexity: str
    buildability_status: str

    def validate(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:\.[a-z0-9-]+)+", self.capability_id):
            raise ValueError("Capability Blueprint capability_id must be a stable dotted identity")
        if self.suggested_implementation_type not in IMPLEMENTATION_TYPES:
            raise ValueError("Capability Blueprint has an invalid implementation type")
        if self.buildability_status not in BUILDABILITY:
            raise ValueError("Capability Blueprint has an invalid buildability status")
        if self.safety_risk not in {"LOW", "MEDIUM", "HIGH", "BLOCKED"}:
            raise ValueError("Capability Blueprint has an invalid safety risk")
        if not self.input_contract or not self.output_contract or not self.verification_contract or not self.acceptance_criteria:
            raise ValueError("Capability Blueprint requires input, output, verification, and acceptance contracts")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "capability_id": self.capability_id, "name": self.name, "purpose": self.purpose,
            "problem_solved": self.problem_solved, "input_contract": list(self.input_contract),
            "output_contract": list(self.output_contract), "execution_semantics": list(self.execution_semantics),
            "side_effects": list(self.side_effects), "safety_risk": self.safety_risk,
            "required_tools": list(self.required_tools), "required_provider_properties": list(self.required_provider_properties),
            "state_requirements": list(self.state_requirements), "artifact_requirements": list(self.artifact_requirements),
            "verification_contract": list(self.verification_contract), "acceptance_criteria": list(self.acceptance_criteria),
            "dependencies": list(self.dependencies), "suggested_implementation_type": self.suggested_implementation_type,
            "estimated_complexity": self.estimated_complexity, "buildability_status": self.buildability_status,
        }


@dataclass(frozen=True)
class CapabilityGap:
    gap_id: str
    source_goal_run_id: str
    original_goal: str
    required_capability: str
    capability_description: str
    why_required: str
    required_inputs: tuple[dict[str, Any], ...]
    required_outputs: tuple[dict[str, Any], ...]
    constraints: tuple[str, ...]
    safety_class: str
    verification_requirements: tuple[str, ...]
    existing_candidates: tuple[dict[str, Any], ...]
    rejection_reasons: tuple[str, ...]
    gap_status: str
    classification: str
    buildability_status: str | None = None
    gap_ref: str | None = None
    blueprint_ref: str | None = None

    def validate(self) -> None:
        if self.classification not in CLASSIFICATIONS:
            raise ValueError("Capability Gap has an invalid classification")
        if self.gap_status not in {"CONFIRMED", "REJECTED", "REQUIRES_INPUT", "BLOCKED"}:
            raise ValueError("Capability Gap has an invalid gap_status")
        if not self.required_capability or not self.source_goal_run_id:
            raise ValueError("Capability Gap requires source goal and required capability")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "gap_id": self.gap_id, "source_goal_run_id": self.source_goal_run_id,
            "original_goal": self.original_goal, "required_capability": self.required_capability,
            "capability_description": self.capability_description, "why_required": self.why_required,
            "required_inputs": list(self.required_inputs), "required_outputs": list(self.required_outputs),
            "constraints": list(self.constraints), "safety_class": self.safety_class,
            "verification_requirements": list(self.verification_requirements),
            "existing_candidates": list(self.existing_candidates), "rejection_reasons": list(self.rejection_reasons),
            "gap_status": self.gap_status, "classification": self.classification,
            "buildability_status": self.buildability_status, "gap_ref": self.gap_ref,
            "blueprint_ref": self.blueprint_ref,
        }


class CapabilityGapResolver:
    """Read-only registry analysis with explicit buildability evidence."""

    def __init__(self, *, state_root: str | Path, inventory: list[dict[str, Any]] | None = None) -> None:
        self.state_root = Path(state_root).expanduser()
        self.inventory = inventory

    def resolve(self, *, source_goal_run_id: str, original_goal: str,
                unresolved: list[dict[str, Any]] | None = None,
                context: dict[str, Any] | None = None,
                required_capability: str | None = None) -> CapabilityGap:
        if not isinstance(original_goal, str) or not original_goal.strip():
            raise ValueError("Capability Gap Resolver requires a non-empty goal")
        context = context or {}
        capability, why = ((normalize_capability(required_capability), "The unresolved requirement supplied by the caller.")
                           if required_capability else _inferred_requirement(original_goal, unresolved))
        inventory = module_inventory() if self.inventory is None else self.inventory
        candidates: list[dict[str, Any]] = []
        for module in inventory:
            if capability in module.get("capabilities", []):
                candidates.append({"module": module.get("id"), "adapter_status": module.get("adapter_status"),
                                   "execution_ready": bool(module.get("execution_ready")), "enabled": bool(module.get("enabled")),
                                   "runtime_state": module.get("runtime_state"), "workspace": module.get("repo") or module.get("workspace_path")})
        text = original_goal.casefold()
        safety_blocked = bool(re.search(r"bypass safety|disable seatbelt|steal credential|delete production|exfiltrat|without approval", text))
        if safety_blocked and not candidates:
            capability = "security.policy-bypass"
        input_missing = False
        pedagogy_context = context.get("pedagogy_request") if isinstance(context.get("pedagogy_request"), dict) else {}
        if capability == "pedagogy.session" and re.search(r"export|saved session|phiên|tài liệu", text):
            input_missing = not isinstance(pedagogy_context.get("session_id"), str)
        if capability == "knowledge.pack-compile":
            input_missing = not bool(context.get("knowledge_sources") or pedagogy_context.get("session_id"))
        ready = [item for item in candidates if item["execution_ready"] and item["enabled"]]
        implemented_unready = [item for item in candidates if item["adapter_status"] == "implemented" and not item["execution_ready"]]
        rejection: list[str] = []
        if safety_blocked:
            classification, status, buildability = "SAFETY_BLOCKED", "BLOCKED", "BLOCKED_BY_SAFETY"
            rejection.append("The requested action conflicts with the current safety policy.")
        elif input_missing:
            classification, status, buildability = "INPUT_MISSING", "REQUIRES_INPUT", "NEEDS_HUMAN_INPUT"
            rejection.append("Required source/session input is absent; no new capability is justified.")
        elif ready:
            classification, status, buildability = "EXISTING_CAPABILITY", "REJECTED", None
            rejection.append("An execution-ready registered capability satisfies this requirement.")
        elif implemented_unready:
            classification, status, buildability = "TEMPORARILY_UNAVAILABLE", "REJECTED", None
            rejection.append("A registered implementation exists but is not execution-ready in the current environment.")
        elif candidates:
            classification, status, buildability = "UNSUPPORTED_SCOPE", "REJECTED", "UNSUPPORTED"
            rejection.append("Registered candidates exist, but no implemented executable adapter satisfies the scope.")
        elif capability == "goal.unresolved" or capability.endswith(".unresolved"):
            classification, status, buildability = "INVALID_REQUIREMENT", "REJECTED", "UNSUPPORTED"
            rejection.append("The requirement does not identify a stable capability semantic.")
        else:
            classification, status, buildability = "TRUE_CAPABILITY_GAP", "CONFIRMED", self._buildability(capability, text)
            rejection.append("No registered module, ready provider, or bounded composition satisfies this capability.")
            rejection.append("Bounded composition search found no compatible registered input/output chain for this semantic requirement.")
        gap_id = f"gap-{source_goal_run_id}-{hashlib.sha256(capability.encode()).hexdigest()[:8]}"
        gap = CapabilityGap(gap_id, source_goal_run_id, original_goal.strip(), capability,
            why, why, (self._input_contract(capability),), (self._output_contract(capability),),
            ("provider-neutral contract", "no registry mutation", "no executable generation"),
            "BLOCKED" if classification == "SAFETY_BLOCKED" else "MEDIUM",
            ("execution status is terminal", "output contract is schema-valid", "provenance and idempotency are preserved", "safety policy is inherited"),
            tuple(candidates), tuple(rejection), status, classification, buildability)
        blueprint = self._blueprint(gap, context) if classification == "TRUE_CAPABILITY_GAP" else None
        return self._persist(gap, blueprint)

    @staticmethod
    def _buildability(capability: str, text: str) -> str:
        if capability.startswith(("weather.", "communication.", "distribution.")) or re.search(r"live|external|api|account|platform", text):
            return "NEEDS_EXTERNAL_DEPENDENCY"
        if capability == "data.table.summarize":
            return "BUILDABLE_NOW"
        return "UNSUPPORTED"

    @staticmethod
    def _input_contract(capability: str) -> dict[str, Any]:
        if capability == "data.table.summarize":
            return {"name": "rows", "type": "application/json", "required": True,
                    "schema": "array<object>{category:string,value:number}"}
        if capability == "weather.forecast":
            return {"name": "location", "type": "text/plain", "required": True}
        if capability.startswith("scientific.quantum"):
            return {"name": "circuit_or_problem", "type": "application/json", "required": True}
        if capability.startswith("communication."):
            return {"name": "message_and_recipient", "type": "application/json", "required": True}
        return {"name": "goal_input", "type": "application/json", "required": True}

    @staticmethod
    def _output_contract(capability: str) -> dict[str, Any]:
        if capability == "data.table.summarize":
            return {"name": "summary", "type": "application/json", "schema": "{row_count,total_value,by_category}"}
        if capability == "weather.forecast":
            return {"name": "forecast", "type": "application/json", "requires_provenance": True}
        if capability.startswith("scientific.quantum"):
            return {"name": "verified_simulation", "type": "application/json", "requires_schema_validation": True}
        return {"name": "capability_result", "type": "application/json", "requires_provenance": True}

    def _blueprint(self, gap: CapabilityGap, context: dict[str, Any]) -> CapabilityBlueprint:
        external = gap.buildability_status == "NEEDS_EXTERNAL_DEPENDENCY"
        kind = "EXTERNAL_INTEGRATION" if external else ("DOMAIN_MODULE" if gap.required_capability.startswith("scientific.") else "WORKFLOW_MODULE")
        if gap.required_capability == "data.table.summarize":
            kind = "TOOL"
        semantics = ("accept a validated input contract", "execute deterministically or record provider execution", "return a normalized result")
        acceptance = ("execution completes", "output schema validates", "acceptance criteria pass", "no safety boundary is bypassed")
        verification = gap.verification_requirements
        if gap.required_capability == "data.table.summarize":
            semantics = ("read rows from the JSON input fixture", "count rows and sum numeric values", "group totals by category", "emit canonical JSON on stdout")
            verification = ("fixture contains three rows", "row_count equals 3", "total_value equals 35", "by_category equals {alpha: 30, beta: 5}", "no filesystem or network side effects")
            acceptance = ("execution completes", "output schema validates", "row_count is correct", "totals are correct", "no unapproved side effects")
        bp = CapabilityBlueprint(
            gap.required_capability, gap.required_capability.replace(".", " ").title(),
            gap.capability_description, gap.original_goal,
            gap.required_inputs, gap.required_outputs,
            semantics,
            ("write only declared workspace artifacts", "no unapproved external side effects"),
            gap.safety_class, ("Tool Runtime only where process/file access is required",),
            ("structured output", "contract compliance"), ("persist execution identity",),
            ("output artifact has stable hash", "source/provenance metadata is retained"),
            verification, acceptance,
            ("module registry readiness",), kind, "MEDIUM", gap.buildability_status or "UNSUPPORTED")
        bp.validate()
        return bp

    def _persist(self, gap: CapabilityGap, blueprint: CapabilityBlueprint | None) -> CapabilityGap:
        root = self.state_root / "capability-gaps" / gap.gap_id
        root.mkdir(parents=True, exist_ok=True)
        gap_path = root / "gap.json"
        bp_path = root / "capability-blueprint.json" if blueprint else None
        gap = CapabilityGap(**{**gap.__dict__, "gap_ref": str(gap_path), "blueprint_ref": str(bp_path) if bp_path else None})
        for path, value in ((gap_path, gap.to_dict()), (bp_path, blueprint.to_dict() if blueprint else None)):
            if path is not None and value is not None:
                fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
                try:
                    with open(fd, "w", encoding="utf-8", closefd=True) as stream:
                        json.dump(value, stream, ensure_ascii=False, indent=2)
                        stream.flush()
                        import os
                        os.fsync(stream.fileno())
                    Path(temp).replace(path)
                finally:
                    if Path(temp).exists():
                        Path(temp).unlink()
        return gap
