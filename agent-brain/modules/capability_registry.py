"""Dynamic capability lookup and deterministic provider selection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from modules.workspace_resolver import ModuleRegistry


class CapabilityResolutionError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


@dataclass(frozen=True)
class ModuleHealth:
    module_id: str
    status: str
    checked_at: str
    evidence: dict[str, Any]

    def __post_init__(self) -> None:
        if self.status not in {"READY", "DEGRADED", "UNAVAILABLE", "AUTH_REQUIRED", "DISABLED", "UNKNOWN"}:
            raise ValueError("Unsupported module health status.")
        if not self.module_id.strip() or not self.checked_at.strip() or not isinstance(self.evidence, dict):
            raise ValueError("ModuleHealth requires moduleId, checkedAt, and object evidence.")

    @classmethod
    def create(cls, module_id: str, status: str, evidence: dict[str, Any] | None = None) -> "ModuleHealth":
        return cls(module_id, status, datetime.now(timezone.utc).isoformat(timespec="milliseconds"), dict(evidence or {}))

    def to_dict(self) -> dict[str, Any]:
        return {"moduleId": self.module_id, "status": self.status, "checkedAt": self.checked_at,
                "evidence": dict(self.evidence)}


@dataclass(frozen=True)
class CapabilityProvider:
    module_id: str
    capability: str
    workspace_path: str
    health: str
    availability: bool
    execution_modes: tuple[str, ...]
    priority: int
    max_concurrency: int
    execution_adapter: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ProviderSelection:
    provider: CapabilityProvider
    policy: str
    evidence: dict[str, Any]


class CapabilityProviderRegistry:
    """Read-only dynamic index; provider health is supplied by the caller."""

    def __init__(self, modules: ModuleRegistry | None = None,
                 health_check: Callable[[str], dict[str, Any]] | None = None) -> None:
        self.modules = modules or ModuleRegistry()
        self.health_check = health_check

    def find_providers(self, capability: str, *, execution_mode: str | None = None) -> list[CapabilityProvider]:
        if not isinstance(capability, str) or not capability.strip():
            raise ValueError("A non-empty capability is required.")
        if execution_mode is not None and execution_mode not in {"READ_ONLY", "MUTATING"}:
            raise ValueError("Unsupported execution mode.")
        found = []
        for module in self.modules.list_modules():
            if capability not in module.capabilities:
                continue
            try:
                exists = Path(module.workspace_path).resolve(strict=True).is_dir()
            except OSError:
                exists = False
            enabled = module.enabled and module.availability is not False
            available = bool(enabled and exists)
            health = module.status
            if self.health_check is not None:
                try:
                    value = self.health_check(module.module_id)
                    health = value.status if isinstance(value, ModuleHealth) else value.get("status", "UNKNOWN") if isinstance(value, dict) else "UNKNOWN"
                    available = available and health == "READY"
                except Exception:
                    health = "UNAVAILABLE"
                    available = False
            modes = module.execution_modes
            if execution_mode and execution_mode not in modes:
                available = False
            found.append(CapabilityProvider(module.module_id, capability, module.workspace_path,
                                            health, available, modes, module.priority, module.max_concurrency,
                                            module.execution_adapter,
                                            dict(module.metadata)))
        return sorted(found, key=lambda item: item.module_id)


class ProviderSelectionPolicy:
    """Select explicit, sole eligible, or uniquely highest-priority provider."""

    def __init__(self, mode: str = "single", configured_priority: dict[str, int] | None = None) -> None:
        if mode not in {"single", "priority"}:
            raise ValueError("Selection policy must be 'single' or 'priority'.")
        if configured_priority is not None and any(type(rank) is not int for rank in configured_priority.values()):
            raise ValueError("Configured provider priorities must be integers.")
        self.mode = mode
        self.configured_priority = dict(configured_priority or {})

    def select(self, providers: list[CapabilityProvider], *, explicit_module_id: str | None = None) -> ProviderSelection:
        eligible = [provider for provider in providers if provider.availability and provider.health == "READY"]
        if explicit_module_id is not None:
            selected = next((provider for provider in eligible if provider.module_id == explicit_module_id), None)
            if selected is None:
                raise CapabilityResolutionError("EXPLICIT_PROVIDER_UNAVAILABLE",
                                                f"Explicit provider {explicit_module_id!r} is not eligible.",
                                                {"eligible": [item.module_id for item in eligible]})
            return ProviderSelection(selected, "explicit_module", {"selected": selected.module_id})
        if not eligible:
            raise CapabilityResolutionError("NO_ELIGIBLE_PROVIDER", "No healthy, available provider satisfies the capability.",
                                            {"candidates": [asdict(item) for item in providers]})
        if len(eligible) == 1:
            selected = eligible[0]
            return ProviderSelection(selected, "single_eligible_provider", {"selected": selected.module_id})
        if self.mode != "priority":
            raise CapabilityResolutionError("AMBIGUOUS_PROVIDER", "Multiple eligible providers require an explicit provider or configured priority.",
                                            {"eligible": [item.module_id for item in eligible]})
        ranked = [(self.configured_priority.get(item.module_id, item.priority), item) for item in eligible]
        top = max(rank for rank, _ in ranked)
        winners = [item for rank, item in ranked if rank == top]
        if len(winners) != 1:
            raise CapabilityResolutionError("AMBIGUOUS_PRIORITY_TIE", "Configured provider priority does not uniquely select a provider.",
                                            {"priority": top, "tied": [item.module_id for item in winners]})
        selected = winners[0]
        return ProviderSelection(selected, "configured_priority", {
            "selected": selected.module_id, "priority": top,
            "eligible": [{"moduleId": item.module_id,
                          "priority": self.configured_priority.get(item.module_id, item.priority)} for item in eligible],
        })
