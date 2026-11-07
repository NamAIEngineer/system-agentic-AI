"""Provider-neutral platform safety contracts.

These records carry evidence between the control plane and a platform adapter.
They deliberately contain no selectors, browser state, credentials, or platform
specific execution instructions.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


class PlatformContractError(ValueError):
    pass


SESSION_STATES = frozenset({
    "READY", "AUTH_REQUIRED", "CHALLENGED", "LOCKED", "COOLDOWN", "DISABLED", "UNKNOWN"
})
ACCEPTANCE_STATES = frozenset({"ACCEPTED", "REJECTED", "UNKNOWN"})
ACTIVATION_STATES = frozenset({
    "NOT_ATTEMPTED", "CLICK_COMMAND_SENT", "CONTROL_ACTIVATED", "CONTROL_NOT_OBSERVED", "UNKNOWN"
})
PUBLICATION_STATES = frozenset({"VERIFIED", "PENDING_MODERATION", "REMOVED", "REJECTED", "UNKNOWN"})
CAPABILITY_STATUSES = frozenset({
    "NOT_IMPLEMENTED", "DISCOVERY", "DRY_RUN_READY", "CONTROLLED",
    "PRODUCTION_READY", "HUMAN_INTERVENTION_REQUIRED"
})
_SECRET_KEY = re.compile(
    r"(password|passwd|cookie|bearer|token|credential|mfa|passkey|secret|authorization|"
    r"browser.?profile|selector|webdriver|cdp|sessionstorage|localstorage)", re.I
)


def _required(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlatformContractError(f"{name} must be a non-empty string")
    return value


def _timestamp(name: str, value: str) -> str:
    _required(name, value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PlatformContractError(f"{name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise PlatformContractError(f"{name} must include a timezone")
    return value


def _safe(value: Any, path: str = "evidence") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _SECRET_KEY.search(str(key)):
                raise PlatformContractError(f"forbidden execution detail at {path}.{key}")
            _safe(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _safe(child, f"{path}[{index}]")
    else:
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise PlatformContractError(f"{path} must be JSON serializable") from exc


def normalize_session_state(value: str) -> str:
    """Normalize control-plane and adapter vocabulary to one safe model."""
    aliases = {"HUMAN_REQUIRED": "AUTH_REQUIRED", "BLOCKED": "UNKNOWN"}
    state = str(value or "UNKNOWN").strip().upper()
    state = aliases.get(state, state)
    if state not in SESSION_STATES:
        raise PlatformContractError(f"unsupported session state: {value}")
    return state


@dataclass(frozen=True)
class SessionHealth:
    platform: str
    account_id: str
    browser_profile_id: str
    expected_identity: str
    state: str
    checked_at: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (("platform", self.platform), ("accountId", self.account_id),
                            ("browserProfileId", self.browser_profile_id),
                            ("expectedIdentity", self.expected_identity)):
            _required(name, value)
        object.__setattr__(self, "state", normalize_session_state(self.state))
        _timestamp("checkedAt", self.checked_at)
        _safe(self.evidence)

    @property
    def eligible(self) -> bool:
        return self.state == "READY"

    def to_dict(self) -> dict[str, Any]:
        return {"platform": self.platform, "accountId": self.account_id,
                "browserProfileId": self.browser_profile_id,
                "expectedIdentity": self.expected_identity, "sessionState": self.state,
                "checkedAt": self.checked_at, "evidence": self.evidence}


@dataclass(frozen=True)
class ReadinessEvidence:
    platform: str
    account_id: str
    browser_profile_id: str
    target: str
    state: str
    checked_at: str
    expires_at: str
    capabilities: dict[str, bool] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (("platform", self.platform), ("accountId", self.account_id),
                            ("browserProfileId", self.browser_profile_id), ("target", self.target)):
            _required(name, value)
        object.__setattr__(self, "state", normalize_session_state(self.state))
        checked = datetime.fromisoformat(_timestamp("checkedAt", self.checked_at).replace("Z", "+00:00"))
        expires = datetime.fromisoformat(_timestamp("expiresAt", self.expires_at).replace("Z", "+00:00"))
        if expires <= checked:
            raise PlatformContractError("expiresAt must follow checkedAt")
        if not isinstance(self.capabilities, dict) or any(not isinstance(k, str) or not isinstance(v, bool) for k, v in self.capabilities.items()):
            raise PlatformContractError("capabilities must map names to booleans")
        _safe(self.evidence)

    def fresh(self, *, now: datetime | None = None) -> bool:
        if self.state != "READY":
            return False
        current = now or datetime.now(timezone.utc)
        expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        return expires > current


@dataclass(frozen=True)
class SubmitEvidence:
    acceptance: str
    activation: str
    click_command: str
    observed_at: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.acceptance not in ACCEPTANCE_STATES:
            raise PlatformContractError("invalid acceptance state")
        if self.activation not in ACTIVATION_STATES:
            raise PlatformContractError("invalid activation state")
        if self.click_command not in {"NOT_SENT", "SENT", "OUTCOME_UNKNOWN"}:
            raise PlatformContractError("invalid click command state")
        _timestamp("observedAt", self.observed_at)
        _safe(self.evidence)
        if self.acceptance == "ACCEPTED" and self.activation != "CONTROL_ACTIVATED":
            raise PlatformContractError("accepted submission requires control activation evidence")


@dataclass(frozen=True)
class PublicationReceipt:
    state: str
    product_id: str
    campaign_id: str
    job_id: str
    content_variant_id: str
    platform: str
    account_id: str
    target: str
    publication_id: str | None = None
    permalink: str | None = None
    published_at: str | None = None
    moderation_evidence: str | None = None

    def __post_init__(self) -> None:
        if self.state not in PUBLICATION_STATES:
            raise PlatformContractError("invalid publication state")
        for name, value in (("productId", self.product_id), ("campaignId", self.campaign_id),
                            ("jobId", self.job_id), ("contentVariantId", self.content_variant_id),
                            ("platform", self.platform), ("accountId", self.account_id), ("target", self.target)):
            _required(name, value)
        if self.published_at:
            _timestamp("publishedAt", self.published_at)
        if self.state == "VERIFIED" and (not self.publication_id or not self.permalink or not self.published_at):
            raise PlatformContractError("VERIFIED publication requires id, permalink, and publishedAt")
        if self.state == "PENDING_MODERATION" and self.moderation_evidence is None:
            raise PlatformContractError("PENDING_MODERATION requires moderation evidence")


def execution_identity(*, campaign_id: str, content_variant_id: str, platform: str, account_id: str, target: str) -> str:
    values = {"campaignId": _required("campaignId", campaign_id), "contentVariantId": _required("contentVariantId", content_variant_id),
              "platform": _required("platform", platform), "accountId": _required("accountId", account_id), "target": _required("target", target)}
    return "exec-" + hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


@dataclass(frozen=True)
class UnknownQuarantine:
    execution_id: str
    reason: str
    submit_boundary_crossed: bool
    retry_allowed: bool = False

    def __post_init__(self) -> None:
        _required("executionId", self.execution_id)
        _required("reason", self.reason)
        if self.retry_allowed:
            raise PlatformContractError("UNKNOWN quarantine cannot enable automatic retry")
