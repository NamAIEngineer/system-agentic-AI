"""Provider-neutral GTM/content planning over verified ArtifactStore evidence."""
from __future__ import annotations
import hashlib, json, re
from datetime import datetime, timezone
from typing import Any, Protocol
from modules.product_contracts import ContentPackage, ContentPlan, ContentVariant, DistributionIntent, GTMContext, MediaVariant

class ContentGenerationError(ValueError): pass
class ContentGenerationAdapter(Protocol):
    def health(self) -> dict[str, Any]: ...
    def validate_input(self, context: GTMContext, plan: ContentPlan) -> None: ...
    def generate(self, context: GTMContext, plan: ContentPlan) -> list[ContentVariant]: ...
    def normalize_result(self, result: Any) -> list[ContentVariant]: ...

_ID_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")

def utc_now() -> str: return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def build_gtm_context(*, product_id: str, objective: str, artifacts: list[dict[str, Any]], media: list[dict[str, Any]], product_metadata: dict[str, Any] | None = None, constraints: list[str] | None = None, target_hints: list[str] | None = None) -> GTMContext:
    refs = tuple(a["artifactId"] for a in artifacts); media_refs = tuple(a.get("mediaArtifactId", a.get("artifactId")) for a in media)
    if any(a.get("productId") != product_id for a in [*artifacts, *media]): raise ContentGenerationError("cross-product source evidence")
    evidence = tuple({"artifactRef": a["artifactId"], "kind": a.get("artifactKind", "ProductArtifact"), "metadata": dict(a.get("metadata", {})), "technicalSpecs": dict(a.get("technicalSpecs", {}))} for a in artifacts)
    return GTMContext(product_id, objective, refs, media_refs, evidence, product_metadata or {}, tuple(constraints or ()), tuple(target_hints or ()), utc_now())

def stable_content_variant_id(*, product_id: str, content_plan_id: str, angle: str, format: str, text: str, source_refs: list[str]) -> str:
    payload = json.dumps({"productId": product_id, "contentPlanId": content_plan_id, "angle": angle, "format": format, "text": text, "sourceRefs": sorted(source_refs)}, sort_keys=True, separators=(",", ":"))
    return "cv-" + hashlib.sha256(payload.encode()).hexdigest()[:24]

class FixtureContentGenerationAdapter:
    """Deterministic local adapter; it emits only grounded claims from evidence."""
    def health(self) -> dict[str, Any]: return {"status": "READY", "provider": "local-fixture", "externalProvider": False}
    def validate_input(self, context: GTMContext, plan: ContentPlan) -> None:
        if context.product_id != plan.product_id: raise ContentGenerationError("context/plan product mismatch")
        if not plan.content_angles: raise ContentGenerationError("at least one content angle is required")
        allowed = set(context.artifact_refs) | set(context.media_refs)
        if not set(plan.source_artifact_refs) <= allowed or not set(plan.source_media_refs) <= allowed: raise ContentGenerationError("plan source refs are not in verified context")
        for req in plan.variant_requests:
            if set(req) - {"angle", "audience", "format", "platformHints", "text"}: raise ContentGenerationError("unknown variant request field")
            if "text" in req and not isinstance(req["text"], str): raise ContentGenerationError("text must be a string")
    def generate(self, context: GTMContext, plan: ContentPlan) -> list[ContentVariant]:
        self.validate_input(context, plan); variants=[]
        requests = list(plan.variant_requests) or [{"angle": a, "audience": (plan.target_audiences[0] if plan.target_audiences else "general"), "format": (plan.requested_formats[0] if plan.requested_formats else "text")} for a in plan.content_angles]
        evidence_summary = "; ".join(f"{e['artifactRef']} ({e['kind']})" for e in context.technical_evidence)
        for req in requests:
            angle, audience, fmt = req.get("angle"), req.get("audience", "general"), req.get("format", "text")
            if not isinstance(angle, str) or angle not in plan.content_angles: raise ContentGenerationError("variant angle is not declared by plan")
            text = req.get("text") or f"{plan.objective}: verified evidence {evidence_summary}."
            source_refs = [*plan.source_artifact_refs, *plan.source_media_refs]
            vid = stable_content_variant_id(product_id=context.product_id, content_plan_id=plan.content_plan_id, angle=angle, format=fmt, text=text, source_refs=source_refs)
            variants.append(ContentVariant(vid, context.product_id, "content-foundation", tuple(plan.source_artifact_refs), tuple(plan.source_media_refs), fmt, angle, audience, tuple(req.get("platformHints", [])), text, (), {"grounding":"VERIFIED_FACT","sourceEvidence":list(context.artifact_refs)}, {"productId":context.product_id,"contentPlanId":plan.content_plan_id}, "content-fixture", "content-generation", utc_now(), plan.content_plan_id))
        return variants
    def normalize_result(self, result: Any) -> list[ContentVariant]:
        if not isinstance(result, list): raise ContentGenerationError("content result must be a list")
        return [item if isinstance(item, ContentVariant) else ContentVariant.from_dict(item) for item in result]

def build_content_package(*, package_id: str, product_id: str, variants: list[ContentVariant], media_variant_refs: list[str], source_artifact_refs: list[str], pipeline_id: str = "content-foundation", producer_step_id: str = "content-package") -> ContentPackage:
    if not variants or any(v.product_id != product_id for v in variants): raise ContentGenerationError("package variants must share product")
    if any(not v.content_plan_id for v in variants): raise ContentGenerationError("package variants have incomplete provenance")
    if len(set(media_variant_refs)) != len(media_variant_refs) or len(set(source_artifact_refs)) != len(source_artifact_refs): raise ContentGenerationError("package references must be unique")
    return ContentPackage(package_id, product_id, tuple(v.content_variant_id for v in variants), tuple(media_variant_refs), tuple(source_artifact_refs), {"variantCount":len(variants),"grounded":True}, utc_now(), pipeline_id, "content-fixture", producer_step_id)

def build_distribution_intent(*, intent_id: str, package: ContentPackage, objective: str, audience_hints: list[str] | None = None, platform_hints: list[str] | None = None, pipeline_id: str = "content-foundation", producer_step_id: str = "distribution-intent") -> DistributionIntent:
    # This contract deliberately carries no platform execution/session details.
    return DistributionIntent(intent_id, package.product_id, package.content_variant_refs, package.media_variant_refs, objective, tuple(audience_hints or ()), tuple(platform_hints or ()), {"contentPackageId": package.content_package_id, "handoff":"LOCAL_ONLY"}, utc_now(), pipeline_id, "content-fixture", producer_step_id, package.content_package_id)

class DistributionBoundaryFixture:
    def validate_only(self, intent: DistributionIntent) -> dict[str, Any]:
        value = DistributionIntent.from_dict(intent.to_dict())
        forbidden = ("selector", "browser", "credential", "session", "subreddit", "account")
        raw = json.dumps(value.to_dict()).lower()
        if any(token in raw for token in forbidden): raise ContentGenerationError("distribution intent contains execution internals")
        return {"accepted": True, "localOnly": True, "distributionIntentId": value.distribution_intent_id}
