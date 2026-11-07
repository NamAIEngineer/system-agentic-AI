"""Module registration and capability discovery, separate from provider routing."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from providers.runtime import ProviderResult, ProviderTask, ProviderUnavailable
from optimization import optimize_provider_summary


WORKBENCH_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = Path(__file__).with_name("registry.json")
TRUSTED_OVERLAY_PATH = Path(os.environ.get("SYSTEM_AGENTIC_TRUSTED_REGISTRY", str(WORKBENCH_ROOT / ".system-agentic-trusted-registry.json")))
TRUSTED_OVERLAY_SCHEMA_VERSION = 1
GSTACK_REVIEW_REQUIRED_PHASES = (
    "preflight", "startup", "gate_scan", "core_review", "exploratory_qa", "adversarial_review", "review_log_finish",
)
GSTACK_REVIEW_SCRATCH_MARKER = ".system-agentic-synthetic-review"
MINIMUM_BUN_VERSION = (1, 3, 3)
GSTACK_GATE_COUNTS = ("listed", "eligible", "inspected", "unread", "unmatched")
GSTACK_FINGERPRINT_RE = re.compile(r"^[0-9a-fA-F]{12,64}$")
KNOWLEDGE_BUILDER_RESULT_PREFIX = "KNOWLEDGE_BUILDER_RESULT="


def _portable_workspace_path(value: str, module_id: str) -> str:
    """Resolve registry placeholders without embedding a host absolute path."""
    expanded = os.path.expanduser(value)
    unresolved = re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", expanded)
    for name in unresolved:
        replacement = os.environ.get(name, "")
        if replacement:
            expanded = expanded.replace("${" + name + "}", replacement)
    if not Path(expanded).is_absolute():
        expanded = str(Path(tempfile.gettempdir()) / "system-agentic-ai-unconfigured" / module_id)
    return str(Path(expanded).resolve(strict=False))


class ModuleUnavailable(ProviderUnavailable):
    pass


@dataclass(frozen=True)
class ModuleResult:
    """Provider-neutral result contract for deterministic local domain modules."""

    task_id: str
    module: str
    execution_status: str
    capability: str
    domain_outcome: str
    result: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "module": self.module,
            "execution_status": self.execution_status,
            "capability": self.capability,
            "domain_outcome": self.domain_outcome,
            **self.result,
        }


def load_module_registry() -> dict[str, Any]:
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    modules = registry.get("modules") if registry.get("schema_version") == 1 else None
    if not isinstance(modules, list):
        raise ValueError("Unsupported module registry schema.")
    ids = [module.get("id") for module in modules if isinstance(module, dict)]
    if len(ids) != len(modules) or len(ids) != len(set(ids)):
        raise ValueError("Module registry entries must have unique IDs.")
    optimization_layers = registry.get("optimization_layers", [])
    if not isinstance(optimization_layers, list):
        raise ValueError("optimization_layers must be a list.")
    layer_ids = [layer.get("id") for layer in optimization_layers if isinstance(layer, dict)]
    if len(layer_ids) != len(optimization_layers) or len(layer_ids) != len(set(layer_ids)) or set(layer_ids) & set(ids):
        raise ValueError("Optimization layers must have unique IDs separate from domain modules.")
    for module in modules:
        for field in (
            "id", "repo", "enabled", "available", "task_types", "capabilities",
            "invocation_type", "adapter_status", "requires_provider", "verification",
            "availability_probe", "declared_runtime_state",
        ):
            if field not in module:
                raise ValueError(f"Module {module['id']} is missing {field}.")
        if type(module["enabled"]) is not bool or module["available"] is not None:
            raise ValueError(f"Module {module['id']} must declare enabled and defer available state to runtime.")
        if module["adapter_status"] not in {"implemented", "not_implemented"}:
            raise ValueError(f"Module {module['id']} has an unsupported adapter status.")
        workspace_path = module.get("workspace_path")
        if workspace_path is not None:
            if not isinstance(workspace_path, str):
                raise ValueError(f"Module {module['id']} workspace_path must be a string.")
            module["workspace_path"] = _portable_workspace_path(workspace_path, module["id"])
        status = module.get("status", "UNKNOWN")
        if status not in {"READY", "UNAVAILABLE", "AUTH_REQUIRED", "DEGRADED", "DISABLED", "UNKNOWN"}:
            raise ValueError(f"Module {module['id']} has an unsupported health status.")
        if not isinstance(module.get("capabilities"), list) or any(not isinstance(value, str) or not value for value in module["capabilities"]):
            raise ValueError(f"Module {module['id']} capabilities must be non-empty strings.")
    canonical_roots: dict[str, str] = {}
    for module in modules:
        declared = module.get("workspace_path")
        root = Path(declared).expanduser().resolve(strict=False) if declared else (WORKBENCH_ROOT / module["repo"]).resolve(strict=False)
        key = str(root)
        owner = canonical_roots.get(key)
        if owner is not None and owner != module["id"]:
            raise ValueError(f"Modules {owner!r} and {module['id']!r} resolve to the same workspace root.")
        canonical_roots[key] = module["id"]
    overlay_path = Path(os.environ.get("SYSTEM_AGENTIC_TRUSTED_REGISTRY", str(TRUSTED_OVERLAY_PATH)))
    if overlay_path.is_file():
        try:
            overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
            if not isinstance(overlay, dict) or overlay.get("schema_version") != TRUSTED_OVERLAY_SCHEMA_VERSION:
                raise ValueError("Unsupported trusted generated registry schema.")
            generated_modules = overlay.get("modules")
            if not isinstance(generated_modules, list):
                raise ValueError("trusted generated registry modules must be a list")
            existing_ids = {m["id"] for m in registry["modules"]}
            existing_caps = {cap for m in registry["modules"] for cap in m.get("capabilities", [])}
            seen_ids: set[str] = set()
            seen_caps: set[str] = set()
            for generated in generated_modules:
                if not isinstance(generated, dict) or not generated.get("id"):
                    raise ValueError("trusted generated registry entry is malformed")
                gid = generated["id"]
                if gid in seen_ids:
                    raise ValueError(f"duplicate trusted generated module id: {gid}")
                seen_ids.add(gid)
                if gid in existing_ids:
                    raise ValueError(f"trusted generated module conflicts with base registry: {gid}")
                if generated.get("generated_origin") != "promoted" or not generated.get("promotion_id"):
                    raise ValueError(f"trusted generated module {gid} lacks promotion provenance")
                package = Path(str(generated.get("workspace_path", ""))).expanduser()
                if not package.is_dir() or not (package / "candidate.py").is_file() or not (package / "fixture.json").is_file():
                    raise ValueError(f"trusted package missing for {gid}")
                expected_hash = generated.get("package_hash")
                actual_hash = hashlib.sha256((package / "candidate.py").read_bytes() + (package / "fixture.json").read_bytes()).hexdigest()
                if not isinstance(expected_hash, str) or expected_hash != actual_hash:
                    raise ValueError(f"trusted package integrity mismatch for {gid}")
                capabilities = generated.get("capabilities")
                if not isinstance(capabilities, list) or not capabilities:
                    raise ValueError(f"trusted generated module {gid} has no capabilities")
                overlap = seen_caps.intersection(capabilities)
                if overlap:
                    raise ValueError(f"duplicate trusted capability: {sorted(overlap)[0]}")
                base_overlap = existing_caps.intersection(capabilities)
                if base_overlap:
                    raise ValueError(f"trusted capability conflicts with base registry: {sorted(base_overlap)[0]}")
                seen_caps.update(capabilities)
                registry["modules"] = [m for m in registry["modules"] if m.get("id") != generated["id"]]
                registry["modules"].append(generated)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            if isinstance(exc, ValueError):
                raise
            raise ValueError("trusted generated registry overlay is unreadable") from exc
    return registry


def list_modules() -> list[dict[str, Any]]:
    """Return registered modules without probing or invoking providers."""
    return [dict(module) for module in load_module_registry()["modules"]]


def get_module(module_id: str) -> dict[str, Any]:
    matches = [module for module in list_modules() if module["id"] == module_id]
    if not matches:
        raise ModuleUnavailable(f"Module {module_id!r} is not registered.")
    return matches[0]


def find_modules_by_capability(capability: str) -> list[dict[str, Any]]:
    if not isinstance(capability, str) or not capability.strip():
        raise ValueError("A non-empty module capability is required.")
    return [module for module in list_modules() if capability in module["capabilities"]]


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def _gstack_runtime_root() -> Path | None:
    codex_home = _codex_home()
    roots = [
        Path(os.environ["GSTACK_ROOT"]).expanduser() if os.environ.get("GSTACK_ROOT") else None,
        codex_home / "skills" / "gstack",
        WORKBENCH_ROOT / "gstack",
    ]
    for root in roots:
        if root is None:
            continue
        try:
            resolved = root.resolve(strict=True)
        except OSError:
            continue
        if (resolved / "bin" / "gstack-skill-start").is_file() and (resolved / "lib").is_dir():
            return resolved
    return None


def _gstack_skill_status() -> tuple[bool, list[str]]:
    codex_home = _codex_home()
    skill = codex_home / "skills" / "gstack-review" / "SKILL.md"
    evidence: list[str] = []
    if not skill.is_file():
        return False, [f"missing installed skill: {skill}"]
    content = skill.read_text(encoding="utf-8", errors="replace")
    name = _skill_frontmatter_name(content)
    if name != "review" or 'gstack-skill-start" --skill "review"' not in content:
        return False, [f"installed skill lacks the gstack review preamble: {skill}"]
    evidence.append(f"skill={skill.resolve()}")
    root = _gstack_runtime_root()
    if root is None:
        return False, evidence + ["gstack-skill-start and gstack lib directory not found"]
    evidence.append(f"runtime_root={root}")
    evidence.append(f"runner={root / 'bin' / 'gstack-skill-start'}")
    return True, evidence


def _bun_candidates() -> list[Path]:
    candidates: list[Path] = []
    bun_install = os.environ.get("BUN_INSTALL")
    if bun_install:
        candidates.append(Path(bun_install).expanduser() / "bin" / "bun")
    candidates.extend((Path.home() / ".bun" / "bin" / "bun", Path("/opt/homebrew/bin/bun"), Path("/usr/local/bin/bun")))
    discovered = shutil.which("bun", path=os.environ.get("PATH", ""))
    if discovered:
        candidates.insert(0, Path(discovered))
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        expanded = candidate.expanduser()
        key = str(expanded)
        if key not in seen:
            seen.add(key)
            unique.append(expanded)
    return unique


def _parse_bun_version(output: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?<![\w.])(\d+)\.(\d+)\.(\d+)(?![\w.])", output)
    return tuple(int(part) for part in match.groups()) if match else None


def _discover_bun() -> tuple[Path, tuple[int, int, int]]:
    for candidate in _bun_candidates():
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        executable = candidate.resolve()
        try:
            completed = subprocess.run(
                [str(executable), "--version"], capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        version = _parse_bun_version(completed.stdout.strip()) if completed.returncode == 0 else None
        if version is None:
            continue
        if version < MINIMUM_BUN_VERSION:
            raise ModuleUnavailable(
                f"Full gstack review requires Bun >= 1.3.3; found {'.'.join(map(str, version))} at {executable}."
            )
        return executable, version
    raise ModuleUnavailable("Full gstack review requires an installed Bun >= 1.3.3; no usable Bun executable was found.")


def _skill_frontmatter_name(content: str) -> str | None:
    frontmatter = re.match(r"\A---\s*\n(.*?)\n---(?:\s|$)", content, re.DOTALL)
    if frontmatter is None:
        return None
    match = re.search(r"^name:\s*([^\s#]+)\s*$", frontmatter.group(1), re.MULTILINE)
    return match.group(1) if match else None


def _install_probe(probe: dict[str, Any], workspace_root: Path | None = None) -> tuple[bool, str]:
    kind = probe.get("type")
    if kind == "workspace_path":
        path = ((workspace_root or WORKBENCH_ROOT) / str(probe.get("path", ""))).resolve()
        return path.exists(), f"path={path}"
    if kind == "workspace_executable":
        path = (workspace_root or WORKBENCH_ROOT) / str(probe.get("path", ""))
        available = path.is_file() and os.access(path, os.X_OK)
        return available, f"executable={path.resolve(strict=False) if available else path}"
    if kind == "codex_skill":
        path = _codex_home() / "skills" / str(probe.get("path", ""))
        return path.is_file(), f"path={path}"
    if kind == "gstack_review_skill":
        available, evidence = _gstack_skill_status()
        return available, "; ".join(evidence)
    if kind == "executable":
        executable = shutil.which(str(probe.get("name", "")))
        return executable is not None, f"executable={executable or probe.get('name')}"
    return False, f"unsupported install probe: {kind}"


def _runtime_probe(probe: dict[str, Any] | None, installed: bool, declared_state: str) -> tuple[str, str | None]:
    if not installed:
        return "not-installed", None
    if not probe:
        return declared_state, None
    if probe.get("type") != "http_get":
        return "unknown", "unsupported runtime probe"
    url = str(probe.get("url", ""))
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return "unknown", "runtime probe must target loopback HTTP"
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=0.5) as response:
            return ("active", f"HTTP {response.status} at {url}") if 200 <= response.status < 400 else ("unavailable", f"HTTP {response.status} at {url}")
    except (OSError, urllib.error.URLError, TimeoutError):
        return "inactive", f"no HTTP response at {url}"


def module_inventory() -> list[dict[str, Any]]:
    result = []
    for module in load_module_registry()["modules"]:
        registered_root = module.get("workspace_path")
        workspace_root = Path(registered_root).expanduser() if registered_root else WORKBENCH_ROOT
        installed, install_evidence = _install_probe(module["availability_probe"], workspace_root)
        runtime_probe = module.get("runtime_probe")
        managed_startable = False
        if module["id"] == "agent-desktop":
            try:
                from modules.agent_desktop_adapter import agent_desktop_health
                health = agent_desktop_health()
                installed = bool(health.get("installed"))
                runtime_state = "active" if health.get("status") == "READY" else "inactive"
                runtime_evidence = json.dumps(health.get("evidence", {}), ensure_ascii=False, separators=(",", ":"))
                module = {**module, "health": health, "status": health.get("status", "UNAVAILABLE")}
            except (OSError, RuntimeError, ValueError) as exc:
                runtime_state, runtime_evidence = "unavailable", f"agent-desktop health probe failed: {type(exc).__name__}"
        elif module["id"] == "rea":
            try:
                from modules.rea_adapter import rea_health
                health = rea_health()
                installed = Path(str(module.get("workspace_path") or WORKBENCH_ROOT / module["repo"])).is_dir() and (WORKBENCH_ROOT / module["repo"] / "dist" / "mcp" / "main.js").is_file()
                runtime_state = "active" if health.get("mcp_status") == "READY" else "inactive"
                runtime_evidence = json.dumps({
                    "mcp_status": health.get("mcp_status"), "tool_count": health.get("tool_count", 0),
                    "capabilities": health.get("capabilities", {}), "errors": health.get("errors", []),
                }, ensure_ascii=False, separators=(",", ":"))
                module = {**module, "health": health, "status": health.get("status", "UNAVAILABLE")}
            except (OSError, RuntimeError, ValueError) as exc:
                runtime_state, runtime_evidence = "unavailable", f"REA health probe failed: {type(exc).__name__}"
        elif module["id"] == "pedagogy":
            try:
                from modules.pedagogy_adapter import service_url
                from modules.pedagogy_runtime import inspect_runtime, is_startable
                if os.environ.get("SYSTEM_AGENTIC_PEDAGOGY_URL"):
                    runtime_probe = {**(runtime_probe or {}), "url": service_url() + "/api/status"}
                    runtime_state, runtime_evidence = _runtime_probe(runtime_probe, installed, module["declared_runtime_state"])
                else:
                    observed = inspect_runtime()
                    runtime_state = "active" if observed.get("health") == "healthy" else "inactive"
                    runtime_evidence = (
                        f"{observed.get('endpoint')} ({'managed' if observed.get('managed') else 'pre-existing service'})"
                        if runtime_state == "active" else "no healthy managed or default loopback Pedagogy service"
                    )
                    managed_startable, start_evidence = is_startable()
                    runtime_evidence += f"; managed_startable={managed_startable}"
                    if start_evidence:
                        runtime_evidence += f" ({start_evidence})"
            except ValueError:
                runtime_state, runtime_evidence = "unavailable", "configured Pedagogy URL is not a loopback HTTP origin"
            except (OSError, RuntimeError) as exc:
                runtime_state, runtime_evidence = "unavailable", f"Pedagogy runtime inspection failed: {type(exc).__name__}"
        elif runtime_probe:
            runtime_state, runtime_evidence = _runtime_probe(runtime_probe, installed, module["declared_runtime_state"])
        else:
            runtime_state, runtime_evidence = _runtime_probe(None, installed, module["declared_runtime_state"])
        available = runtime_state == "active" if module.get("runtime_probe") or module["id"] in {"rea", "agent-desktop"} else installed
        execution_ready = bool(
            module["enabled"] and module["adapter_status"] == "implemented"
            and (available or (module["id"] == "pedagogy" and managed_startable))
        )
        if module["id"] in {"rea", "agent-desktop"}:
            execution_ready = bool(module["enabled"] and available and module.get("adapter_status") == "implemented")
        result.append({
            **module,
            "installation_state": "installed" if installed else "not-installed",
            "available": available,
            "runtime_state": runtime_state,
            **({"managed_startable": managed_startable} if module["id"] == "pedagogy" else {}),
            "execution_ready": execution_ready,
            "state_evidence": [install_evidence] + ([runtime_evidence] if runtime_evidence else []),
        })
    return result


def generated_module_entry(capability: str, package_root: str, *, promotion_id: str, candidate_id: str,
                           blueprint_ref: str, package_hash: str) -> dict[str, Any]:
    """Build the trusted overlay entry used by the bounded promotion authority."""
    package = Path(package_root).resolve()
    entrypoint = package / "candidate.py"
    entrypoint_hash = hashlib.sha256(entrypoint.read_bytes()).hexdigest() if entrypoint.is_file() else None
    return {
        "schema_version": 1,
        "id": f"generated-{capability.replace('.', '-')}", "name": f"Promoted {capability}",
        "repo": str(Path(package_root).relative_to(WORKBENCH_ROOT)) if Path(package_root).is_relative_to(WORKBENCH_ROOT) else str(package_root),
        "workspace_path": str(Path(package_root).resolve()), "enabled": True, "available": None,
        "task_types": ["generated-capability"], "capabilities": [capability],
        "invocation_type": "promoted-local-runtime", "adapter_status": "implemented", "requires_provider": None,
        "verification": [str(Path(package_root) / "candidate.py")],
        "availability_probe": {"type": "workspace_path", "path": "candidate.py"},
        "declared_runtime_state": "promoted", "generated_origin": "promoted", "promotion_id": promotion_id,
        "candidate_id": candidate_id, "blueprint_ref": blueprint_ref, "package_hash": package_hash,
        "entrypoint": "candidate.py", "entrypoint_hash": entrypoint_hash,
        "execution_ready": True,
        "composition_contracts": {capability: {"context_match": {}, "inputs": {},
                              "outputs": {"summary": {"type": "application/json", "artifact_type": "table-summary"}}}},
    }


def optimization_layer_inventory() -> list[dict[str, Any]]:
    from optimization import optimization_inventory
    return optimization_inventory()


def optimize_module_result(result: ProviderResult) -> ProviderResult:
    module = result.execution_context.get("module")
    module_id = module.get("id") if isinstance(module, dict) else None
    if module_id not in {"gstack", "knowledge-builder"}:
        return result
    return optimize_provider_summary(result, content_type="terminal")


def execute_pedagogy_module(task: ProviderTask, capability: str) -> ModuleResult:
    """Call the official local rehearsal API; this path has no provider execution."""
    from modules.pedagogy_adapter import PedagogyUnavailable, execute

    try:
        data = execute(task.task_id, capability, task.context)
    except PedagogyUnavailable as exc:
        raise ModuleUnavailable(str(exc)) from exc
    return ModuleResult(
        task_id=task.task_id,
        module="pedagogy",
        execution_status=data["execution_status"],
        capability=capability,
        domain_outcome=data["domain_outcome"],
        result={key: value for key, value in data.items()
                if key not in {"task_id", "module", "execution_status", "capability", "domain_outcome"}},
    )


def module_candidates(*, task_type: str | None = None, capabilities: set[str] | None = None) -> list[dict[str, Any]]:
    requested = capabilities or set()
    if not task_type and not requested:
        raise ValueError("A task type or at least one capability is required.")
    return [
        module for module in module_inventory()
        if (task_type is None or task_type in module["task_types"])
        and requested.issubset(module["capabilities"])
    ]


def module_by_id(module_id: str) -> dict[str, Any]:
    for module in module_inventory():
        if module["id"] == module_id:
            return module
    raise ValueError(f"Unknown module: {module_id}")


def _is_gstack_review_scratch(task: ProviderTask) -> bool:
    declaration = task.context.get("gstack_review_workspace")
    if declaration != "synthetic-scratch-v1":
        return False
    try:
        workspace_path = task.workspace.expanduser()
        workspace = workspace_path.resolve(strict=True)
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
        marker = workspace / GSTACK_REVIEW_SCRATCH_MARKER
        state_root = workspace / ".gstack-review-state"
        gitdir = state_root / "git"
        git_pointer = workspace / ".git"
        return (
            workspace_path.is_absolute()
            and not workspace_path.is_symlink()
            and workspace_path.parent.resolve(strict=True) == temp_root
            and workspace.is_dir()
            and workspace.parent == temp_root
            and workspace.name.startswith("system-agentic-gstack-review-")
            and marker.is_file()
            and not marker.is_symlink()
            and marker.read_text(encoding="utf-8") == "system-agentic synthetic review workspace v1\n"
            and state_root.is_dir()
            and not state_root.is_symlink()
            and gitdir.is_dir()
            and not gitdir.is_symlink()
            and gitdir.resolve(strict=True) == workspace / ".gstack-review-state" / "git"
            and (gitdir / "HEAD").is_file()
            and not (gitdir / "HEAD").is_symlink()
            and git_pointer.is_file()
            and not git_pointer.is_symlink()
            and git_pointer.read_text(encoding="utf-8") == "gitdir: .gstack-review-state/git\n"
        )
    except (OSError, UnicodeError):
        return False


def _summary_json_line(summary: str, prefix: str, *, must_be_last: bool = False) -> dict[str, Any] | None:
    lines: list[tuple[str, str]] = []
    for line in summary.splitlines():
        if line.startswith(prefix):
            lines.append((line, line))
        elif line.startswith("`") and line.lstrip("`").startswith(prefix):
            # Accept one inline-code pair around the complete contract line.
            # Other backtick wrapping remains a malformed matching line.
            wrapped_once = not line.startswith("``") and line.endswith("`") and not line.endswith("``")
            lines.append((line, line[1:-1] if wrapped_once else ""))
    if len(lines) != 1 or (must_be_last and summary.splitlines()[-1:] != [lines[0][0]]):
        return None
    try:
        parsed = json.loads(lines[0][1].partition("=")[2])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _gate_scan_evidence(summary: str, workflow: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Validate the worker's normalized gate receipt against its scratch artifact."""
    if any(line.startswith("GATE_ERROR=") or line.startswith("GATE_ERROR ") for line in summary.splitlines()):
        return {"status": "FAILED"}, "gstack gate helper reported GATE_ERROR"

    raw = _summary_json_line(summary, "GATE_SUMMARY=")
    if raw is None:
        # Accept the helper's native summary for older workers, while still
        # requiring its machine-readable counters, fingerprint and artifact.
        native = next((line for line in summary.splitlines() if line.startswith("GATE_SUMMARY:")), None)
        if native is not None:
            try:
                fields = dict(token.split("=", 1) for token in shlex.split(native.partition(":")[2]) if "=" in token)
                candidate = fields.get("candidate", "")
                raw = {
                    **fields,
                    "fingerprint": candidate.rsplit("+", 1)[-1],
                    "artifact": fields.get("artifact"),
                }
                for key in GSTACK_GATE_COUNTS:
                    if key in fields:
                        raw[key] = int(fields[key])
            except (ValueError, TypeError):
                raw = None
    if raw is None:
        return {"status": "FAILED"}, "missing or invalid GATE_SUMMARY"

    values: dict[str, Any] = {}
    for key in GSTACK_GATE_COUNTS:
        value = raw.get(key)
        if type(value) is not int or value < 0:
            return {"status": "FAILED"}, f"invalid gate scan counter: {key}"
        values[key] = value
    fingerprint = raw.get("fingerprint")
    candidate = raw.get("candidate")
    artifact = raw.get("artifact")
    if not isinstance(fingerprint, str):
        return {"status": "FAILED", **values}, "missing or invalid working-tree fingerprint"
    if "+" in fingerprint:
        parts = fingerprint.split("+")
        if len(parts) != 2 or any(not GSTACK_FINGERPRINT_RE.fullmatch(part) for part in parts):
            return {"status": "FAILED", **values}, "missing or invalid working-tree fingerprint"
        composed_candidate, fingerprint = fingerprint, parts[1]
        if candidate is not None and candidate != composed_candidate:
            return {"status": "FAILED", **values}, "gate candidate does not match working-tree fingerprint"
        candidate = composed_candidate
    elif not GSTACK_FINGERPRINT_RE.fullmatch(fingerprint):
        return {"status": "FAILED", **values}, "missing or invalid working-tree fingerprint"
    if candidate is not None:
        if not isinstance(candidate, str):
            return {"status": "FAILED", **values}, "invalid gate candidate"
        candidate_parts = candidate.split("+")
        if len(candidate_parts) != 2 or any(not GSTACK_FINGERPRINT_RE.fullmatch(part) for part in candidate_parts):
            return {"status": "FAILED", **values}, "invalid gate candidate"
        if candidate_parts[1] != fingerprint:
            return {"status": "FAILED", **values}, "gate candidate does not match working-tree fingerprint"
    if not isinstance(artifact, str) or not artifact:
        return {"status": "FAILED", **values, "fingerprint": fingerprint}, "missing gate artifact path"

    workspace = workflow.get("workspace")
    try:
        root = Path(workspace).resolve(strict=True) if isinstance(workspace, str) else None
        artifact_path = Path(artifact)
        if not artifact_path.is_absolute():
            raise OSError("artifact path is not absolute")
        resolved_artifact = artifact_path.resolve(strict=True)
        state_root = (root / ".gstack-review-state").resolve(strict=True) if root else None
        if (root is None or state_root is None or artifact_path.is_symlink() or resolved_artifact.is_symlink()
                or not resolved_artifact.is_file() or state_root not in resolved_artifact.parents):
            raise OSError("artifact is not an existing file under the scratch state root")
        artifact_bytes = resolved_artifact.stat().st_size
    except (OSError, RuntimeError, TypeError, ValueError):
        return {"status": "FAILED", **values, "fingerprint": fingerprint, "artifact": artifact}, "missing or invalid gate artifact"

    reported_bytes = raw.get("artifact_bytes", artifact_bytes)
    if type(reported_bytes) is not int or reported_bytes != artifact_bytes:
        return {"status": "FAILED", **values, "fingerprint": fingerprint, "artifact": str(resolved_artifact), "artifact_bytes": artifact_bytes}, "gate artifact byte count does not match"
    if values["listed"] == values["eligible"] == values["unread"] == 0 and artifact_bytes != 0:
        return {"status": "FAILED", **values, "fingerprint": fingerprint, "artifact": str(resolved_artifact), "artifact_bytes": artifact_bytes}, "unexpected nonempty gate artifact for an empty scan"
    no_gate_edits = values["listed"] == values["eligible"] == values["unread"] == 0 and artifact_bytes == 0
    status = "NOT_APPLICABLE" if no_gate_edits else "WORKING"
    reported_status = raw.get("status")
    accepted_statuses = (None, status, "WORKING") if no_gate_edits else (None, status)
    if reported_status not in accepted_statuses:
        return {"status": "FAILED", **values, "fingerprint": fingerprint, "artifact": str(resolved_artifact), "artifact_bytes": artifact_bytes}, "gate scan status conflicts with its evidence"
    gate = {
        "status": status,
        **values,
        "fingerprint": fingerprint,
        "artifact": str(resolved_artifact),
        "artifact_bytes": artifact_bytes,
    }
    if candidate is not None:
        gate["candidate"] = candidate
    if values["unmatched"]:
        gate["coverage_note"] = f"{values['unmatched']} unmatched path(s); no recognized gate edits"
    return gate, None


