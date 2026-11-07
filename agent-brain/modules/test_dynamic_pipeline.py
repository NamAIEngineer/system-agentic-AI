from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from modules.capability_registry import CapabilityProviderRegistry, CapabilityResolutionError, ProviderSelectionPolicy
from modules.pipeline_contracts import PipelinePlan, PipelineStep, ProductArtifact
from modules.pipeline_executor import (FixtureModuleExecutionAdapter, GlobalExecutionBudget,
    PipelineExecutionError, PipelineExecutor, PipelineStore, ProcessRuntimeCoordinator)
from modules.product_contracts import ContentVariant, DistributionIntent, DistributionMetric, MediaArtifact
from modules.workspace_resolver import ModuleRegistry


class DynamicPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def setup_modules(self, count: int, *, common_capability: str | None = None, namespace: str = "default"):
        records, roots, handlers = [], {}, {}
        module_root = self.root / namespace
        module_root.mkdir(exist_ok=True)
        for index in range(count):
            module_id = f"fixture-module-{index:02d}"
            root = module_root / module_id
            root.mkdir()
            roots[module_id] = root
            capability = common_capability or f"capability-{index:02d}"
            records.append({"id": module_id, "name": module_id, "repo": module_id,
                            "workspace_path": str(root), "capabilities": [capability],
                            "invocation_type": "fixture", "status": "READY"})
            handlers[module_id] = lambda step, context: {"status": "COMPLETED", "summary": Path(context["resolvedWorkspace"]).name,
                                                         "product_artifacts": []}
        return ModuleRegistry({"modules": records}), roots, handlers

    @staticmethod
    def plan(pipeline_id, steps, **extra):
        return PipelinePlan.from_dict({"pipelineId": pipeline_id, "goal": "synthetic fixture",
                                       "execution_policy": "WORKSPACE_WRITE", "steps": steps, **extra})

    def test_module_counts_and_capability_only_provider_resolution(self):
        for count in (1, 4, 10, 20):
            with self.subTest(count=count):
                registry, roots, handlers = self.setup_modules(count, namespace=f"count-{count}")
                capability = f"capability-{count - 1:02d}"
                store = PipelineStore(self.root / f"modules-{count}.sqlite3")
                plan = self.plan(f"module-count-{count}", [{"stepId": "step", "requiredCapability": capability}])
                result = PipelineExecutor(FixtureModuleExecutionAdapter(handlers), registry=registry, store=store).run(plan)
                self.assertEqual(result["pipeline"].status, "COMPLETED")
                step = result["steps"][0]
                self.assertEqual(step.module_id, f"fixture-module-{count - 1:02d}")
                self.assertEqual(step.resolution_evidence["resolvedModuleId"], step.module_id)
                self.assertEqual(step.resolution_evidence["requestedCapability"], capability)
                events = result["events"]
                resolved = next(item for item in events if item["event"] == "capability_provider_resolved")
                self.assertEqual(Path(resolved["payload"]["resolvedWorkspace"]), roots[step.module_id].resolve())

    def test_explicit_priority_and_ambiguous_provider_selection(self):
        registry, _, handlers = self.setup_modules(2, common_capability="shared")
        providers = CapabilityProviderRegistry(registry, FixtureModuleExecutionAdapter(handlers).health)
        found = providers.find_providers("shared", execution_mode="MUTATING")
        with self.assertRaisesRegex(CapabilityResolutionError, "Multiple eligible"):
            ProviderSelectionPolicy().select(found)
        selected = ProviderSelectionPolicy("priority", {"fixture-module-00": 1, "fixture-module-01": 4}).select(found)
        self.assertEqual(selected.provider.module_id, "fixture-module-01")
        explicit = ProviderSelectionPolicy().select(found, explicit_module_id="fixture-module-00")
        self.assertEqual(explicit.provider.module_id, "fixture-module-00")
        self.assertTrue(selected.evidence)

    def test_no_provider_fails_before_durable_run(self):
        registry, _, handlers = self.setup_modules(1)
        store = PipelineStore(self.root / "no-provider.sqlite3")
        plan = self.plan("no-provider", [{"stepId": "x", "requiredCapability": "missing"}])
        with self.assertRaisesRegex(PipelineExecutionError, "NO_ELIGIBLE_PROVIDER"):
            PipelineExecutor(FixtureModuleExecutionAdapter(handlers), registry=registry, store=store).run(plan)
        with self.assertRaisesRegex(PipelineExecutionError, "not found"):
            store.inspect("no-provider")

    def test_empty_registry_is_a_valid_zero_module_configuration(self):
        registry = ModuleRegistry({"modules": []})
        self.assertEqual(registry.list_modules(), [])
        self.assertEqual(CapabilityProviderRegistry(registry).find_providers("anything"), [])

    def test_multi_product_runs_and_artifact_provenance_do_not_cross(self):
        registry, roots, _ = self.setup_modules(3, namespace="products")
        module_ids = list(roots)
        handlers = {}
        barrier = threading.Barrier(3)
        for index, module_id in enumerate(module_ids):
            def handler(step, context, index=index, module_id=module_id):
                barrier.wait(timeout=3)
                product_id = context["plan"].product_id
                name = f"{product_id}.txt"
                (Path(context["resolvedWorkspace"]) / name).write_text(product_id, encoding="utf-8")
                artifact = ProductArtifact(f"artifact-{product_id}", module_id, "fixture", (name,), created_at="2026-10-16T00:00:00Z",
                                           producer_step_id=step.step_id, pipeline_id=context["plan"].pipeline_id, product_id=product_id)
                return {"status": "COMPLETED", "product_artifacts": [artifact]}
            handlers[module_id] = handler
        store = PipelineStore(self.root / "products.sqlite3")
        executor = PipelineExecutor(FixtureModuleExecutionAdapter(handlers), registry=registry, store=store,
                                    coordinator=ProcessRuntimeCoordinator(GlobalExecutionBudget(3)))
        plans = [self.plan(f"pipeline-{product}", [{"stepId": "produce", "moduleId": module_ids[i],
                                                     "capability": f"capability-{i:02d}", "outputRefs": [f"artifact-{product}"]}],
                           productId=product)
                 for i, product in enumerate(("product-a", "product-b", "product-c"))]
        results = executor.run_many(plans, max_product_concurrency=3)
        self.assertTrue(all(item["pipeline"].status == "COMPLETED" for item in results))
        products = {item.product_id: item for item in store.product_runs()}
        self.assertEqual(set(products), {"product-a", "product-b", "product-c"})
        for product_id, product in products.items():
            self.assertEqual(product.status, "COMPLETED")
            self.assertEqual(product.artifact_refs, (f"artifact-{product_id}",))
        expected_products = {"pipeline-product-a": "product-a", "pipeline-product-b": "product-b", "pipeline-product-c": "product-c"}
        for result in results:
            artifact = result["artifacts"][0]
            self.assertEqual(artifact["productId"], expected_products[result["pipeline"].pipeline_id])

    def test_content_variant_fanout_then_distribution_handoff(self):
        registry, roots, _ = self.setup_modules(1, namespace="fanout")
        module_id = next(iter(roots))

        def produce(step, context):
            (Path(context["resolvedWorkspace"]) / "base.txt").write_text("base", encoding="utf-8")
            (Path(context["resolvedWorkspace"]) / "clip.bin").write_bytes(b"synthetic-media")
            return {"status": "COMPLETED", "product_artifacts": [ProductArtifact(
                "base", module_id, "specification", ("base.txt",), created_at="now", producer_step_id="source",
                pipeline_id="fanout", product_id="product-x")], "media_artifacts": [MediaArtifact(
                "media-1", "product-x", "fanout", module_id, "source", "synthetic-binary", "clip.bin", created_at="now")]}

        def variants(step, context):
            return {"status": "COMPLETED", "content_variants": [ContentVariant(
                f"variant-{i}", "product-x", "fanout", ("base",), (), f"format-{i}", "angle", "audience", (),
                f"text-{i}", ("media-1",), provenance={"source": "fixture"}, producer_module_id=module_id,
                producer_step_id="variants", created_at="now") for i in range(3)]}

        def handoff(step, context):
            inputs = [item["artifact"]["artifactId"] for item in context["inputArtifacts"]]
            self.assertEqual(set(inputs), {"variant-0", "variant-1", "variant-2", "media-1"})
            return {"status": "COMPLETED", "distribution_intents": [DistributionIntent(
                "intent-x", "product-x", ("variant-0", "variant-1", "variant-2"), ("media-1",), "fixture objective", created_at="now", pipeline_id="fanout",
                producer_module_id=module_id, producer_step_id="handoff")]}

        plan = self.plan("fanout", [
            {"stepId": "source", "moduleId": module_id, "capability": "capability-00", "outputRefs": ["base", "media-1"]},
            {"stepId": "variants", "moduleId": module_id, "capability": "capability-00", "dependsOn": ["source"],
             "inputRefs": ["base", "media-1"], "outputRefs": ["variant-0", "variant-1", "variant-2"]},
            {"stepId": "handoff", "moduleId": module_id, "capability": "capability-00", "dependsOn": ["variants"],
             "inputRefs": ["variant-0", "variant-1", "variant-2", "media-1"], "outputRefs": ["intent-x"]},
        ], productId="product-x")
        result = PipelineExecutor(FixtureModuleExecutionAdapter({module_id: lambda step, context: {
            "source": produce, "variants": variants, "handoff": handoff}[step.step_id](step, context)}),
            registry=registry, store=PipelineStore(self.root / "fanout.sqlite3")).run(plan)
        self.assertEqual(result["pipeline"].status, "COMPLETED", [step.error for step in result["steps"]])
        self.assertEqual(len([x for x in result["artifacts"] if x["artifactKind"] == "ContentVariant"]), 3)
        self.assertEqual(len([x for x in result["artifacts"] if x["artifactKind"] == "MediaArtifact"]), 1)
        intent = next(x for x in result["artifacts"] if x["artifactKind"] == "DistributionIntent")
        self.assertEqual(intent["contentVariantRefs"], ["variant-0", "variant-1", "variant-2"])
        self.assertEqual(intent["mediaRefs"], ["media-1"])

    def test_executor_core_does_not_name_registered_domain_modules(self):
        source = (Path(__file__).with_name("pipeline_executor.py")).read_text(encoding="utf-8")
        for forbidden in ("private-domain-module", "private-product-adapter", "vibecode"):
            self.assertNotIn(forbidden, source)

    def test_media_and_distribution_metric_contracts_are_extensible(self):
        media = MediaArtifact.from_dict({"mediaArtifactId": "m1", "productId": "p1", "pipelineId": "pipe",
            "producerModuleId": "m", "producerStepId": "s", "mediaType": "future-custom-media",
            "fileRef": "media.bin", "createdAt": "now"})
        self.assertEqual(media.media_type, "future-custom-media")
        metric = DistributionMetric.from_dict({"productId": "p1", "campaignId": "c", "contentVariantId": "v",
            "platform": "future-platform", "observedAt": "now", "metrics": {"customMetric": {"value": 3}}})
        self.assertEqual(metric.metrics["customMetric"]["value"], 3)


if __name__ == "__main__":
    unittest.main()
