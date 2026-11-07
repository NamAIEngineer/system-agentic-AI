"""Advisory task routing through Quyet Tiny, opt-in TypeSafe JEV, then host."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from modules.runtime import module_candidates
from providers.runtime import WORKBENCH_ROOT


QUYET_THRESHOLDS_DEFAULT = {"route": 0.65, "module": 0.80, "execution_class": 0.80}
JEV_THRESHOLDS_DEFAULT = {"route": 0.65, "module": 0.65, "execution_class": 0.65}
DECISION_CHOICES = {
    "route": {"DIRECT", "DISPATCH"},
    "module": {"none", "gstack", "system-one", "orchestration", "knowledge-builder", "pedagogy", "rea", "agent-desktop"},
    "execution_class": {"READ_ONLY", "WORKSPACE_WRITE", "ELEVATED"},
}
MODULE_VALIDATION_TARGETS = {
    "agent-desktop": ("native-desktop-automation", "desktop.automation"),
    "gstack": ("software.code-review", "software.code-review"),
    "system-one": ("structured-decision", "structured.decisions"),
    "orchestration": ("agent-orchestration", "agent.dispatch-design"),
    "knowledge-builder": ("knowledge-pack", "knowledge.pack-compile"),
    "pedagogy": ("teaching-rehearsal", "pedagogy.rehearse"),
    "rea": ("reverse-engineering", "REVERSE_ENGINEERING"),
}
REVERSE_ENGINEERING_INTENT = re.compile(
    r"\b(?:reverse[ -]?engineer|reverse engineering|binary analysis|application analysis|runtime behavior|runtime analysis|network capture|packet capture|electron|apk|android package|native app|firmware|assembly|decompile|disassembl|functions?\s+(?:exist\s+)?(?:in|of)\s+(?:this\s+)?(?:binary|executable)|inspect (?:this|the) (?:app|binary|executable|artifact)|analy[sz]e (?:this|the) (?:app|application|binary|executable|apk|artifact)|phân tích (?:app|ứng dụng|binary|file chạy|file|apk|executable)|ngược mã|hành vi runtime|phân tích runtime)\b|"
    r"(?:analy[sz]e|inspect|reverse[ -]?engineer|understand|trace|map|decompile)\b.{0,60}(?:web(?:site| app| application| bundle)?|managed assembly|\.net(?:\s+assembly)?)\b",
    re.IGNORECASE,
)
COMPUTER_USE_INTENT = re.compile(
    r"\b(?:click|type\s+(?:this|that|the|a|an|text|value|url|into)|open|navigate|"
    r"screenshot|browser|chrome|safari|firefox|orca|computer use|nút)\b",
    re.IGNORECASE,
)
DESKTOP_INTENT = re.compile(
    r"\b(?:native desktop|desktop automation|desktop app|native app|accessibility tree|os window|menu bar|file dialog|ứng dụng native|cửa sổ desktop|giao diện native|thao tác native)\b",
    re.IGNORECASE,
)
BROWSER_INTENT = re.compile(
    r"\b(?:website|web site|dom|web form|trình duyệt|trang web)\b",
    re.IGNORECASE,
)
REVIEW_INTENT = re.compile(
    r"\b(?:review|pull request|PR|diff|pre-landing|patch|rà soát mã|review mã)\b",
    re.IGNORECASE,
)
CODING_INTENT = re.compile(
    r"\b(?:fix|implement|build|refactor|debug|typescript|python|rust|react|code|bug|compile|"
    r"endpoint|middleware|migration|add|update|optimize|test|tests|parser|css|sql|authentication|layout)\b",
    re.IGNORECASE,
)
UNDERSPECIFIED_ROUTE = re.compile(
    r"\b(?:take care of|handle (?:this|it|the project)|make (?:this|it) better|"
    r"do whatever|if necessary|whichever makes sense|should we change it)\b|"
    r"giúp tôi xử lý(?: việc này)?",
    re.IGNORECASE,
)
EXPLICIT_ELEVATED = re.compile(
    r"\b(?:deploy|release)\b.{0,60}\b(?:production|prod|live)\b|"
    r"\b(?:push|modify|update|change)\b.{0,60}\b(?:production|prod|live)\b|"
    r"\b(?:publish|push)\b.{0,60}\b(?:public|registry|marketplace)\b|"
    r"\b(?:send|email|message)\b.{0,60}\b(?:customer|client|external|team)\b|"
    r"\b(?:delete|wipe|remove)\b.{0,60}\b(?:production|real|customer|records|database|data)\b|"
    r"\b(?:pay|purchase|buy|transfer)\b.{0,60}\b(?:payment|money|vendor|account)\b|"
    r"\b(?:change|modify|update|grant|revoke)\b.{0,60}\b(?:global|machine-wide|system-wide|security policy|credentials)\b|"
    r"(?:triển khai|phát hành)\b.{0,60}\b(?:production|prod|thật)\b|"
    r"(?:xóa|xoá)\b.{0,60}\b(?:dữ liệu thật|dữ liệu khách hàng|production)\b|"
    r"(?:gửi email|gửi tin nhắn)\b.{0,60}\b(?:khách hàng|đội ngũ bên ngoài|đối tác)\b",
    re.IGNORECASE,
)


def _threshold(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number between 0 and 1") from exc
    if not 0 <= value <= 1:
        raise ValueError(f"{name} must be a number between 0 and 1")
    return value


def _thresholds(engine: str, defaults: dict[str, float]) -> dict[str, float]:
    base_name = f"SYSTEM_AGENTIC_DECISION_{engine}_THRESHOLD"
    base = _threshold(base_name, min(defaults.values())) if base_name in os.environ else None
    return {
        field: _threshold(
            f"SYSTEM_AGENTIC_DECISION_{engine}_{field.upper()}_THRESHOLD",
            defaults[field] if base is None else base,
        )
        for field in defaults
    }


def _quyet_decide(
    prompt: str,
    module_options: list[str] | None = None,
    execution_class_options: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"task": prompt}
    if module_options is not None:
        payload["module_options"] = module_options
    if execution_class_options is not None:
        payload["execution_class_options"] = execution_class_options
    request = urllib.request.Request(
        "http://127.0.0.1:8766/decision",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if not isinstance(result, dict) or result.get("advisory") is not True:
        raise ValueError("Quyet returned an invalid advisory response")
    if isinstance(result.get("inference"), dict) and result["inference"].get("valid") is False:
        raise ValueError("Quyet returned invalid uniform probabilities after CPU fallback")
    answers = result.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("Quyet response omitted answers")
    return {
        "engine": "quyet-tiny",
        "answers": answers,
        "model": result.get("model"),
        "inference": result.get("inference"),
    }


def _jev_decide(prompt: str, decision_types: list[str]) -> dict[str, Any]:
    script = WORKBENCH_ROOT / "scripts/decision-plane-jev.mjs"
    result = subprocess.run(
        ["node", str(script)],
        input=json.dumps({"prompt": prompt, "decision_types": decision_types}, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=60,
        cwd=WORKBENCH_ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"TypeSafe JEV adapter failed ({result.returncode})")
    payload = json.loads(result.stdout)
    answers = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers, dict):
        raise ValueError("TypeSafe JEV response omitted answers")
    return {"engine": "typesafe-jev", "answers": answers, "model": payload.get("model")}


def _validate_answers(
    raw: object, thresholds: dict[str, float], fields: set[str]
) -> tuple[dict[str, Any], dict[str, str]]:
    if not isinstance(raw, dict):
        return {}, {field: "invalid_answers" for field in fields}
    choices: dict[str, Any] = {}
    confidence: dict[str, float | None] = {}
    reasons: dict[str, str] = {}
    for name in fields:
        allowed = DECISION_CHOICES[name]
        answer = raw.get(name)
        if not isinstance(answer, dict) or answer.get("choice") not in allowed:
            reasons[name] = f"invalid_{name}"
            continue
        value = answer.get("confidence")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1):
            reasons[name] = f"invalid_{name}_confidence"
            continue
        choices[name] = answer["choice"]
        confidence[name] = float(value) if value is not None else None
        if value is None:
            reasons[name] = f"missing_{name}_confidence"
        elif value < thresholds[name]:
            reasons[name] = f"{name}_below_threshold"
    return {"choices": choices, "confidence": confidence}, reasons


class DecisionPlane:
    """Collects advisory choices; never authorizes execution or dispatch."""

    def __init__(
        self,
        *,
        quyet: Callable[..., dict[str, Any]] = _quyet_decide,
        jev: Callable[[str, list[str]], dict[str, Any]] = _jev_decide,
        modules: Callable[..., list[dict[str, Any]]] = module_candidates,
    ) -> None:
        self.quyet = quyet
        self.jev = jev
        self.modules = modules

    def _module_options(self, prompt: str) -> tuple[list[str], dict[str, str] | None]:
        """Use task intent to query only relevant, live executable registry candidates."""
        text = prompt.casefold()
        if REVERSE_ENGINEERING_INTENT.search(prompt):
            # REA discovery performs a real, disposable MCP handshake.  A
            # cold Node/MCP start can miss the bounded health window once;
            # retry the same read-only inventory probe once so routing does
            # not become nondeterministic while still refusing an unavailable
            # provider after the retry budget is exhausted.
            candidates = None
            try:
                for _attempt in range(2):
                    candidates = self.modules(task_type="reverse-engineering", capabilities={"REVERSE_ENGINEERING"})
                    if any(item.get("id") == "rea" and item.get("execution_ready") is True for item in candidates):
                        break
            except Exception:
                return ["none"], {"task_type": "reverse-engineering", "status": "registry_unavailable"}
            ready = any(item.get("id") == "rea" and item.get("execution_ready") is True for item in (candidates or []))
            return (["none", "rea"] if ready else ["none"], {
                "task_type": "reverse-engineering",
                "status": "shortlisted" if ready else "no_execution_ready_candidate",
            })
        routes = (
            (("native desktop", "desktop automation", "desktop app", "native app", "accessibility tree", "file dialog", "ứng dụng native", "cửa sổ desktop"), "native-desktop-automation", "desktop.automation"),
            (("code review", "review code", "review this code", "review the code", "review this patch", "review this diff", "pull request", "pre-landing", "sql injection", "security bugs in code", "security issues in this code", "rà soát mã", "review mã", "kiểm tra mã nguồn", "rà soát diff"), "software.code-review", "software.code-review"),
            (("knowledge pack", "compile approved sources", "compile these source documents", "validated knowledge pack", "validate knowledge sources", "knowledge compilation", "biên soạn gói tri thức", "biến các nguồn này thành knowledge pack", "tạo gói tri thức", "kiểm định nguồn tri thức"), "knowledge-pack", "knowledge.pack-compile"),
            (("teaching rehearsal", "practice teaching", "fraction teaching", "fraction lesson rehearsal", "diễn tập giảng dạy", "luyện dạy phân số", "luyện tập dạy phân số"), "teaching-rehearsal", "pedagogy.rehearse"),
            (("structured decision", "decision matrix", "classify this request", "classify the following", "phân loại yêu cầu", "ma trận quyết định"), "structured-decision", "structured.decisions"),
            (("coordinate agents", "orchestrate agents", "delegate tasks to agents", "supervise agents", "điều phối agent", "giao việc cho nhiều agent"), "agent-orchestration", "agent.dispatch-design"),
        )
        for phrases, task_type, capability in routes:
            if any(phrase in text for phrase in phrases):
                try:
                    candidates = self.modules(task_type=task_type, capabilities={capability})
                except Exception:
                    return ["none"], {"task_type": task_type, "status": "registry_unavailable"}
                ids = [item["id"] for item in candidates if item.get("execution_ready") is True and item.get("id") in DECISION_CHOICES["module"]]
                return ["none", *ids], {"task_type": task_type, "status": "shortlisted" if ids else "no_execution_ready_candidate"}
        return ["none"], {"task_type": "unmatched", "status": "no_specialized_candidate"}

    def decide(self, prompt: str, *, allow_typesafe: bool = False) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        started = time.monotonic()
        decision_id = str(uuid.uuid4())
        quyet_thresholds = _thresholds("QUYET", QUYET_THRESHOLDS_DEFAULT)
        jev_thresholds = _thresholds("JEV", JEV_THRESHOLDS_DEFAULT)
        trace: dict[str, Any] = {
            "decision_id": decision_id,
            "decision_type": "task_routing",
            "advisory": True,
            "execution_authorized": False,
            "engines": [],
            "choices": {field: None for field in DECISION_CHOICES},
            "confidence": {field: None for field in DECISION_CHOICES},
            "field_sources": {field: "host" for field in DECISION_CHOICES},
            "thresholds": {"quyet_tiny": quyet_thresholds, "typesafe_jev": jev_thresholds},
            "escalation_reason": None,
            "fallback": None,
            "final_source": None,
            "capability": None,
            "provider": None,
            "route_reason": None,
            "route_lane": "HOST",
        }

        module_options, module_prefilter = self._module_options(prompt)
        trace["module_prefilter"] = {**(module_prefilter or {}), "options": module_options}
        class_options = (
            ["READ_ONLY", "WORKSPACE_WRITE", "ELEVATED"]
            if EXPLICIT_ELEVATED.search(prompt)
            else ["READ_ONLY", "WORKSPACE_WRITE"]
        )
        trace["execution_class_prefilter"] = {
            "options": class_options,
            "elevated_signal": "explicit_external_or_privileged_action" if "ELEVATED" in class_options else None,
        }

        pending = set(DECISION_CHOICES)
        try:
            response = self.quyet(
                prompt,
                module_options=module_options,
                execution_class_options=class_options,
            )
            normalized, reasons = _validate_answers(response.get("answers"), quyet_thresholds, pending)
            class_choice = normalized.get("choices", {}).get("execution_class")
            if class_choice is not None and class_choice not in class_options:
                reasons["execution_class"] = "execution_class_outside_advisory_prefilter"
            if UNDERSPECIFIED_ROUTE.search(prompt):
                reasons["route"] = "prompt_underspecified_requires_host_or_secondary_decision"
            engine_result = {"engine": "quyet-tiny", "status": "ok", "model": response.get("model")}
            if response.get("inference"):
                engine_result["inference"] = response["inference"]
            engine_result.update(normalized)
            if reasons:
                engine_result["reasons"] = reasons
            trace["engines"].append(engine_result)
            for field in pending.copy():
                if field not in reasons:
                    trace["choices"][field] = normalized["choices"][field]
                    trace["confidence"][field] = normalized["confidence"][field]
                    trace["field_sources"][field] = "quyet-tiny"
                    pending.remove(field)
            if reasons:
                trace["escalation_reason"] = reasons
                if reasons.get("route") == "prompt_underspecified_requires_host_or_secondary_decision":
                    trace["route_ambiguity_guard"] = "requires_clarification_or_secondary_decision"
        except Exception as exc:
            trace["engines"].append({"engine": "quyet-tiny", "status": "unavailable", "error": type(exc).__name__})
            trace["escalation_reason"] = {field: "quyet_unavailable" for field in pending}

        if module_options == ["none"] and module_prefilter.get("status") != "registry_unavailable":
            trace["choices"]["module"] = "none"
            trace["confidence"]["module"] = 1.0
            trace["field_sources"]["module"] = "registry-prefilter"
            pending.discard("module")
            for engine_result in trace["engines"]:
                engine_result.get("choices", {}).pop("module", None)
                engine_result.get("confidence", {}).pop("module", None)
                engine_result.get("reasons", {}).pop("module", None)
        elif module_prefilter.get("status") == "registry_unavailable":
            trace["choices"]["module"] = None
            trace["confidence"]["module"] = None
            trace["field_sources"]["module"] = "host"
            trace["escalation_reason"] = {
                **(trace.get("escalation_reason") or {}),
                "module": "module_registry_unavailable",
            }
            pending.add("module")
        elif trace["choices"].get("module") not in {None, *module_options}:
            trace["choices"]["module"] = "none"
            trace["confidence"]["module"] = None
            trace["field_sources"]["module"] = "module-registry"
            trace["module_filter"] = "module_not_in_registry_shortlist"
            pending.add("module")

        # Strong reverse-engineering language is a deterministic capability
        # prefilter.  It remains advisory: no provider process is started and
        # execution_authorized stays false until the existing dispatch gates.
        if module_prefilter.get("task_type") == "reverse-engineering" and "rea" in module_options:
            trace["choices"]["route"] = "DISPATCH"
            trace["confidence"]["route"] = 1.0
            trace["field_sources"]["route"] = "reverse-engineering-prefilter"
            trace["choices"]["module"] = "rea"
            trace["confidence"]["module"] = 1.0
            trace["field_sources"]["module"] = "reverse-engineering-prefilter"
            trace["capability"] = "REVERSE_ENGINEERING"
            trace["provider"] = "rea"
            trace["route_reason"] = "explicit reverse-engineering intent matched; REA is execution-ready"
            trace["route_lane"] = "REA"
            pending.discard("route")
            pending.discard("module")

        if module_prefilter.get("task_type") == "native-desktop-automation" and "agent-desktop" in module_options:
            trace["choices"]["route"] = "DISPATCH"
            trace["confidence"]["route"] = 1.0
            trace["field_sources"]["route"] = "desktop-prefilter"
            trace["choices"]["module"] = "agent-desktop"
            trace["confidence"]["module"] = 1.0
            trace["field_sources"]["module"] = "desktop-prefilter"
            trace["capability"] = "desktop.automation"
            trace["provider"] = "agent-desktop"
            trace["route_reason"] = "native desktop intent matched; agent-desktop is execution-ready"
            trace["route_lane"] = "DESKTOP"
            pending.discard("route")
            pending.discard("module")

        if trace.get("route_lane") == "HOST":
            if DESKTOP_INTENT.search(prompt):
                trace["route_lane"] = "DESKTOP"
                trace["route_reason"] = "native desktop intent remains on the desktop capability lane"
            elif BROWSER_INTENT.search(prompt):
                trace["route_lane"] = "BROWSER"
                trace["route_reason"] = "browser/DOM intent remains on the existing browser lane"
            elif COMPUTER_USE_INTENT.search(prompt):
                trace["route_lane"] = "ORCA"
                trace["route_reason"] = "computer-use intent remains on the existing ORCA lane"
            elif REVIEW_INTENT.search(prompt):
                trace["route_lane"] = "GSTACK"
                trace["route_reason"] = "review intent remains on the existing gstack lane"
            elif CODING_INTENT.search(prompt):
                trace["route_lane"] = "CODING"
                trace["route_reason"] = "ordinary coding intent remains on the existing coding lane"

        if allow_typesafe and pending:
            try:
                jev_fields = set(pending)
                response = self.jev(prompt, sorted(jev_fields))
                normalized, reasons = _validate_answers(response.get("answers"), jev_thresholds, jev_fields)
                engine_result = {"engine": "typesafe-jev", "status": "ok", "model": response.get("model")}
                engine_result.update(normalized)
                if reasons:
                    engine_result["reasons"] = reasons
                trace["engines"].append(engine_result)
                for field in jev_fields:
                    if field not in reasons:
                        trace["choices"][field] = normalized["choices"][field]
                        trace["confidence"][field] = normalized["confidence"][field]
                        trace["field_sources"][field] = "typesafe-jev"
                        pending.remove(field)
                if reasons:
                    trace["escalation_reason"] = {**(trace.get("escalation_reason") or {}), **{field: f"jev_{reason}" for field, reason in reasons.items()}}
            except Exception as exc:
                trace["engines"].append({"engine": "typesafe-jev", "status": "unavailable", "error": type(exc).__name__})
                trace["escalation_reason"] = {**(trace.get("escalation_reason") or {}), **{field: "jev_unavailable" for field in pending}}

        if pending:
            if not allow_typesafe:
                trace["escalation_reason"] = {**(trace.get("escalation_reason") or {}), **{field: "typesafe_not_authorized" for field in pending}}
            trace["fallback"] = "host_reasoning_required"
        sources = set(trace["field_sources"].values())
        trace["final_source"] = next(iter(sources)) if len(sources) == 1 else "mixed"
        trace["host_reasoning_required"] = sorted(pending)
        return self._finish(trace, started)

    def _finish(self, trace: dict[str, Any], started: float) -> dict[str, Any]:
        trace["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
        choices = trace.get("choices")
        if isinstance(choices, dict) and choices.get("module") not in {None, "none"}:
            module_id = choices.get("module")
            target = MODULE_VALIDATION_TARGETS.get(module_id)
            if target is None:
                choices["module"] = "none"
                trace["module_filter"] = "module_not_registered"
                trace["field_sources"]["module"] = "module-registry"
            else:
                try:
                    task_type, capability = target
                    candidates = self.modules(task_type=task_type, capabilities={capability})
                    if not any(item.get("id") == module_id and item.get("execution_ready") is True for item in candidates):
                        choices["module"] = "none"
                        trace["module_filter"] = f"{module_id}_not_execution_ready_or_capability_mismatch"
                        trace["field_sources"]["module"] = "module-registry"
                except Exception:
                    choices["module"] = "none"
                    trace["module_filter"] = "module_registry_unavailable"
                    trace["field_sources"]["module"] = "module-registry"
        sources = set(trace["field_sources"].values())
        trace["final_source"] = next(iter(sources)) if len(sources) == 1 else "mixed"
        return trace


def decide(prompt: str, *, allow_typesafe: bool = False) -> dict[str, Any]:
    return DecisionPlane().decide(prompt, allow_typesafe=allow_typesafe)