def normalize_module_result(result: ProviderResult) -> ProviderResult:
    """Normalize module-specific outcomes without conflating them with execution health."""
    module = result.execution_context.get("module")
    if isinstance(module, dict) and module.get("id") == "knowledge-builder":
        return _normalize_knowledge_builder_result(result, module)
    workflow = module.get("review_workflow") if isinstance(module, dict) else None
    if not isinstance(workflow, dict):
        return result
    if result.status not in {"COMPLETED", "FAILED", "PARTIAL"}:
        return result

    summary = result.summary or ""
    phases = _summary_json_line(summary, "GSTACK_REVIEW_PHASES=") or {}
    final_result = _summary_json_line(summary, "GSTACK_REVIEW_RESULT=", must_be_last=True)

    missing = [name for name in GSTACK_REVIEW_REQUIRED_PHASES if name not in phases]
    invalid = [
        name for name, phase in phases.items()
        if name in GSTACK_REVIEW_REQUIRED_PHASES
        and (
            not isinstance(phase, dict)
            or phase.get("status") not in {"passed", "blocked", "failed"}
            or not isinstance(phase.get("evidence"), str)
            or not phase["evidence"].strip()
        )
    ]
    gate_scan, gate_error = _gate_scan_evidence(summary, workflow)
    failed = [name for name in GSTACK_REVIEW_REQUIRED_PHASES if isinstance(phases.get(name), dict) and phases[name].get("status") == "failed"]
    if gate_error:
        failed.append("gate_scan")
    blocked = [name for name in GSTACK_REVIEW_REQUIRED_PHASES if isinstance(phases.get(name), dict) and phases[name].get("status") == "blocked"]
    execution_status = "COMPLETED"
    if result.status == "FAILED" or result.errors or failed:
        execution_status = "FAILED"
    elif (
        result.status != "COMPLETED"
        or missing
        or invalid
        or blocked
        or any(phases[name]["status"] != "passed" for name in GSTACK_REVIEW_REQUIRED_PHASES if name in phases and name not in invalid)
        or workflow.get("mode") != "full"
        or result.execution_policy != "WORKSPACE_WRITE"
        or final_result is None
    ):
        execution_status = "PARTIAL"

    review_outcome = None
    critical_count = None
    acceptance_passed = None
    result_error = None
    if final_result is not None:
        review_outcome = final_result.get("review_outcome")
        critical_count = final_result.get("critical_count")
        acceptance_passed = final_result.get("acceptance_passed")
        if (review_outcome not in {"CLEAN", "ISSUES_FOUND", "INCONCLUSIVE"}
                or type(critical_count) is not int or critical_count < 0
                or type(acceptance_passed) is not bool
                or acceptance_passed != (review_outcome == "CLEAN")
                or (review_outcome == "CLEAN" and critical_count != 0)):
            result_error = "invalid GSTACK_REVIEW_RESULT"
            execution_status = "PARTIAL" if execution_status == "COMPLETED" else execution_status

    context = dict(result.execution_context)
    module_context = dict(module)
    workflow_context = dict(workflow)
    workflow_context.update({
        "outcome": execution_status,
        "execution_status": execution_status,
        "review_outcome": review_outcome if result_error is None else None,
        "critical_count": critical_count if result_error is None else None,
        "acceptance_passed": acceptance_passed if result_error is None else None,
        "phase_results": phases,
        "missing_phases": missing,
        "invalid_phases": invalid,
        "gate_scan": gate_scan,
    })
    module_context["review_workflow"] = workflow_context
    context["module"] = module_context
    errors = result.errors
    if execution_status != "COMPLETED" and not errors:
        errors = ({
            "code": "GSTACK_REVIEW_INCOMPLETE",
            "message": result_error or gate_error or "Full gstack review requires valid result and passed evidence for preflight, startup, gate scan, core review, exploratory QA, adversarial review, and review-log finish.",
        },)
    return replace(result, status=execution_status, execution_context=context, errors=errors)


