"""Provider-neutral module registry views and deterministic workspace paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modules.runtime import ModuleUnavailable, WORKBENCH_ROOT, load_module_registry


@dataclass(frozen=True)
class ModuleDefinition:
    module_id: str
    display_name: str
    workspace_path: str
    capabilities: tuple[str, ...]
    status: str
    execution_mode: str
    execution_adapter: str | None
    health_check: str | None
    metadata: dict[str, Any]
    role: str = "UTILITY"
    adapter_status: str = "NOT_IMPLEMENTED"
    adapter_readiness: str = "BLOCKED"
    input_artifact_types: tuple[str, ...] = ()
    output_artifact_types: tuple[str, ...] = ()
    execution_modes: tuple[str, ...] = ("READ_ONLY", "MUTATING")
    mutation_mode: str = "MUTATING"
    max_concurrency: int = 1
    priority: int = 0
    enabled: bool = True
    availability: bool | None = None
    test_command: str | None = None

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "ModuleDefinition":
        declared = record.get("workspace_path")
        root = Path(declared).expanduser() if declared else WORKBENCH_ROOT / record["repo"]
        return cls(
            module_id=record["id"],
            display_name=record["name"],
            workspace_path=str(root.resolve(strict=False)),
            capabilities=tuple(record["capabilities"]),
            status=record.get("status", "UNKNOWN"),
            execution_mode=record.get("execution_mode", record["invocation_type"]),
            execution_adapter=record.get("execution_adapter"),
            health_check=record.get("health_check"),
            metadata=dict(record.get("metadata", {})),
            role=record.get("role", "UTILITY"),
            adapter_status=record.get("adapterStatus", "NOT_IMPLEMENTED"),
            adapter_readiness=record.get("adapter_readiness", "BLOCKED"),
            input_artifact_types=tuple(record.get("input_artifact_types", [])),
            output_artifact_types=tuple(record.get("output_artifact_types", [])),
            execution_modes=tuple(record.get("execution_modes", ("READ_ONLY", "MUTATING"))),
            mutation_mode=record.get("mutation_mode", "MUTATING"),
            max_concurrency=record.get("max_concurrency", 1),
            priority=record.get("priority", 0),
            enabled=record.get("enabled", True),
            availability=record.get("available"),
            test_command=record.get("test_command"),
        )


class ModuleRegistry:
    """Read-only lookup over the canonical modules registry; it never routes providers."""

    def __init__(self, registry: dict[str, Any] | None = None) -> None:
        self._registry = registry or load_module_registry()
        records = self._registry.get("modules")
        if not isinstance(records, list):
            raise ValueError("Module registry must contain a modules list.")
        ids = [item.get("id") for item in records if isinstance(item, dict)]
        if len(ids) != len(records) or len(ids) != len(set(ids)):
            raise ValueError("Module registry entries must have unique IDs.")
        self._modules = tuple(ModuleDefinition.from_record(item) for item in records)
        for module in self._modules:
            if module.role not in {"CREATION", "TRANSFORMATION", "ANALYSIS", "GTM", "DISTRIBUTION", "UTILITY"}:
                raise ValueError(f"Module {module.module_id!r} has invalid role.")
            if module.adapter_status not in {"NOT_IMPLEMENTED", "FIXTURE_ONLY", "READ_ONLY", "PRODUCTION_READY"}:
                raise ValueError(f"Module {module.module_id!r} has invalid adapter status.")
            if module.adapter_readiness not in {"READY_FOR_ADAPTER", "NEEDS_STABLE_ENTRYPOINT", "CONTROLLED", "BLOCKED"}:
                raise ValueError(f"Module {module.module_id!r} has invalid adapter readiness.")
            if (not isinstance(module.max_concurrency, int) or isinstance(module.max_concurrency, bool)
                    or module.max_concurrency < 1 or not isinstance(module.priority, int)
                    or isinstance(module.priority, bool)):
                raise ValueError(f"Module {module.module_id!r} has invalid concurrency/priority metadata.")
            if type(module.enabled) is not bool or (module.availability is not None and type(module.availability) is not bool):
                raise ValueError(f"Module {module.module_id!r} has invalid enabled/availability metadata.")
            if module.mutation_mode not in {"READ_ONLY", "MUTATING", "MIXED"}:
                raise ValueError(f"Module {module.module_id!r} has invalid mutation_mode.")
            if not module.execution_modes or any(mode not in {"READ_ONLY", "MUTATING"} for mode in module.execution_modes):
                raise ValueError(f"Module {module.module_id!r} has invalid execution_modes.")
            for field_name, values in (("input_artifact_types", module.input_artifact_types),
                                       ("output_artifact_types", module.output_artifact_types)):
                if any(not isinstance(value, str) or not value.strip() for value in values):
                    raise ValueError(f"Module {module.module_id!r} has invalid {field_name}.")
        roots: dict[str, str] = {}
        for module in self._modules:
            previous = roots.get(module.workspace_path)
            if previous is not None and previous != module.module_id:
                raise ValueError(f"Modules {previous!r} and {module.module_id!r} resolve to the same workspace root.")
            roots[module.workspace_path] = module.module_id

    def list_modules(self) -> list[ModuleDefinition]:
        return list(self._modules)

    def get_module(self, module_id: str) -> ModuleDefinition:
        matches = [module for module in self._modules if module.module_id == module_id]
        if not matches:
            raise ModuleUnavailable(f"Module {module_id!r} is not registered.")
        return matches[0]

    def find_modules_by_capability(self, capability: str) -> list[ModuleDefinition]:
        if not isinstance(capability, str) or not capability.strip():
            raise ValueError("A non-empty capability is required.")
        return [module for module in self._modules if capability in module.capabilities]


class WorkspaceResolver:
    """Resolve a registered module root, then a contained relative path."""

    def __init__(self, registry: ModuleRegistry | None = None, current_workspace: str | Path | None = None) -> None:
        self.registry = registry or ModuleRegistry()
        self.current_workspace = Path(current_workspace).expanduser().resolve(strict=False) if current_workspace else None

    def resolve_module_workspace(self, module_id: str) -> Path:
        module = self.registry.get_module(module_id)
        root = Path(module.workspace_path).expanduser()
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise ModuleUnavailable(f"Registered workspace for {module_id!r} does not exist: {root}") from exc
        if not resolved.is_dir():
            raise ModuleUnavailable(f"Registered workspace for {module_id!r} is not a directory: {resolved}")
        return resolved

    def resolve_path(self, module_id: str, relative_path: str | Path = ".") -> Path:
        root = self.resolve_module_workspace(module_id)
        relative = Path(relative_path)
        if relative.is_absolute():
            raise ValueError("Module-relative path must not be absolute.")
        resolved = (root / relative).resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise ValueError("Module-relative path escapes the registered workspace.")
        return resolved


@dataclass(frozen=True)
class ModuleExecutionContext:
    """Validated module scope passed unchanged from host dispatch to a worker."""

    target_module_id: str
    requested_capability: str
    resolved_workspace: str
    workspace_authority: str
    pipeline_id: str | None = None
    step_id: str | None = None
    product_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"targetModuleId": self.target_module_id,
                "requestedCapability": self.requested_capability,
                "resolvedWorkspace": self.resolved_workspace,
                "workspaceAuthority": self.workspace_authority,
                "pipelineId": self.pipeline_id, "stepId": self.step_id,
                "productId": self.product_id}

    @classmethod
    def resolve(cls, module_id: str, capability: str, *, resolver: WorkspaceResolver | None = None,
                pipeline_id: str | None = None, step_id: str | None = None,
                product_id: str | None = None) -> "ModuleExecutionContext":
        resolver = resolver or WorkspaceResolver()
        module = resolver.registry.get_module(module_id)
        if capability not in module.capabilities:
            raise ValueError(f"Capability {capability!r} is not registered for module {module_id!r}.")
        root = resolver.resolve_module_workspace(module_id)
        return cls(module_id, capability, str(root), "MODULE_REGISTRY", pipeline_id, step_id, product_id)

    @classmethod
    def validate(cls, value: Any, workspace: str | Path, *, resolver: WorkspaceResolver | None = None) -> "ModuleExecutionContext":
        if not isinstance(value, dict):
            raise ValueError("moduleExecutionContext must be an object.")
        expected = {"targetModuleId", "requestedCapability", "resolvedWorkspace", "workspaceAuthority"}
        if any(not isinstance(value.get(key), str) or not value[key].strip() for key in expected):
            raise ValueError("moduleExecutionContext requires module, capability, resolved workspace, and authority.")
        context = cls.resolve(value["targetModuleId"], value["requestedCapability"], resolver=resolver,
                              pipeline_id=value.get("pipelineId"), step_id=value.get("stepId"),
                              product_id=value.get("productId"))
        if value["workspaceAuthority"] != "MODULE_REGISTRY" or value["resolvedWorkspace"] != context.resolved_workspace:
            raise ValueError("Module workspace authority does not match canonical registry resolution.")
        assigned = Path(workspace).expanduser().resolve(strict=True)
        if assigned != Path(context.resolved_workspace):
            raise ValueError("Queued workspace differs from the authoritative module workspace.")
        return context
