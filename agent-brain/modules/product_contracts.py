"""Extensible product, media, content, and distribution handoff contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def _nonempty(name: str, value: Any, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
    return value


def _string_list(name: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{name} must be an array of non-empty strings.")
    return tuple(value)


def _object(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object.")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain JSON-safe values.") from exc
    return dict(value)


@dataclass(frozen=True)
class ProductRun:
    product_id: str
    objective: str
    status: str = "PENDING"
    pipeline_ids: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    media_refs: tuple[str, ...] = ()
    content_variant_refs: tuple[str, ...] = ()
    campaign_refs: tuple[str, ...] = ()
    distribution_intent_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProductRun":
        product_id = _nonempty("productId", value.get("productId"))
        objective = _nonempty("objective", value.get("objective"))
        status = value.get("status", "PENDING")
        if status not in {"PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"}:
            raise ValueError("ProductRun status is invalid.")
        return cls(product_id, objective, status, _string_list("pipelineIds", value.get("pipelineIds", [])),
                   _string_list("artifactRefs", value.get("artifactRefs", [])),
                   _string_list("mediaRefs", value.get("mediaRefs", [])),
                   _string_list("contentVariantRefs", value.get("contentVariantRefs", [])),
                   _string_list("campaignRefs", value.get("campaignRefs", [])),
                   _string_list("distributionIntentRefs", value.get("distributionIntentRefs", [])),
                   _object("metadata", value.get("metadata", {})),
                   _nonempty("createdAt", value.get("createdAt", "")))

    def to_dict(self) -> dict[str, Any]:
        return {"productId": self.product_id, "objective": self.objective, "status": self.status,
                "pipelineIds": list(self.pipeline_ids), "artifactRefs": list(self.artifact_refs),
                "mediaRefs": list(self.media_refs), "contentVariantRefs": list(self.content_variant_refs),
                "campaignRefs": list(self.campaign_refs), "distributionIntentRefs": list(self.distribution_intent_refs),
                "metadata": self.metadata, "createdAt": self.created_at}


@dataclass(frozen=True)
class MediaArtifact:
    media_artifact_id: str
    product_id: str
    pipeline_id: str
    producer_module_id: str
    producer_step_id: str
    media_type: str
    file_ref: str
    metadata: dict[str, Any] = field(default_factory=dict)
    source_artifact_refs: tuple[str, ...] = ()
    created_at: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MediaArtifact":
        return cls(_nonempty("mediaArtifactId", value.get("mediaArtifactId")),
                   _nonempty("productId", value.get("productId")),
                   _nonempty("pipelineId", value.get("pipelineId")),
                   _nonempty("producerModuleId", value.get("producerModuleId")),
                   _nonempty("producerStepId", value.get("producerStepId")),
                   _nonempty("mediaType", value.get("mediaType")),
                   _nonempty("fileRef", value.get("fileRef")),
                   _object("metadata", value.get("metadata", {})),
                   _string_list("sourceArtifactRefs", value.get("sourceArtifactRefs", [])),
                   _nonempty("createdAt", value.get("createdAt", "")))

    def to_dict(self) -> dict[str, Any]:
        return {"mediaArtifactId": self.media_artifact_id, "productId": self.product_id,
                "pipelineId": self.pipeline_id, "producerModuleId": self.producer_module_id,
                "producerStepId": self.producer_step_id, "mediaType": self.media_type,
                "fileRef": self.file_ref, "metadata": self.metadata,
                "sourceArtifactRefs": list(self.source_artifact_refs), "createdAt": self.created_at}


@dataclass(frozen=True)
class ContentVariant:
    content_variant_id: str
    product_id: str
    pipeline_id: str
    source_artifact_refs: tuple[str, ...]
    source_media_refs: tuple[str, ...]
    format: str
    angle: str
    audience: str
    platform_hints: tuple[str, ...]
    text: str
    media_refs: tuple[str, ...]
    metadata: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    producer_module_id: str = ""
    producer_step_id: str = ""
    created_at: str = ""
    content_plan_id: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ContentVariant":
        return cls(_nonempty("contentVariantId", value.get("contentVariantId")),
                   _nonempty("productId", value.get("productId")),
                   _nonempty("pipelineId", value.get("pipelineId")),
                   _string_list("sourceArtifactRefs", value.get("sourceArtifactRefs", [])),
                   _string_list("sourceMediaRefs", value.get("sourceMediaRefs", [])),
                   _nonempty("format", value.get("format")), _nonempty("angle", value.get("angle")),
                   _nonempty("audience", value.get("audience")),
                   _string_list("platformHints", value.get("platformHints", [])),
                   value.get("text", "") if isinstance(value.get("text", ""), str) else _nonempty("text", value.get("text")),
                   _string_list("mediaRefs", value.get("mediaRefs", [])),
                   _object("metadata", value.get("metadata", {})), _object("provenance", value.get("provenance", {})),
                   _nonempty("producerModuleId", value.get("producerModuleId")),
                   _nonempty("producerStepId", value.get("producerStepId")),
                   _nonempty("createdAt", value.get("createdAt", "")),
                   value.get("contentPlanId", "") if isinstance(value.get("contentPlanId", ""), str) else _nonempty("contentPlanId", value.get("contentPlanId")))

    def to_dict(self) -> dict[str, Any]:
        result = {"contentVariantId": self.content_variant_id, "productId": self.product_id,
                  "pipelineId": self.pipeline_id, "sourceArtifactRefs": list(self.source_artifact_refs),
                  "sourceMediaRefs": list(self.source_media_refs), "format": self.format, "angle": self.angle,
                  "audience": self.audience, "platformHints": list(self.platform_hints), "text": self.text,
                  "mediaRefs": list(self.media_refs), "metadata": self.metadata, "provenance": self.provenance,
                  "producerModuleId": self.producer_module_id, "producerStepId": self.producer_step_id,
                  "createdAt": self.created_at}
        if self.content_plan_id:
            result["contentPlanId"] = self.content_plan_id
        return result


@dataclass(frozen=True)
class MediaVariant:
    """A derived media item with explicit transformation provenance."""
    media_variant_id: str
    product_id: str
    pipeline_id: str
    content_plan_id: str
    source_artifact_refs: tuple[str, ...]
    source_media_refs: tuple[str, ...]
    format: str
    file_ref: str
    producer_module_id: str
    producer_step_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    transformation_id: str = ""
    dimensions: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float | None = None
    created_at: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MediaVariant":
        duration = value.get("durationSeconds")
        if duration is not None and (type(duration) not in {int, float} or duration < 0):
            raise ValueError("durationSeconds must be a non-negative number")
        return cls(_nonempty("mediaVariantId", value.get("mediaVariantId")),
                   _nonempty("productId", value.get("productId")), _nonempty("pipelineId", value.get("pipelineId")),
                   _nonempty("contentPlanId", value.get("contentPlanId")),
                   _string_list("sourceArtifactRefs", value.get("sourceArtifactRefs", [])),
                   _string_list("sourceMediaRefs", value.get("sourceMediaRefs", [])),
                   _nonempty("format", value.get("format")), _nonempty("fileRef", value.get("fileRef")),
                   _nonempty("producerModuleId", value.get("producerModuleId")),
                   _nonempty("producerStepId", value.get("producerStepId")),
                   _object("metadata", value.get("metadata", {})),
                   _nonempty("transformationId", value.get("transformationId")),
                   _object("dimensions", value.get("dimensions", {})), duration,
                   _nonempty("createdAt", value.get("createdAt", "")))

    def to_dict(self) -> dict[str, Any]:
        return {"mediaVariantId": self.media_variant_id, "productId": self.product_id, "pipelineId": self.pipeline_id,
                "contentPlanId": self.content_plan_id, "sourceArtifactRefs": list(self.source_artifact_refs),
                "sourceMediaRefs": list(self.source_media_refs), "format": self.format,
                "fileRef": self.file_ref, "producerModuleId": self.producer_module_id,
                "producerStepId": self.producer_step_id, "metadata": self.metadata,
                "transformationId": self.transformation_id, "dimensions": self.dimensions,
                "durationSeconds": self.duration_seconds, "createdAt": self.created_at}


@dataclass(frozen=True)
class GTMContext:
    product_id: str
    product_objective: str
    artifact_refs: tuple[str, ...]
    media_refs: tuple[str, ...]
    technical_evidence: tuple[dict[str, Any], ...] = ()
    product_metadata: dict[str, Any] = field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    target_hints: tuple[str, ...] = ()
    created_at: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "GTMContext":
        evidence = value.get("technicalEvidence", [])
        if not isinstance(evidence, list) or any(not isinstance(item, dict) for item in evidence):
            raise ValueError("technicalEvidence must be an object array")
        return cls(_nonempty("productId", value.get("productId")), _nonempty("productObjective", value.get("productObjective")),
                   _string_list("artifactRefs", value.get("artifactRefs", [])), _string_list("mediaRefs", value.get("mediaRefs", [])),
                   tuple(_object("technicalEvidence", item) for item in evidence), _object("productMetadata", value.get("productMetadata", {})),
                   _string_list("constraints", value.get("constraints", [])), _string_list("targetHints", value.get("targetHints", [])),
                   _nonempty("createdAt", value.get("createdAt", "")))

    def to_dict(self) -> dict[str, Any]:
        return {"productId": self.product_id, "productObjective": self.product_objective,
                "artifactRefs": list(self.artifact_refs), "mediaRefs": list(self.media_refs),
                "technicalEvidence": list(self.technical_evidence), "productMetadata": self.product_metadata,
                "constraints": list(self.constraints), "targetHints": list(self.target_hints), "createdAt": self.created_at}


@dataclass(frozen=True)
class ContentPlan:
    content_plan_id: str
    product_id: str
    objective: str
    source_artifact_refs: tuple[str, ...]
    source_media_refs: tuple[str, ...]
    target_audiences: tuple[str, ...] = ()
    content_angles: tuple[str, ...] = ()
    requested_formats: tuple[str, ...] = ()
    variant_requests: tuple[dict[str, Any], ...] = ()
    constraints: tuple[str, ...] = ()
    created_at: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ContentPlan":
        requests = value.get("variantRequests", [])
        if not isinstance(requests, list) or any(not isinstance(item, dict) for item in requests):
            raise ValueError("variantRequests must be an object array")
        return cls(_nonempty("contentPlanId", value.get("contentPlanId")), _nonempty("productId", value.get("productId")),
                   _nonempty("objective", value.get("objective")), _string_list("sourceArtifactRefs", value.get("sourceArtifactRefs", [])),
                   _string_list("sourceMediaRefs", value.get("sourceMediaRefs", [])), _string_list("targetAudiences", value.get("targetAudiences", [])),
                   _string_list("contentAngles", value.get("contentAngles", [])), _string_list("requestedFormats", value.get("requestedFormats", [])),
                   tuple(_object("variantRequests", item) for item in requests), _string_list("constraints", value.get("constraints", [])),
                   _nonempty("createdAt", value.get("createdAt", "")))

    def to_dict(self) -> dict[str, Any]:
        return {"contentPlanId": self.content_plan_id, "productId": self.product_id, "objective": self.objective,
                "sourceArtifactRefs": list(self.source_artifact_refs), "sourceMediaRefs": list(self.source_media_refs),
                "targetAudiences": list(self.target_audiences), "contentAngles": list(self.content_angles),
                "requestedFormats": list(self.requested_formats), "variantRequests": list(self.variant_requests),
                "constraints": list(self.constraints), "createdAt": self.created_at}


@dataclass(frozen=True)
class ContentPackage:
    content_package_id: str
    product_id: str
    content_variant_refs: tuple[str, ...]
    media_variant_refs: tuple[str, ...] = ()
    source_artifact_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    pipeline_id: str = ""
    producer_module_id: str = ""
    producer_step_id: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ContentPackage":
        variants = _string_list("contentVariantRefs", value.get("contentVariantRefs", []))
        if not variants:
            raise ValueError("ContentPackage requires at least one content variant")
        return cls(_nonempty("contentPackageId", value.get("contentPackageId")), _nonempty("productId", value.get("productId")),
                   variants, _string_list("mediaVariantRefs", value.get("mediaVariantRefs", [])),
                   _string_list("sourceArtifactRefs", value.get("sourceArtifactRefs", [])), _object("metadata", value.get("metadata", {})),
                   _nonempty("createdAt", value.get("createdAt", "")), _nonempty("pipelineId", value.get("pipelineId")),
                   _nonempty("producerModuleId", value.get("producerModuleId")), _nonempty("producerStepId", value.get("producerStepId")))

    def to_dict(self) -> dict[str, Any]:
        return {"contentPackageId": self.content_package_id, "productId": self.product_id,
                "contentVariantRefs": list(self.content_variant_refs), "mediaVariantRefs": list(self.media_variant_refs),
                "sourceArtifactRefs": list(self.source_artifact_refs), "metadata": self.metadata, "createdAt": self.created_at,
                "pipelineId": self.pipeline_id, "producerModuleId": self.producer_module_id, "producerStepId": self.producer_step_id}


@dataclass(frozen=True)
class DistributionIntent:
    distribution_intent_id: str
    product_id: str
    content_variant_refs: tuple[str, ...]
    media_refs: tuple[str, ...]
    objective: str
    audience_hints: tuple[str, ...] = ()
    platform_hints: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    pipeline_id: str = ""
    producer_module_id: str = ""
    producer_step_id: str = ""
    content_package_ref: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DistributionIntent":
        return cls(_nonempty("distributionIntentId", value.get("distributionIntentId")),
                   _nonempty("productId", value.get("productId")),
                   _string_list("contentVariantRefs", value.get("contentVariantRefs", [])),
                   _string_list("mediaRefs", value.get("mediaRefs", [])),
                   _nonempty("objective", value.get("objective")),
                   _string_list("audienceHints", value.get("audienceHints", [])),
                   _string_list("platformHints", value.get("platformHints", [])),
                   _object("metadata", value.get("metadata", {})),
                   _nonempty("createdAt", value.get("createdAt", "")),
                   _nonempty("pipelineId", value.get("pipelineId")),
                   _nonempty("producerModuleId", value.get("producerModuleId")),
                   _nonempty("producerStepId", value.get("producerStepId")), value.get("contentPackageRef", ""))

    def to_dict(self) -> dict[str, Any]:
        result = {"distributionIntentId": self.distribution_intent_id, "productId": self.product_id,
                  "contentVariantRefs": list(self.content_variant_refs), "mediaRefs": list(self.media_refs),
                  "objective": self.objective, "audienceHints": list(self.audience_hints),
                  "platformHints": list(self.platform_hints), "metadata": self.metadata,
                  "createdAt": self.created_at, "pipelineId": self.pipeline_id,
                  "producerModuleId": self.producer_module_id, "producerStepId": self.producer_step_id}
        if self.content_package_ref:
            result["contentPackageRef"] = self.content_package_ref
        return result


@dataclass(frozen=True)
class DistributionMetric:
    product_id: str
    campaign_id: str
    content_variant_id: str
    platform: str
    observed_at: str
    metrics: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DistributionMetric":
        return cls(_nonempty("productId", value.get("productId")),
                   _nonempty("campaignId", value.get("campaignId")),
                   _nonempty("contentVariantId", value.get("contentVariantId")),
                   _nonempty("platform", value.get("platform")),
                   _nonempty("observedAt", value.get("observedAt")),
                   _object("metrics", value.get("metrics")), _object("metadata", value.get("metadata", {})))

    def to_dict(self) -> dict[str, Any]:
        return {"productId": self.product_id, "campaignId": self.campaign_id,
                "contentVariantId": self.content_variant_id, "platform": self.platform,
                "observedAt": self.observed_at, "metrics": self.metrics, "metadata": self.metadata}


@dataclass(frozen=True)
class PipelineTemplate:
    template_id: str
    objective: str
    steps: tuple[dict[str, Any], ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PipelineTemplate":
        steps = value.get("steps")
        if not isinstance(steps, list) or not steps or any(not isinstance(step, dict) for step in steps):
            raise ValueError("PipelineTemplate requires a non-empty steps array of objects.")
        return cls(_nonempty("templateId", value.get("templateId")),
                   _nonempty("objective", value.get("objective")), tuple(dict(step) for step in steps),
                   _object("metadata", value.get("metadata", {})))

    def instantiate(self, pipeline_id: str, product_id: str) -> dict[str, Any]:
        return {"pipelineId": _nonempty("pipelineId", pipeline_id), "productId": _nonempty("productId", product_id),
                "templateId": self.template_id, "goal": self.objective,
                "steps": [dict(step) for step in self.steps]}