def _knowledge_builder_executable() -> Path:
    executable = WORKBENCH_ROOT / "design-os-knowledge-builder/.venv/bin/knowledge-builder"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ModuleUnavailable(f"Knowledge Builder CLI is unavailable: {executable}")
    return executable.resolve(strict=True)


def _stage_knowledge_sources(task: ProviderTask, output_root: Path) -> tuple[ProviderTask, dict[str, dict[str, Any]]]:
    """Byte-preserving host staging keeps source transport out of model-generated text."""
    names = task.context.get("knowledge_sources")
    if not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names):
        raise ModuleUnavailable("Knowledge Builder needs explicit workspace-relative source paths.")
    workspace = task.workspace.expanduser().resolve(strict=True)
    evidence_root = output_root / "_evidence" / "run-001" / "sources"
    staged: list[str] = []
    manifest: dict[str, dict[str, Any]] = {}
    basenames: set[str] = set()
    incoming = task.context.get("composition_inputs")
    incoming_artifact = incoming[0] if isinstance(incoming, list) and len(incoming) == 1 and isinstance(incoming[0], dict) else {}
    incoming_metadata = incoming_artifact.get("metadata", {}) if isinstance(incoming_artifact, dict) else {}
    for raw in names:
        source = _workspace_artifact_path(workspace, raw)
        if source is None or not source.is_file():
            raise ModuleUnavailable(f"Knowledge Builder source is missing or outside the workspace: {raw}")
        if source.name in basenames:
            raise ModuleUnavailable("Knowledge Builder source filenames must be unique for evidence staging.")
        basenames.add(source.name)
        source_bytes = source.read_bytes()
        if len(source_bytes) > 8_000_000:
            raise ModuleUnavailable("Knowledge Builder source exceeds the bounded staging size.")
        target = evidence_root / source.name
        if target.exists():
            if not target.is_file() or target.is_symlink() or target.read_bytes() != source_bytes:
                raise ModuleUnavailable("Knowledge Builder evidence path already contains different data; refusing overwrite.")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source_bytes)
        relative = target.relative_to(workspace).as_posix()
        digest = hashlib.sha256(source_bytes).hexdigest()
        staged.append(relative)
        manifest[relative] = {
            "producer_module": incoming_artifact.get("producerModuleId", "pedagogy"),
            "producer_task_id": incoming_metadata.get("producer_task_id"),
            "producer_session_id": incoming_metadata.get("session_id"),
            "producer_artifact": incoming_metadata.get("path", str(source.relative_to(workspace))),
            "producer_path": str(source.relative_to(workspace)),
            "producer_bytes": len(source_bytes),
            "producer_sha256": digest,
            "consumer_module": "knowledge-builder",
            "consumer_binding": "knowledge_sources",
            "consumer_staged_path": relative,
            "consumer_path": relative,
            "consumer_bytes": target.stat().st_size,
            "consumer_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "transformation": "none",
        }
        if manifest[relative]["consumer_sha256"] != digest:
            raise ModuleUnavailable("Knowledge Builder byte-preserving staging hash mismatch; downstream blocked.")
    context = dict(task.context)
    context["knowledge_sources"] = staged
    context["knowledge_source_hashes"] = {Path(name).name: value["producer_sha256"] for name, value in manifest.items()}
    context["knowledge_source_staging"] = manifest
    return replace(task, context=context), manifest


