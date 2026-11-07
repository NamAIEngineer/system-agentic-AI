"""Safe System-Agentic -> downstream-distribution handoff boundary.

The boundary is intentionally provider neutral.  DRY_RUN writes only to an
explicit isolated store supplied by the caller.  LIVE is fail-closed until a
future, separately authorized phase enables it.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


class HandoffError(ValueError):
    pass


SECRET_KEY = re.compile(r"(password|passwd|cookie|bearer|token|credential|mfa|passkey|secret|authorization|browser.?profile|selector|subreddit|subreddit.?runtime|session)", re.I)
SOCIAL_INTERNAL_KEY = re.compile(r"(button|selector|browser|profile|credential|cookie|subreddit|group.?id|account.?session|webdriver|cdp)", re.I)
SESSION_STATES = frozenset({"READY", "AUTH_REQUIRED", "CHALLENGED", "LOCKED", "COOLDOWN", "DISABLED", "UNKNOWN"})
HANDOFF_MODES = frozenset({"DRY_RUN", "LIVE"})


def _required(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HandoffError(f"{name} must be a non-empty string")
    return value


def _strings(name: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise HandoffError(f"{name} must be an array of non-empty strings")
    return tuple(value)


def _iso(name: str, value: str) -> str:
    _required(name, value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HandoffError(f"{name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise HandoffError(f"{name} must include a timezone")
    return value


def _json_safe(value: Any, name: str = "value") -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise HandoffError(f"{name} must be JSON serializable") from exc


def _assert_safe(value: Any, *, path: str = "metadata", social: bool = False) -> None:
    """Reject credentials and downstream browser execution details."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if SECRET_KEY.search(key_text):
                raise HandoffError(f"forbidden secret or execution field at {path}.{key_text}")
            if social and SOCIAL_INTERNAL_KEY.search(key_text):
                raise HandoffError(f"forbidden social execution detail at {path}.{key_text}")
            _assert_safe(child, path=f"{path}.{key_text}", social=social)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_safe(child, path=f"{path}[{index}]", social=social)


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class DistributionHandoffAuthorization:
    authorization_id: str
    product_id: str
    distribution_intent_id: str
    allowed_content_variant_refs: tuple[str, ...]
    allowed_media_refs: tuple[str, ...]
    allowed_platforms: tuple[str, ...]
    allowed_mode: str
    expires_at: str
    created_at: str

    def __post_init__(self) -> None:
        _required("authorizationId", self.authorization_id)
        _required("productId", self.product_id)
        _required("distributionIntentId", self.distribution_intent_id)
        _strings("allowedContentVariantRefs", self.allowed_content_variant_refs)
        _strings("allowedMediaRefs", self.allowed_media_refs)
        _strings("allowedPlatforms", self.allowed_platforms)
        if self.allowed_mode not in HANDOFF_MODES:
            raise HandoffError("allowedMode must be DRY_RUN or LIVE")
        _iso("expiresAt", self.expires_at)
        _iso("createdAt", self.created_at)

    def to_dict(self) -> dict[str, Any]:
        return {"authorizationId": self.authorization_id, "productId": self.product_id,
                "distributionIntentId": self.distribution_intent_id,
                "allowedContentVariantRefs": list(self.allowed_content_variant_refs),
                "allowedMediaRefs": list(self.allowed_media_refs), "allowedPlatforms": list(self.allowed_platforms),
                "allowedMode": self.allowed_mode, "expiresAt": self.expires_at, "createdAt": self.created_at}


