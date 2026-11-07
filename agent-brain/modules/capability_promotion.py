"""Bounded independent promotion of low-risk verified candidates."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from modules.capability_builder import CandidateImplementation
from modules.capability_gap import CapabilityBlueprint, CapabilityGap
from modules.runtime import WORKBENCH_ROOT, generated_module_entry, load_module_registry, module_inventory

PROMOTION_SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class PromotionDecision:
    promotion_id: str
    candidate_id: str
    capability_id: str
    candidate_hash: str
    blueprint_ref: str
    reviewed_at: str
    review_status: str
    compatibility_status: str
    safety_status: str
    verification_status: str
    registry_conflicts: list[str]
    decision: str
    decision_reason: str
    registry_version_before: str
    registry_version_after: str | None
    rollback_ref: str
    package_ref: str | None = None
    package_hash: str | None = None
    canary: dict[str, Any] | None = None
    schema_version: int = PROMOTION_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CapabilityPromotion:
    """Promotion authority; candidate code has no access to this class."""

    def __init__(self, *, state_root: str | Path):
        self.state_root = Path(state_root).expanduser().resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.trusted_root = Path(os.environ.get("SYSTEM_AGENTIC_TRUSTED_ROOT", str(WORKBENCH_ROOT / ".system-agentic-trusted"))).expanduser().resolve()
        self.overlay = Path(os.environ.get("SYSTEM_AGENTIC_TRUSTED_REGISTRY", str(WORKBENCH_ROOT / ".system-agentic-trusted-registry.json"))).expanduser().resolve()
        self.trusted_root.mkdir(parents=True, exist_ok=True)
        self.promotions = self.state_root / "capability-promotions"
        self.promotions.mkdir(parents=True, exist_ok=True)

    def promote(self, gap: CapabilityGap, blueprint: CapabilityBlueprint, candidate: CandidateImplementation | dict[str, Any]) -> PromotionDecision | dict[str, Any]:
        if isinstance(candidate, dict):
            try:
                candidate = CandidateImplementation(**candidate)
            except (TypeError, ValueError) as exc:
                return {"decision": "REJECTED", "reason": f"invalid candidate contract: {exc}"}
        candidate_root = Path(candidate.candidate_root).resolve(strict=True)
        current_hash = self._candidate_hash(candidate_root)
        if current_hash != candidate.candidate_hash:
            return {"decision": "STALE_CANDIDATE", "reason": "verified candidate hash no longer matches current files"}
        if candidate.candidate_status != "VERIFIED" or candidate.verification_status != "VALID":
            return {"decision": "REJECTED", "reason": "candidate is not VERIFIED/VALID"}
        if gap.classification != "TRUE_CAPABILITY_GAP" or blueprint.buildability_status != "BUILDABLE_NOW":
            return {"decision": "REJECTED", "reason": "gap or buildability eligibility failed"}
        independent = self._independent_review(candidate, blueprint, candidate_root)
        if independent["status"] != "VALID":
            return {"decision": "REJECTED", "reason": independent["reason"]}
        before = self._registry_hash()
        existing = [m for m in module_inventory() if candidate.capability_id in m.get("capabilities", [])]
        promotion_id = f"promotion-{candidate.candidate_id}"
        decision_path = self.promotions / f"{promotion_id}.json"
        if decision_path.is_file():
            value = json.loads(decision_path.read_text(encoding="utf-8"))
            if value.get("schema_version", PROMOTION_SCHEMA_VERSION) != PROMOTION_SCHEMA_VERSION:
                return {"decision": "REJECTED", "reason": "unsupported promotion decision schema"}
            return value
        if existing:
            return {"decision": "CONFLICT", "reason": "trusted capability already exists", "registry_conflicts": [m["id"] for m in existing]}
        package = self.trusted_root / candidate.capability_id.replace(".", "-")
        if package.exists():
            return {"decision": "CONFLICT", "reason": "trusted package already exists"}
        package.mkdir(parents=True)
        shutil.copy2(candidate_root / "candidate.py", package / "candidate.py")
        shutil.copy2(candidate_root / "fixture.json", package / "fixture.json")
        package_hash = hashlib.sha256((package / "candidate.py").read_bytes() + (package / "fixture.json").read_bytes()).hexdigest()
        entry = generated_module_entry(candidate.capability_id, str(package), promotion_id=promotion_id,
                                       candidate_id=candidate.candidate_id, blueprint_ref=candidate.blueprint_ref,
                                       package_hash=package_hash)
        preimage = self.promotions / f"{promotion_id}.registry-before.json"
        if self.overlay.is_file():
            shutil.copy2(self.overlay, preimage)
        self._atomic_overlay({"schema_version": 1, "modules": [entry]})
        after = self._registry_hash()
        readiness = next((m for m in module_inventory() if m.get("id") == entry["id"]), None)
        if not readiness or not readiness.get("execution_ready"):
            self.rollback(package, entry["id"], preimage=preimage)
            return {"decision": "REJECTED", "reason": "registered package is not execution-ready"}
        decision = PromotionDecision(promotion_id, candidate.candidate_id, candidate.capability_id, package_hash,
            candidate.blueprint_ref, _now(), "VALID", "COMPATIBLE", "LOW", "VALID", [], "APPROVED",
            "independent low-risk review passed", before, after, str(self.promotions / f"{promotion_id}.rollback.json"), str(package), package_hash)
        self._atomic_json(decision_path, decision.to_dict())
        return decision

    def _independent_review(self, candidate: CandidateImplementation, blueprint: CapabilityBlueprint, root: Path) -> dict[str, Any]:
        try:
            blueprint.validate()
            manifest = candidate.manifest
            if manifest.get("trusted") is not False or candidate.capability_id != blueprint.capability_id:
                return {"status": "INVALID", "reason": "manifest trust or capability identity mismatch"}
            if not (root / "candidate.py").is_file() or not (root / "fixture.json").is_file():
                return {"status": "INVALID", "reason": "required candidate files missing"}
            source = (root / "candidate.py").read_text(encoding="utf-8")
            if any(token in source for token in ("socket", "subprocess", "requests", "os.system", "credential", "registry.json")):
                return {"status": "INVALID", "reason": "forbidden behavior in candidate source"}
            if candidate.safety_class not in {"LOW", "MEDIUM"}:
                return {"status": "INVALID", "reason": "risk is not eligible for bounded promotion"}
            if candidate.capability_id != "data.table.summarize":
                return {"status": "INVALID", "reason": "bounded promotion policy has no eligible implementation"}
            return {"status": "VALID", "risk": "LOW", "source_hash": _sha(root / "candidate.py"), "fixture_hash": _sha(root / "fixture.json")}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return {"status": "INVALID", "reason": str(exc)}

    def _candidate_hash(self, root: Path) -> str:
        return hashlib.sha256((root / "candidate.py").read_bytes() + (root / "fixture.json").read_bytes()).hexdigest()

    def _registry_hash(self) -> str:
        payload = self.overlay.read_bytes() if self.overlay.is_file() else b"{}"
        return hashlib.sha256(payload).hexdigest()

    def rollback(self, package: Path | None = None, module_id: str | None = None, *, preimage: Path | None = None) -> None:
        if package and package.exists(): shutil.rmtree(package)
        if preimage and preimage.is_file():
            os.replace(preimage, self.overlay)
        elif self.overlay.exists():
            self.overlay.unlink()
        audit = self.promotions / "rollback-audit.jsonl"
        self.promotions.mkdir(parents=True, exist_ok=True)
        with audit.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": _now(), "event": "promotion_rollback", "module_id": module_id,
                                     "package": str(package) if package else None}, sort_keys=True) + "\n")

    def _atomic_overlay(self, value: dict[str, Any]) -> None:
        self._atomic_json(self.overlay, value)

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2); stream.flush(); os.fsync(stream.fileno())
            Path(name).replace(path)
        finally:
            if Path(name).exists(): Path(name).unlink()
