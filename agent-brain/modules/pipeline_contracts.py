"""Small transport contracts for product artifacts and dependency plans."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any


@dataclass(frozen=True)
class ProductArtifact:
    artifact_id: str
    producer_module_id: str
    product_type: str
    files: tuple[str, ...]
    preview_assets: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    technical_specs: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    producer_step_id: str = ""
    pipeline_id: str = ""
    product_id: str | None = None
    source_artifact_refs: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProductArtifact":
        required = ("artifactId", "producerModuleId", "producerStepId", "pipelineId", "productType", "files", "createdAt")
        if not isinstance(value, dict) or any(not isinstance(value.get(key), str) or not value[key].strip() for key in required if key != "files"):
            raise ValueError("ProductArtifact requires artifactId, producerModuleId, producerStepId, pipelineId, productType, and createdAt.")
        if not isinstance(value.get("files"), list) or any(not isinstance(item, str) or not item.strip() for item in value["files"]):
            raise ValueError("ProductArtifact files must be an array of non-empty paths.")
        previews = value.get("previewAssets", [])
        if not isinstance(previews, list) or any(not isinstance(item, str) or not item.strip() for item in previews):
            raise ValueError("ProductArtifact previewAssets must be an array of non-empty paths.")
        metadata = value.get("metadata", {})
        specs = value.get("technicalSpecs", {})
        if not isinstance(metadata, dict) or not isinstance(specs, dict):
            raise ValueError("ProductArtifact metadata and technicalSpecs must be objects.")
        try:
            json.dumps({"metadata": metadata, "technicalSpecs": specs}, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("ProductArtifact metadata and technicalSpecs must contain JSON-safe values.") from exc
        product_id = value.get("productId")
        source_refs = value.get("sourceArtifactRefs", [])
        if product_id is not None and (not isinstance(product_id, str) or not product_id.strip()):
            raise ValueError("ProductArtifact productId must be a non-empty string when supplied.")
        if not isinstance(source_refs, list) or any(not isinstance(item, str) or not item.strip() for item in source_refs):
            raise ValueError("ProductArtifact sourceArtifactRefs must be an array of non-empty strings.")
        return cls(value["artifactId"], value["producerModuleId"], value["productType"], tuple(value["files"]), tuple(previews), metadata, specs, value["createdAt"], value["producerStepId"], value["pipelineId"], product_id, tuple(source_refs))

    def to_dict(self) -> dict[str, Any]:
        result = {
            "artifactId": self.artifact_id,
            "producerModuleId": self.producer_module_id,
            "productType": self.product_type,
            "files": list(self.files),
            "previewAssets": list(self.preview_assets),
            "metadata": self.metadata,
            "technicalSpecs": self.technical_specs,
            "createdAt": self.created_at,
            "producerStepId": self.producer_step_id,
            "pipelineId": self.pipeline_id,
        }
        if self.product_id is not None:
            result["productId"] = self.product_id
        if self.source_artifact_refs:
            result["sourceArtifactRefs"] = list(self.source_artifact_refs)
        return result


@dataclass(frozen=True)
class PipelineStep:
    step_id: str
    module_id: str | None
    capability: str
    depends_on: tuple[str, ...] = ()
    input_refs: tuple[str, ...] = ()
    output_refs: tuple[str, ...] = ()
    task: dict[str, Any] = field(default_factory=dict)
    input_bindings: tuple[dict[str, Any], ...] = ()
    expected: dict[str, Any] = field(default_factory=dict)
    execution_mode: str = "MUTATING"
    required_capability: str | None = None
    selection_policy: dict[str, Any] = field(default_factory=dict)
    resolved_workspace: str | None = None
    resolution_evidence: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PipelineStep":
        if not isinstance(value.get("stepId"), str) or not value["stepId"].strip():
            raise ValueError("PipelineStep requires non-empty stepId.")
        module_id = value.get("moduleId")
        capability = value.get("capability") or value.get("requiredCapability")
        if module_id is not None and (not isinstance(module_id, str) or not module_id.strip()):
            raise ValueError("PipelineStep moduleId must be non-empty when supplied.")
        if not isinstance(capability, str) or not capability.strip():
            raise ValueError("PipelineStep requires capability or requiredCapability.")
        if module_id is None and not isinstance(value.get("requiredCapability"), str):
            raise ValueError("A capability-only step requires requiredCapability.")
        fields = ("dependsOn", "inputRefs", "outputRefs")
        for key in fields:
            values = value.get(key, [])
            if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
                raise ValueError(f"PipelineStep {key} must be an array of non-empty strings.")
        task, expected, bindings = value.get("task", {}), value.get("expected", {}), value.get("inputBindings", [])
        selection, resolution = value.get("selectionPolicy", {}), value.get("resolutionEvidence", {})
        if (not isinstance(task, dict) or not isinstance(expected, dict) or not isinstance(bindings, list)
                or any(not isinstance(b, dict) for b in bindings) or not isinstance(selection, dict)
                or not isinstance(resolution, dict)):
            raise ValueError("PipelineStep task/expected/selection/resolution must be objects and inputBindings an object array.")
        mode = value.get("executionMode", "MUTATING")
        if mode not in {"READ_ONLY", "MUTATING"}:
            raise ValueError("PipelineStep executionMode must be READ_ONLY or MUTATING.")
        return cls(value["stepId"], module_id, capability, tuple(value.get("dependsOn", [])), tuple(value.get("inputRefs", [])), tuple(value.get("outputRefs", [])), task, tuple(bindings), expected, mode, value.get("requiredCapability"), selection, value.get("resolvedWorkspace"), resolution)

    def to_dict(self) -> dict[str, Any]:
        result = {"stepId": self.step_id, "capability": self.capability,
                "dependsOn": list(self.depends_on), "inputRefs": list(self.input_refs), "outputRefs": list(self.output_refs),
                "task": self.task, "inputBindings": list(self.input_bindings), "expected": self.expected,
                "executionMode": self.execution_mode, "selectionPolicy": self.selection_policy,
                "resolutionEvidence": self.resolution_evidence}
        if self.module_id is not None:
            result["moduleId"] = self.module_id
        if self.required_capability:
            result["requiredCapability"] = self.required_capability
        if self.resolved_workspace:
            result["resolvedWorkspace"] = self.resolved_workspace
        return result


@dataclass(frozen=True)
class PipelineConcurrencyPolicy:
    max_global_concurrency: int = 1
    max_module_concurrency: dict[str, int] = field(default_factory=dict)
    workspace_lease_required: bool = True
    max_product_concurrency: int = 16

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "PipelineConcurrencyPolicy":
        value = value or {}
        global_limit = value.get("maxGlobalConcurrency", 1)
        product_limit = value.get("maxProductConcurrency", 16)
        module_limits = value.get("maxModuleConcurrency", {})
        if type(global_limit) is not int or not 1 <= global_limit <= 16:
            raise ValueError("maxGlobalConcurrency must be an integer between 1 and 16.")
        if type(product_limit) is not int or not 1 <= product_limit <= 16:
            raise ValueError("maxProductConcurrency must be an integer between 1 and 16.")
        if not isinstance(module_limits, dict) or any(
            not isinstance(module_id, str) or not module_id.strip() or type(limit) is not int or not 1 <= limit <= 16
            for module_id, limit in module_limits.items()
        ):
            raise ValueError("maxModuleConcurrency must map module IDs to integers between 1 and 16.")
        lease_required = value.get("workspaceLeaseRequired", True)
        if type(lease_required) is not bool:
            raise ValueError("workspaceLeaseRequired must be a boolean.")
        return cls(global_limit, dict(module_limits), lease_required, product_limit)


@dataclass(frozen=True)
class PipelinePlan:
    pipeline_id: str
    steps: tuple[PipelineStep, ...]
    concurrency: PipelineConcurrencyPolicy = field(default_factory=PipelineConcurrencyPolicy)
    goal: str = ""
    workspace: str = ""
    execution_policy: str = "READ_ONLY"
    failure_policy: str = "FAIL_FAST"
    product_id: str | None = None
    template_id: str | None = None
    authorized_product_ids: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PipelinePlan":
        if not isinstance(value, dict) or not isinstance(value.get("pipelineId"), str) or not value["pipelineId"].strip():
            raise ValueError("PipelinePlan requires a non-empty pipelineId.")
        raw_steps = value.get("steps")
        if not isinstance(raw_steps, list):
            raise ValueError("PipelinePlan steps must be an array.")
        steps = tuple(PipelineStep.from_dict(step) for step in raw_steps)
        ids = [step.step_id for step in steps]
        if len(ids) != len(set(ids)):
            raise ValueError("PipelinePlan step IDs must be unique.")
        known = set(ids)
        for step in steps:
            unknown = set(step.depends_on) - known
            if unknown:
                raise ValueError(f"PipelineStep {step.step_id!r} depends on unknown steps: {sorted(unknown)}")
            if step.step_id in step.depends_on:
                raise ValueError(f"PipelineStep {step.step_id!r} cannot depend on itself.")
        concurrency = PipelineConcurrencyPolicy.from_dict(value.get("concurrency"))
        failure_policy = value.get("failurePolicy", "FAIL_FAST")
        if failure_policy not in {"FAIL_FAST", "CONTINUE_INDEPENDENT"}:
            raise ValueError("failurePolicy must be FAIL_FAST or CONTINUE_INDEPENDENT.")
        execution_policy = value.get("execution_policy", "READ_ONLY")
        if execution_policy not in {"READ_ONLY", "WORKSPACE_WRITE"}:
            raise ValueError("Pipeline execution_policy must be READ_ONLY or WORKSPACE_WRITE.")
        product_id = value.get("productId")
        template_id = value.get("templateId")
        if product_id is not None and (not isinstance(product_id, str) or not product_id.strip()):
            raise ValueError("PipelinePlan productId must be non-empty when supplied.")
        if template_id is not None and (not isinstance(template_id, str) or not template_id.strip()):
            raise ValueError("PipelinePlan templateId must be non-empty when supplied.")
        authorized_products = value.get("authorizedProductIds", [])
        if not isinstance(authorized_products, list) or any(not isinstance(item, str) or not item.strip() for item in authorized_products):
            raise ValueError("PipelinePlan authorizedProductIds must be an array of non-empty product IDs.")
        if product_id in authorized_products:
            raise ValueError("PipelinePlan authorizedProductIds must not repeat its own productId.")
        plan = cls(value["pipelineId"], steps, concurrency=concurrency, goal=value.get("goal", ""),
                   workspace=value.get("workspace", ""), execution_policy=execution_policy,
                   failure_policy=failure_policy, product_id=product_id, template_id=template_id,
                   authorized_product_ids=tuple(dict.fromkeys(authorized_products)))
        plan._assert_acyclic()
        return plan

    def to_dict(self) -> dict[str, Any]:
        return {"pipelineId": self.pipeline_id, "goal": self.goal, "workspace": self.workspace,
                "productId": self.product_id, "templateId": self.template_id,
                "authorizedProductIds": list(self.authorized_product_ids),
                "execution_policy": self.execution_policy, "failurePolicy": self.failure_policy,
                "concurrency": {"maxGlobalConcurrency": self.concurrency.max_global_concurrency,
                                "maxProductConcurrency": self.concurrency.max_product_concurrency,
                                "maxModuleConcurrency": self.concurrency.max_module_concurrency,
                                "workspaceLeaseRequired": self.concurrency.workspace_lease_required},
                "steps": [step.to_dict() for step in self.steps],
                "composition_required": len(self.steps) > 1,
                "execution_status": "NOT_RUN", "verification": "dependencies, expected outcomes, artifact hashes and provenance"}

    def _assert_acyclic(self) -> None:
        dependencies = {step.step_id: set(step.depends_on) for step in self.steps}
        completed: set[str] = set()
        while dependencies:
            ready = {step_id for step_id, deps in dependencies.items() if deps.issubset(completed)}
            if not ready:
                raise ValueError("PipelinePlan dependency graph contains a cycle.")
            completed.update(ready)
            for step_id in ready:
                dependencies.pop(step_id)

    def readiness(self, completed_step_ids: set[str] | None = None) -> dict[str, str]:
        completed = completed_step_ids or set()
        return {
            step.step_id: "RUNNABLE" if set(step.depends_on).issubset(completed) else "WAITING_FOR_DEPENDENCIES"
            for step in self.steps if step.step_id not in completed
        }