@dataclass(frozen=True)
class DistributionHandoffRequest:
    request_id: str
    product_id: str
    distribution_intent_id: str
    content_package_ref: str
    content_variant_refs: tuple[str, ...]
    media_refs: tuple[str, ...]
    media_sha256: dict[str, str]
    allowed_platforms: tuple[str, ...]
    mode: str = "DRY_RUN"
    provenance: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def __post_init__(self) -> None:
        _required("requestId", self.request_id)
        _required("productId", self.product_id)
        _required("distributionIntentId", self.distribution_intent_id)
        _required("contentPackageRef", self.content_package_ref)
        _strings("contentVariantRefs", self.content_variant_refs)
        _strings("mediaRefs", self.media_refs)
        if not isinstance(self.media_sha256, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v, re.I) for k, v in self.media_sha256.items()):
            raise HandoffError("mediaSha256 must map refs to SHA-256 strings")
        _strings("allowedPlatforms", self.allowed_platforms)
        if self.mode not in HANDOFF_MODES:
            raise HandoffError("mode must be DRY_RUN or LIVE")
        _json_safe(self.provenance, "provenance")
        _assert_safe(self.provenance, social=True)
        _iso("createdAt", self.created_at)

    def to_dict(self) -> dict[str, Any]:
        return {"requestId": self.request_id, "productId": self.product_id,
                "distributionIntentId": self.distribution_intent_id, "contentPackageRef": self.content_package_ref,
                "contentVariantRefs": list(self.content_variant_refs), "mediaRefs": list(self.media_refs),
                "mediaSha256": dict(self.media_sha256), "allowedPlatforms": list(self.allowed_platforms),
                "mode": self.mode, "provenance": self.provenance, "createdAt": self.created_at}


@dataclass(frozen=True)
class AccountSessionState:
    platform: str
    account_id: str
    browser_profile_id: str
    expected_identity: str
    session_state: str
    last_checked_at: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (("platform", self.platform), ("accountId", self.account_id), ("browserProfileId", self.browser_profile_id), ("expectedIdentity", self.expected_identity)):
            _required(name, value)
        if self.session_state not in SESSION_STATES:
            raise HandoffError(f"unsupported session state: {self.session_state}")
        _iso("lastCheckedAt", self.last_checked_at)
        _json_safe(self.evidence, "evidence")
        _assert_safe(self.evidence)

    def eligible(self) -> bool:
        return self.session_state == "READY"

    def to_dict(self) -> dict[str, Any]:
        return {"platform": self.platform, "accountId": self.account_id, "browserProfileId": self.browser_profile_id,
                "expectedIdentity": self.expected_identity, "sessionState": self.session_state,
                "lastCheckedAt": self.last_checked_at, "evidence": self.evidence}