def _knowledge_result_line(summary: str) -> dict[str, Any] | None:
    lines = [line for line in summary.splitlines() if line.startswith(KNOWLEDGE_BUILDER_RESULT_PREFIX)]
    if len(lines) != 1:
        return None
    try:
        payload = json.loads(lines[0].partition("=")[2])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _workspace_artifact_path(workspace: Path, raw: object) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        root = workspace.expanduser().resolve(strict=True)
        candidate = Path(raw).expanduser()
        candidate = candidate if candidate.is_absolute() else root / candidate
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        if resolved == root or not resolved.exists():
            return None
        probe = root
        for part in resolved.relative_to(root).parts:
            probe = probe / part
            if probe.is_symlink():
                return None
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None


def _workspace_output_path(workspace: Path, raw: object) -> Path | None:
    """Resolve a not-yet-created output location without allowing workspace escape."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        root = workspace.expanduser().resolve(strict=True)
        requested = Path(raw).expanduser()
        lexical = requested if requested.is_absolute() else root / requested
        resolved = lexical.resolve(strict=False)
        resolved.relative_to(root)
        if resolved == root:
            return None
        probe = root
        for part in resolved.relative_to(root).parts:
            probe = probe / part
            if probe.is_symlink():
                return None
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None


def _normalize_knowledge_builder_result(result: ProviderResult, module: dict[str, Any]) -> ProviderResult:
    """Keep compiler execution status separate from official UKMC validation outcome."""
    if result.status in {"SUBMITTED", "RUNNING", "UNKNOWN"}:
        return result
    workflow = module.get("knowledge_workflow")
    if not isinstance(workflow, dict):
        return result
    payload = _knowledge_result_line(result.summary or "")
    context = dict(result.execution_context)
    module_context = dict(module)
    workflow_context = dict(workflow)
    errors = result.errors
    artifacts: tuple[dict[str, Any], ...] = ()
    sources: list[dict[str, Any]] = []
    warnings: list[str] = []
    validation = workflow.get("validation")
    validation_status = validation.get("status") if isinstance(validation, dict) else "NOT_RUN"
    validation_evidence: dict[str, Any] = validation if isinstance(validation, dict) else {"status": "NOT_RUN"}
    execution_status = result.status
    reason: str | None = None

    if payload is None:
        execution_status = "PARTIAL" if result.status == "COMPLETED" else result.status
        reason = "missing or invalid KNOWLEDGE_BUILDER_RESULT contract line"
    else:
        declared_status = payload.get("execution_status")
        if declared_status == "SUCCESS":
            declared_status = "COMPLETED"
        declared_validation = payload.get("validation_status")
        sources_raw = payload.get("sources", [])
        artifacts_raw = payload.get("artifacts", [])
        # The provider contract historically allowed a relative artifact path
        # string. Normalize that legacy shape at the host boundary; do not let
        # harmless serialization variation turn a validated pack into a
        # semantic execution failure.
        if isinstance(artifacts_raw, list):
            artifacts_raw = [({"path": item} if isinstance(item, str) else item) for item in artifacts_raw]
        warnings_raw = payload.get("warnings", [])
        errors_raw = payload.get("errors", [])
        valid_shape = (
            declared_status in {"COMPLETED", "PARTIAL", "FAILED", "BLOCKED"}
            and declared_validation in {"VALID", "INVALID", "WARNING", "NOT_RUN"}
            and isinstance(sources_raw, list) and all(isinstance(item, dict) for item in sources_raw)
            and isinstance(artifacts_raw, list) and bool(artifacts_raw) and all(isinstance(item, dict) and isinstance(item.get("path"), str) for item in artifacts_raw)
            and isinstance(warnings_raw, list) and all(isinstance(item, str) for item in warnings_raw)
            and isinstance(errors_raw, list) and all(isinstance(item, (str, dict)) for item in errors_raw)
        )
        if not valid_shape:
            execution_status = "PARTIAL" if result.status == "COMPLETED" else result.status
            reason = "invalid knowledge result fields"
        else:
            sources = sources_raw
            warnings = warnings_raw
            workspace = Path(workflow.get("workspace", result.execution_context.get("workspace", Path.cwd())))
            output_root = _workspace_artifact_path(workspace, workflow.get("output_root"))
            if output_root is None or not output_root.is_dir():
                execution_status = "PARTIAL" if result.status == "COMPLETED" else result.status
                reason = "knowledge pack output root is missing or outside the task workspace"
                output_root = None
            resolved_artifacts = []
            for item in artifacts_raw:
                artifact_path = _workspace_artifact_path(workspace, item.get("path"))
                if artifact_path is None:
                    execution_status = "PARTIAL" if result.status == "COMPLETED" else result.status
                    reason = "artifact path is missing, unreadable, or outside the task workspace"
                    break
                try:
                    if output_root is None:
                        raise ValueError("missing output root")
                    artifact_path.relative_to(output_root)
                except ValueError:
                    execution_status = "PARTIAL" if result.status == "COMPLETED" else result.status
                    reason = "artifact path is outside the declared Knowledge Builder output root"
                    break
                resolved_artifacts.append({**item, "path": str(artifact_path)})
            artifacts = tuple(resolved_artifacts)
            validation_evidence = workflow.get("validation") if isinstance(workflow.get("validation"), dict) else validation_evidence
            if (
                output_root is not None
                and isinstance(validation_evidence, dict)
                and validation_evidence.get("index_present") is True
            ):
                index_path = output_root / "index.json"
                if index_path.is_file() and not index_path.is_symlink():
                    artifacts = (*artifacts, {"path": str(index_path.resolve(strict=True)), "type": "knowledge-index"})

            if output_root is None:
                pass
            elif validation_status not in {"VALID", "INVALID"}:
                execution_status = "PARTIAL" if result.status == "COMPLETED" else result.status
                reason = reason or "official Knowledge Builder validation evidence is missing"
            elif result.status == "COMPLETED" and reason is None:
                execution_status = "COMPLETED" if declared_status == "COMPLETED" else declared_status
            if declared_validation not in {validation_status, "WARNING", "NOT_RUN"}:
                warnings.append("worker_validation_status_disagreed_with_host_verify")

    workflow_context.update({
        "execution_status": execution_status,
        "validation_status": validation_status,
        "validation": validation_evidence,
        "sources": sources,
        "artifacts": list(artifacts),
        "warnings": warnings,
    })
    if reason:
        workflow_context["normalization_error"] = reason
        if not errors:
            errors = ({"code": "KNOWLEDGE_BUILDER_RESULT_INVALID", "message": reason},)
    module_context["knowledge_workflow"] = workflow_context
    context["module"] = module_context
    evidence = result.evidence + (({"type": "knowledge-builder-validation", **validation_evidence},) if validation_evidence else ())
    return replace(
        result,
        status=execution_status,
        artifacts=artifacts or result.artifacts,
        evidence=evidence,
        errors=errors,
        execution_context=context,
    )


def run_knowledge_builder_workflow(result: ProviderResult, *, include_index: bool = True) -> ProviderResult:
    """Run official host-side CLI checks after generation; status reconciliation never writes index."""
    module = result.execution_context.get("module")
    workflow = module.get("knowledge_workflow") if isinstance(module, dict) else None
    if not isinstance(workflow, dict) or result.status not in {"COMPLETED", "FAILED", "PARTIAL"}:
        return result
    if _knowledge_result_line(result.summary or "") is None:
        return replace(
            result,
            status="FAILED",
            errors=result.errors + ({"code": "RESULT_CONTRACT_MISSING", "message": "Worker completed without exactly one valid KNOWLEDGE_BUILDER_RESULT contract; validators were not run."},),
        )
    if result.status != "COMPLETED":
        return result
    workspace = Path(workflow.get("workspace", Path.cwd()))
    output_root = _workspace_artifact_path(workspace, workflow.get("output_root"))
    if output_root is None or not output_root.is_dir():
        validation = {"status": "NOT_RUN", "reason": "output root missing or outside task workspace"}
    else:
        try:
            cli = str(_knowledge_builder_executable())
            env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
            node_paths = sorted(
                path for path in output_root.rglob("*.md")
                if "_evidence" not in path.relative_to(output_root).parts
                and path.name.lower() != "readme.md"
                and not path.is_symlink()
            )
            commands = [[cli, "lint", str(path)] for path in node_paths]
            commands.append([cli, "verify", "--claims", str(output_root)])
            if include_index and workflow.get("write_index") is True:
                commands.append([cli, "index", "--root", str(output_root)])
            command_results = []
            for argv in commands:
                completed = subprocess.run(
                    argv, cwd=WORKBENCH_ROOT, capture_output=True, text=True, timeout=120,
                    check=False, env=env,
                )
                command_results.append({
                    "command": " ".join(argv[:3]) + (" <path>" if len(argv) > 3 else ""),
                    "exit_code": completed.returncode,
                    "output": (completed.stdout + completed.stderr)[-4000:],
                })
            passed = bool(node_paths) and all(item["exit_code"] == 0 for item in command_results)
            validation = {
                "status": "VALID" if passed else "INVALID",
                "commands": command_results,
                "node_count": len(node_paths),
                "output_root": str(output_root),
                "index_written": include_index and workflow.get("write_index") is True,
                "index_present": (output_root / "index.json").is_file() and not (output_root / "index.json").is_symlink(),
            }
        except (OSError, subprocess.SubprocessError, ModuleUnavailable) as exc:
            validation = {"status": "NOT_RUN", "reason": type(exc).__name__, "output_root": str(output_root)}

    context = dict(result.execution_context)
    module_context = dict(module)
    workflow_context = dict(workflow)
    workflow_context["validation"] = validation
    workflow_context["validation_status"] = validation["status"]
    module_context["knowledge_workflow"] = workflow_context
    context["module"] = module_context
    evidence = result.evidence + ({"type": "knowledge-builder-validation", **validation},)
    return replace(result, execution_context=context, evidence=evidence)


def prepare_module_task(task: ProviderTask, module_id: str, capability: str) -> ProviderTask:
    module = module_by_id(module_id)
    if capability not in module["capabilities"]:
        raise ValueError(f"Capability {capability!r} is not registered for module {module_id!r}.")
    if not module["enabled"]:
        raise ModuleUnavailable(f"Module {module_id!r} is disabled.")
    if module["adapter_status"] != "implemented":
        raise ModuleUnavailable(f"Module {module_id!r} is registered for discovery only; no execution adapter is implemented.")
    managed_startable = module_id == "pedagogy" and module.get("managed_startable") is True
    if module_id == "agent-desktop":
        from modules.agent_desktop_adapter import agent_desktop_health
        desktop_config = task.context.get("desktop", {})
        if isinstance(desktop_config, dict) and desktop_config.get("enabled", True) is False:
            raise ModuleUnavailable("agent-desktop is disabled by project configuration.")
        health = agent_desktop_health(desktop_config if isinstance(desktop_config, dict) else {})
        if health.get("status") != "READY":
            evidence = health.get("evidence", {}) if isinstance(health, dict) else {}
            raise ModuleUnavailable(f"agent-desktop is not execution-ready: {evidence.get('reason', health.get('status', 'UNKNOWN'))}")
    elif not module["execution_ready"] or (not module["available"] and not managed_startable):
        raise ModuleUnavailable(f"Module {module_id!r} is not currently execution-ready.")
    provider_requirement = module["requires_provider"]
    provider_id = provider_requirement.get("provider_id") if isinstance(provider_requirement, dict) else None
    allowed = task.provider_policy.get("allowed_providers")
    if provider_id and allowed is not None and provider_id not in allowed:
        raise ModuleUnavailable(f"Module {module_id!r} requires provider {provider_id!r}, excluded by task policy.")

    execution_policy = task.execution_policy
    review_workflow: dict[str, Any] | None = None
    constraints = task.constraints
    runtime_root: Path | None = None
    bun_executable: Path | None = None
    bun_version: tuple[int, int, int] | None = None
    if module_id == "gstack" and capability == "software.code-review":
        full_requested = task.context.get("gstack_review_mode") == "full"
        scratch_verified = _is_gstack_review_scratch(task)
        if task.execution_policy == "WORKSPACE_WRITE" and not (full_requested and scratch_verified):
            raise ModuleUnavailable("WORKSPACE_WRITE for gstack review requires an attested synthetic scratch workspace; arbitrary project workspaces remain READ_ONLY.")
        if full_requested and not scratch_verified:
            raise ModuleUnavailable("Full gstack review requires a dedicated synthetic scratch workspace with the workbench marker; refusing an unsafe write scope.")
        if full_requested and task.execution_policy != "WORKSPACE_WRITE":
            raise ModuleUnavailable("Full gstack review requires explicit WORKSPACE_WRITE on the attested synthetic scratch workspace; refusing a partial run disguised as full.")
        if full_requested:
            runtime_root = _gstack_runtime_root()
            if runtime_root is None:
                raise ModuleUnavailable("Full gstack review requires the installed gstack runtime root; refusing to enqueue without it.")
            bun_executable, bun_version = _discover_bun()
        mode = "full" if full_requested and scratch_verified and task.execution_policy == "WORKSPACE_WRITE" else "read-only-partial"
        review_workflow = {
            "id": "gstack-review-v1",
            "mode": mode,
            "required_phases": list(GSTACK_REVIEW_REQUIRED_PHASES),
            "write_boundary": (
                "WORKSPACE_WRITE is confined to .gstack-review-state/ and .tmp/ in this attested synthetic scratch workspace; Git metadata writes (including fetch/FETCH_HEAD) go only under .gstack-review-state/git/ via GIT_DIR; gstack-wtree fingerprint objects go under the separate .gstack-review-state/wtree-objects/ directory; .git is a read-only pointer file; source/fixture content is report-only and unchanged; no remote writes or writes outside these directories"
                if mode == "full" else "READ_ONLY; full workflow unavailable because scratch-scoped writes were not explicitly authorized"
            ),
            "report_only": True,
        }
        if mode == "full":
            assert runtime_root is not None and bun_executable is not None and bun_version is not None
            environment = (
                "GSTACK_SESSION_KIND=spawned "
                f"GSTACK_ROOT={shlex.quote(str(runtime_root))} "
                f"BUN_EXE={shlex.quote(str(bun_executable))} "
                f"PATH={shlex.quote(str(bun_executable.parent))}:\"$PATH\" "
                'GIT_DIR="$PWD/.gstack-review-state/git" GIT_WORK_TREE="$PWD" '
                'GSTACK_STATE_ROOT="$PWD/.gstack-review-state" '
                'GSTACK_HOME="$PWD/.gstack-review-state" TMPDIR="$PWD/.tmp" '
                'GSTACK_WTREE_OBJECT_DIR="$PWD/.gstack-review-state/wtree-objects"'
            )
            review_workflow.update({
                "workspace": str(task.workspace.resolve()),
                "gstack_root": str(runtime_root),
                "bun_executable": str(bun_executable),
                "bun_bin_directory": str(bun_executable.parent),
                "bun_version": ".".join(map(str, bun_version)),
                "command_environment": environment,
            })
        constraints = (*constraints,
            "Report-only review: do not edit, create, delete, or reformat fixture/source files; do not apply or request fixes.",
            "A full gstack review execution is successful only after preflight, skill startup, gate scan, core review, exploratory QA, adversarial review, and successful review-log --finish; report evidence for each phase. Execution status records whether commands/bookkeeping ran successfully. QA/review findings belong only in the final review outcome: a completed QA probe that exposes a product defect is a passed phase, and review-log --finish successfully writing completed=false due findings is also a passed phase. Findings never trigger retries.",
            "Emit all contract lines as bare text without Markdown fences or inline backticks. Return one final line GSTACK_REVIEW_PHASES=<JSON object> with each required phase mapped to {\"status\":\"passed|blocked|failed\",\"evidence\":\"command and observable result\"}. Status describes phase execution only, never whether the reviewed product is defective. Never mark a phase passed without command/result evidence.",
            "After the gate helper succeeds, emit one machine-readable line GATE_SUMMARY=<JSON object> with status (WORKING or NOT_APPLICABLE), listed, eligible, inspected, unread, unmatched (nonnegative integers), candidate (the full <merge-base-short>+<working-tree-fingerprint-short> value copied unchanged from gstack-gate-diff using gstack-wtree), fingerprint (the final hex suffix of candidate, separately when possible; never fabricate or substitute either value), artifact (the existing absolute JSONL artifact path), and artifact_bytes (actual file size). Preserve any GATE_ERROR; never emit a successful summary when the helper reports it. Prefer NOT_APPLICABLE when the artifact is empty and listed, eligible, unread, and artifact_bytes are all zero; retain unmatched count as a coverage note.",
            "Return one final line GSTACK_REVIEW_RESULT=<JSON object> with review_outcome (CLEAN, ISSUES_FOUND, or INCONCLUSIVE), critical_count (nonnegative integer), and acceptance_passed (true only for CLEAN). Report actual findings here; findings do not make execution fail. Do not add retries for findings.",
            (
                f"For full scratch review, WORKSPACE_WRITE is confined to .gstack-review-state/ and .tmp/ in this attested synthetic scratch workspace. The .git file is a read-only pointer to .gstack-review-state/git; before the gate scan, create .gstack-review-state/wtree-objects/ under the scratch state root. Prefix every gstack command and every command that invokes Git, including startup, fetch, diff, gates, QA, and review bookkeeping, with exactly {review_workflow['command_environment']}. This sets GSTACK_WTREE_OBJECT_DIR for gstack-wtree and the gate scan; keep its private fingerprint objects in that directory, separate from .gstack-review-state/git. Do not omit or alter any environment assignment. Git metadata and gstack state stay under .gstack-review-state/, fingerprint objects stay under .gstack-review-state/wtree-objects/, and temporary writes stay under .tmp/. Do not push, alter remote state, write source/fixture content, or write elsewhere; startup skips interactive sync/update flows."
                if mode == "full" else
                "For full scratch review, preflight creates .gstack-review-state/, .gstack-review-state/wtree-objects/, and .tmp/ in this workspace; prefix every gstack command with GSTACK_SESSION_KIND=spawned GSTACK_STATE_ROOT=\"$PWD/.gstack-review-state\" GSTACK_HOME=\"$PWD/.gstack-review-state\" TMPDIR=\"$PWD/.tmp\" GSTACK_WTREE_OBJECT_DIR=\"$PWD/.gstack-review-state/wtree-objects\" so state, private fingerprint objects, and temporary writes stay inside this workspace and the fingerprint directory remains separate from Git metadata under .gstack-review-state/git/. Startup skips interactive sync/update flows."
            ),
        )
        if mode != "full":
            constraints += ("The adapter will classify this run as PARTIAL unless the full scratch write scope is available; do not claim full gstack completion.",)
            execution_policy = "READ_ONLY"

    knowledge_workflow: dict[str, Any] | None = None
    if module_id == "knowledge-builder":
        if capability not in {"knowledge.pack-compile", "knowledge.source-governance", "knowledge.validation"}:
            raise ValueError(f"Unsupported Knowledge Builder capability: {capability}")
        if capability == "knowledge.pack-compile" and execution_policy != "WORKSPACE_WRITE":
            raise ModuleUnavailable("Knowledge pack compilation requires WORKSPACE_WRITE inside the declared workspace.")
        if capability != "knowledge.pack-compile" and execution_policy != "READ_ONLY":
            raise ModuleUnavailable("Knowledge source governance and validation require READ_ONLY execution.")
        output_root = task.context.get("knowledge_output") or f"artifacts/knowledge-packs/{task.task_id}"
        if capability == "knowledge.pack-compile":
            resolved_output = _workspace_output_path(task.workspace, output_root)
            if resolved_output is None:
                raise ModuleUnavailable("Knowledge Builder output must be a new path contained in the declared workspace.")
            if resolved_output.exists():
                existing = list(resolved_output.iterdir()) if resolved_output.is_dir() else []
                host_evidence_only = (
                    resolved_output.is_dir()
                    and len(existing) == 1
                    and existing[0].name == "_evidence"
                    and not existing[0].is_symlink()
                )
                if not host_evidence_only:
                    raise ModuleUnavailable("Knowledge Builder output must be a new path or a host-staged evidence-only directory; refusing overwrite.")
        else:
            resolved_output = _workspace_artifact_path(task.workspace, output_root)
            if resolved_output is None or not resolved_output.is_dir():
                raise ModuleUnavailable("Knowledge validation requires an existing pack directory inside the declared workspace.")
        knowledge_workflow = {
            "id": "knowledge-builder-ukmc-v1",
            "capability": capability,
            "workflow": (
                ["preserve_source_evidence", "author_ukmc_nodes", "lint", "verify_claims", "index"]
                if capability == "knowledge.pack-compile" else ["lint", "verify_claims"]
            ),
            "write_index": capability == "knowledge.pack-compile",
            "output_root": str(resolved_output or (task.workspace.expanduser().resolve(strict=False) / output_root)),
            "output_relative": resolved_output.relative_to(task.workspace.expanduser().resolve(strict=True)).as_posix(),
            "workspace": str(task.workspace.expanduser().resolve(strict=True)),
            "execution_policy": execution_policy,
            "sandbox_claim": "provider-enforced workspace policy; no path-level OS sandbox is claimed",
            "validation_contract": "execution_status is separate from validation_status; validator rejection is an artifact outcome, not an execution crash",
        }
        sources = task.context.get("knowledge_sources")
        staging = task.context.get("knowledge_source_staging", {})
        source_note = (
            "Input source paths (relative to workspace): " + json.dumps(sources, ensure_ascii=False)
            if isinstance(sources, list) and all(isinstance(item, str) for item in sources)
            else "Use only local source paths explicitly named by the user; if none are available, stop and report BLOCKED."
        )
        if capability == "knowledge.pack-compile":
            constraints = (*constraints,
                f"Create an actual legacy UKMC knowledge pack under relative path {knowledge_workflow['output_relative']!r}. The output directory contains only the host-staged source evidence; preserve it and add your node there. The worker file tools require paths relative to the workspace; never use absolute paths. {source_note}",
                "The host has already byte-preserving staged every source at <output>/_evidence/run-001/sources/<filename>. Do not recreate, rewrite, or copy source evidence. Verify the staged files and their supplied SHA-256 values; stop if any mismatch. Preserve original inputs unchanged. Do not run network access or crawl URLs. The official Knowledge Builder CLI does not ingest Markdown; do not claim Markdown was passed to `ingest`.",
                "Write one useful UKMC Markdown node at <output>/<id>.md with frontmatter id/title/domain/description/when/trust_tier/version/source_attribution and quarantine.is_external_untrusted: true. Set trust_tier: draft. Every source_attribution URI is relative to the pack root and every sha256 must be the exact source copy hash (obtain it with `shasum -a 256`). Include the four exact required sections: Purpose; When to Use / When NOT; Core Knowledge Content; Failure Modes (Mandatory). Cite every source fact with an ease:source anchor containing its exact copied-source URI, hash, capture timestamp, and quote copied verbatim from that source.",
                "The knowledge filename stem must exactly match the UKMC frontmatter `id`; the official `index` command rejects mismatches. For example, id `release-policy` must be written as `release-policy.md`.",
                'Follow the official UKMC grammar: `when` is a non-empty YAML list. Source anchors use whitespace-separated attributes, for example <!-- ease:source uri="_evidence/run-001/sources/source.md" sha256="<exact hash>" quote="<verbatim source quote>" -->, never JSON. Every Core Knowledge Content prose block needs its own source anchor; explicitly derived recommendations start with `Derived:` as permitted by the official contract. Preserve source limitations and draft/untrusted metadata.',
                "Use the declared create_knowledge_node tool exactly once with output_dir equal to the declared output path, node_id `pedagogy-session-reference`, and the first staged source path. This bounded helper writes the UKMC node with the exact staged-source hash; do not use read_file first, do not use write_file for the node, and do not write evidence.",
                "The host adapter runs the official installed Knowledge Builder CLI after your task: `lint` on each node, `verify --claims` on the pack, then `index --root`. Do not try to execute that CLI inside the worker sandbox. A validator rejection is an INVALID artifact, not an execution failure.",
                "Completion is bounded: first create the evidence-backed node and inspect the expected output. Once the required files exist, do not repeat completed writes. Your final response must end with exactly one bare KNOWLEDGE_BUILDER_RESULT=<JSON> line; this is mandatory for completion, even when the artifact is invalid or incomplete. If the expected artifact exists, inspect it and emit the result contract instead of rewriting it.",
            )
        else:
            constraints = (*constraints,
                f"Validate the existing UKMC pack at relative path {knowledge_workflow['output_relative']!r}. Do not change any pack files. The host adapter runs the official installed Knowledge Builder CLI `lint` and `verify --claims` after your read-only task.",
            )
        constraints = (*constraints,
            "Return one final bare machine-readable line KNOWLEDGE_BUILDER_RESULT=<JSON> with execution_status, validation_status (use NOT_RUN because the host runs validators), sources [{uri, sha256, trust_tier, provenance}], artifacts [{path}], warnings, and errors. Use workspace-relative paths only. Do not claim validation passed.",
        )
        data_classification = task.context.get("data_classification", "unspecified")
        provider_requirements = ["task.submit", "structured_artifact_generation", "knowledge_builder_contract_compliance", "workspace.write"]
        execution_metadata = dict(task.execution_context)
        execution_metadata["data_classification"] = data_classification
        execution_metadata["provider_requirements"] = provider_requirements
        execution_metadata["knowledge_source_staging"] = staging
        task = replace(task, required_capabilities=tuple(provider_requirements), execution_context=execution_metadata)
        if task.provider_policy.get("allowed_providers") is None:
            policy = dict(task.provider_policy)
            policy["allowed_providers"] = ["local-qwen"]
            task = replace(task, provider_policy=policy)

    pedagogy_workflow: dict[str, Any] | None = None
    if module_id == "pedagogy":
        from modules.pedagogy_adapter import CAPABILITY_ACTIONS

        request = task.context.get("pedagogy_request")
        action = request.get("action") if isinstance(request, dict) else None
        if capability not in CAPABILITY_ACTIONS or action not in CAPABILITY_ACTIONS[capability]:
            raise ValueError(f"Unsupported Pedagogy capability/action: {capability!r}/{action!r}.")
        writes_session = action in {"create", "advance"}
        required_policy = "WORKSPACE_WRITE" if writes_session else "READ_ONLY"
        if task.execution_policy != required_policy:
            raise ModuleUnavailable(f"Pedagogy action {action!r} requires explicit {required_policy}.")
        pedagogy_workflow = {
            "id": "design-os-pedagogy-rehearsal-api-v1",
            "capability": capability,
            "action": action,
            "execution_policy": required_policy,
            "service_url": os.environ.get("SYSTEM_AGENTIC_PEDAGOGY_URL", "http://127.0.0.1:8876").rstrip("/"),
            "provider_required": False,
            "semantic_evaluation": "NOT_APPLICABLE",
            "storage_boundary": (
                "Managed service uses the explicit SYSTEM_AGENTIC_AI runtime state root via official --data-dir; a healthy pre-existing/configured service is reused without claiming its database path is confined"
            ),
        }

    rea_workflow: dict[str, Any] | None = None
    if module_id == "rea":
        if capability not in {
            "REVERSE_ENGINEERING", "reverse_engineering", "reverse-engineering", "rea",
            "binary_analysis", "application_analysis", "runtime_analysis", "evidence.collection",
        }:
            raise ValueError(f"Unsupported REA capability: {capability}")
        if execution_policy != "READ_ONLY":
            raise ModuleUnavailable("REA analysis is read-only and requires explicit READ_ONLY execution.")
        target = task.context.get("target_path") or task.context.get("target") or task.context.get("artifact")
        if not isinstance(target, str) or not target.strip():
            raise ModuleUnavailable("REA dispatch requires context.target_path (a local authorized artifact).")
        if task.context.get("target_authorized") is not True:
            raise ModuleUnavailable("REA dispatch requires explicit context.target_authorized=true; the router cannot grant authorization.")
        rea_workflow = {
            "id": "rea-mcp-read-only-v1", "capability": "REVERSE_ENGINEERING", "target_path": target,
            "source": "https://github.com/morluto/rea.git", "version": "6.8.0",
            "commit": "cd68181c90792775ef4b3fd4910982084f79c285", "transport": "stdio",
            "safety": "local authorized artifacts only; no target execution, injection, protection bypass, or mutation",
        }
        requirements = tuple(dict.fromkeys((*task.required_capabilities, "REVERSE_ENGINEERING")))
        task = replace(task, required_capabilities=requirements)

    metadata = {
        "id": module["id"],
        "capability": capability,
        "invocation_type": module["invocation_type"],
    }
    if module_id == "gstack":
        metadata.update({"skill_id": "review", "invocation": {"type": "codex_skill", "skill_id": "review", "command": "/review"}})
    if review_workflow is not None:
        metadata["review_workflow"] = review_workflow
    if knowledge_workflow is not None:
        metadata["knowledge_workflow"] = knowledge_workflow
    if pedagogy_workflow is not None:
        metadata["pedagogy_workflow"] = pedagogy_workflow
    if rea_workflow is not None:
        metadata["rea_workflow"] = rea_workflow
    if module_id == "agent-desktop":
        metadata.update({
            "desktop_workflow": {
                "id": "agent-desktop-mcp-v1",
                "capability": capability,
                "transport": "stdio",
                "version": "1.2.0",
                "protocol": "2024-11-05",
                "observation_contract": "snapshot -> qualified ref action -> snapshot/assertion",
                "action_authorization": "WORKSPACE_WRITE plus context.desktop.action_authorization=explicit-task",
            }
        })
    context = dict(task.context)
    context["module"] = metadata
    execution_context = dict(task.execution_context)
    execution_context["module"] = metadata
    policy = dict(task.provider_policy)
    if provider_id:
        policy["allowed_providers"] = [provider_id]
    if module_id == "gstack":
        objective = "Use the installed /review Codex skill and follow its current review workflow.\n\nUser review request:\n" + task.objective.strip()
    elif module_id == "knowledge-builder":
        objective = "Use the registered Knowledge Builder module and its official local CLI workflow to create or validate the requested knowledge artifact.\n\nUser request:\n" + task.objective.strip()
    elif module_id == "pedagogy":
        objective = "Use the official local Pedagogy rehearsal service for the explicit requested session operation.\n\nUser request:\n" + task.objective.strip()
    elif module_id == "rea":
        objective = "Use the local REA MCP read-only adapter to collect reverse-engineering evidence.\n\nUser request:\n" + task.objective.strip()
    else:
        objective = task.objective.strip()
    return replace(task, objective=objective, context=context, execution_context=execution_context, provider_policy=policy, constraints=constraints, execution_policy=execution_policy)