class MarketingSeedingHandoffAdapter:
    """Creates isolated dry-run campaign/job representations only."""

    def __init__(self, dry_run_store: str | Path):
        self.dry_run_store = Path(dry_run_store).expanduser().resolve()

    def _load(self) -> dict[str, Any]:
        if not self.dry_run_store.exists():
            return {"schemaVersion": 1, "campaigns": [], "jobs": [], "createdAt": datetime.now(timezone.utc).isoformat()}
        value = json.loads(self.dry_run_store.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schemaVersion") != 1 or not isinstance(value.get("campaigns"), list) or not isinstance(value.get("jobs"), list):
            raise HandoffError("invalid isolated dry-run store")
        return value

    def handoff(self, request: DistributionHandoffRequest, authorization: DistributionHandoffAuthorization,
                *, content_variants: Sequence[Mapping[str, Any]], media_artifacts: Sequence[Mapping[str, Any]] = (), now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        if request.mode != "DRY_RUN" or authorization.allowed_mode != "DRY_RUN":
            raise HandoffError("LIVE handoff is disabled; only DRY_RUN is supported")
        if authorization.product_id != request.product_id or authorization.distribution_intent_id != request.distribution_intent_id:
            raise HandoffError("authorization is not bound to the request")
        if datetime.fromisoformat(authorization.expires_at.replace("Z", "+00:00")) <= now:
            raise HandoffError("handoff authorization is expired")
        if set(request.content_variant_refs) - set(authorization.allowed_content_variant_refs):
            raise HandoffError("request contains content variant outside authorization")
        if set(request.media_refs) - set(authorization.allowed_media_refs):
            raise HandoffError("request contains media outside authorization")
        if set(request.allowed_platforms) - set(authorization.allowed_platforms):
            raise HandoffError("request contains platform outside authorization")
        variants = {str(item.get("contentVariantId")): item for item in content_variants}
        if set(variants) != set(request.content_variant_refs):
            raise HandoffError("content variant refs do not resolve exactly")
        for variant in variants.values():
            if variant.get("productId") != request.product_id:
                raise HandoffError("cross-product content variant rejected")
            _assert_safe(variant, social=True)
        media = {str(item.get("mediaArtifactId", item.get("mediaVariantId", item.get("artifactId")))): item for item in media_artifacts}
        if set(request.media_refs) - set(media):
            raise HandoffError("media refs do not resolve")
        for ref in request.media_refs:
            item = media[ref]
            if item.get("productId") != request.product_id or str(item.get("metadata", {}).get("sha256", "")).lower() != request.media_sha256.get(ref, "").lower():
                raise HandoffError(f"media integrity/provenance mismatch for {ref}")
            _assert_safe(item, social=True)
        store = self._load()
        campaign_id = "dryrun-campaign-" + _sha({"productId": request.product_id, "intent": request.distribution_intent_id})[:16]
        if not any(x.get("id") == campaign_id for x in store["campaigns"]):
            store["campaigns"].append({"id": campaign_id, "productId": request.product_id, "distributionIntentId": request.distribution_intent_id, "mode": "DRY_RUN", "status": "DRY_RUN_ONLY"})
        jobs = []
        for variant_id in request.content_variant_refs:
            for platform in request.allowed_platforms:
                job_id = "dryrun-job-" + _sha({"campaign": campaign_id, "variant": variant_id, "platform": platform})[:16]
                job = {"id": job_id, "campaignId": campaign_id, "productId": request.product_id, "contentVariantId": variant_id,
                       "mediaRefs": list(request.media_refs), "platform": platform, "mode": "DRY_RUN", "status": "DRY_RUN_READY",
                       "executionCreated": False, "liveDispatch": False, "publishClicked": False, "quotaConsumed": False,
                       "provenance": {"requestId": request.request_id, "distributionIntentId": request.distribution_intent_id, "contentPackageRef": request.content_package_ref}}
                if not any(x.get("id") == job_id for x in store["jobs"]):
                    store["jobs"].append(job)
                jobs.append(job)
        self.dry_run_store.parent.mkdir(parents=True, exist_ok=True)
        self.dry_run_store.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {"mode": "DRY_RUN", "campaignId": campaign_id, "jobIds": [job["id"] for job in jobs], "jobs": jobs,
                "canonicalStateTouched": False, "liveDispatch": False, "publicationAttempts": 0}


def capability_matrix(*, session_states: Mapping[str, str]) -> list[dict[str, Any]]:
    rows = []
    for platform in ("linkedin", "reddit", "facebook", "threads"):
        state = session_states.get(platform, "UNKNOWN")
        if platform == "linkedin":
            status = "PRODUCTION_READY" if state == "READY" else "HUMAN_INTERVENTION_REQUIRED"
        else:
            status = "DRY_RUN_READY" if state == "READY" else ("HUMAN_INTERVENTION_REQUIRED" if state in {"AUTH_REQUIRED", "CHALLENGED", "LOCKED"} else "DISCOVERY")
        rows.append({"platform": platform, "adapterStatus": status, "sessionState": state,
                     "targetTypes": [], "text": "UNKNOWN", "link": "UNKNOWN", "image": "UNKNOWN", "video": "UNKNOWN",
                     "flairOrCategory": "UNKNOWN", "audienceControls": "UNKNOWN", "readiness": status in {"PRODUCTION_READY", "DRY_RUN_READY"},
                     "dryRun": True, "liveExactDispatch": platform == "linkedin" and state == "READY",
                     "acceptanceEvidence": platform == "linkedin" and state == "READY", "publicationVerifier": platform == "linkedin" and state == "READY",
                     "metrics": True, "liveValidation": "NOT_EXECUTED"})
    return rows

@dataclass(frozen=True)
class PublicationMetricIdentity:
    product_id: str
    campaign_id: str
    job_id: str
    content_variant_id: str
    platform: str
    account_id: str
    target: str
    publication_id: str
    permalink: str
    published_at: str

    def __post_init__(self) -> None:
        for name, value in (("productId", self.product_id), ("campaignId", self.campaign_id), ("jobId", self.job_id),
                            ("contentVariantId", self.content_variant_id), ("platform", self.platform), ("accountId", self.account_id),
                            ("target", self.target), ("publicationId", self.publication_id), ("permalink", self.permalink)):
            _required(name, value)
        _iso("publishedAt", self.published_at)

    def to_dict(self) -> dict[str, str]:
        return {"productId": self.product_id, "campaignId": self.campaign_id, "jobId": self.job_id,
                "contentVariantId": self.content_variant_id, "platform": self.platform, "accountId": self.account_id,
                "target": self.target, "publicationId": self.publication_id, "permalink": self.permalink,
                "publishedAt": self.published_at}
