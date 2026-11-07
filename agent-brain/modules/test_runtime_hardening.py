from __future__ import annotations

import tempfile
import sys
import threading
import time
import unittest
from pathlib import Path

from modules.capability_registry import CapabilityProviderRegistry, ModuleHealth
from modules.pipeline_contracts import PipelinePlan, ProductArtifact
from modules.pipeline_executor import (FixtureModuleExecutionAdapter, GlobalExecutionBudget,
    PipelineExecutionError, PipelineExecutor, PipelineStore, ProcessRuntimeCoordinator,
    PROCESS_RUNTIME_COORDINATOR)
from modules.product_contracts import ProductRun
from modules.workspace_resolver import ModuleExecutionContext, ModuleRegistry, WorkspaceResolver
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "products/agent-runtime"))
from local_agent import execute_tool


class DynamicRuntimeHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.roots = {}
        for name in ("module-a", "module-b"):
            self.roots[name] = self.root / name
            self.roots[name].mkdir()
        self.registry = ModuleRegistry({"modules": [
            {"id": name, "name": name, "repo": name, "workspace_path": str(root),
             "capabilities": ["fixture"], "invocation_type": "fixture", "status": "READY",
             "input_artifact_types": ["fixture-file"], "output_artifact_types": ["fixture-file"]}
            for name, root in self.roots.items()
        ]})

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, pipeline_id, step, *, product_id="product-a", **extra):
        return PipelinePlan.from_dict({"pipelineId": pipeline_id, "productId": product_id,
            "goal": "safe runtime fixture", "execution_policy": "WORKSPACE_WRITE",
            "concurrency": {"maxGlobalConcurrency": 4, "maxModuleConcurrency": {"module-a": 4, "module-b": 4}},
            "steps": [step], **extra})

    def test_global_budget_is_shared_across_executor_instances_and_run_many(self):
        budget = GlobalExecutionBudget(1)
        coordinator = ProcessRuntimeCoordinator(budget)
        active = peak = 0
        lock = threading.Lock()
        first_entered = threading.Event()

        def handler(step, context):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                first_entered.set()
            time.sleep(0.08)
            with lock:
                active -= 1
            return {"status": "COMPLETED", "product_artifacts": []}

        adapter = FixtureModuleExecutionAdapter({"module-a": handler, "module-b": handler})
        executor1 = PipelineExecutor(adapter, registry=self.registry, store=PipelineStore(self.root / "a.sqlite"), coordinator=coordinator)
        executor2 = PipelineExecutor(adapter, registry=self.registry, store=PipelineStore(self.root / "b.sqlite"), coordinator=coordinator)
        plans = [self.plan("global-one", {"stepId": "a", "moduleId": "module-a", "capability": "fixture"}),
                 self.plan("global-two", {"stepId": "b", "moduleId": "module-b", "capability": "fixture"})]
        outcomes = []
        t1 = threading.Thread(target=lambda: outcomes.append(executor1.run(plans[0])))
        t2 = threading.Thread(target=lambda: outcomes.append(executor2.run_many([plans[1]], max_product_concurrency=2)))
        t1.start(); t2.start()
        t1.join(3); t2.join(3)
        self.assertFalse(t1.is_alive() or t2.is_alive())
        self.assertEqual(peak, 1)
        self.assertEqual(budget.inspect()["maxGlobalSteps"], 1)
        self.assertEqual(budget.inspect()["activeGlobalSteps"], 0)
        self.assertEqual(budget.inspect()["activeProductSteps"], {})
        self.assertEqual(budget.inspect()["activeModuleSteps"], {})

    def test_default_executors_share_the_process_coordinator(self):
        adapter = FixtureModuleExecutionAdapter({"module-a": lambda s,c: {"status":"COMPLETED","product_artifacts":[]}})
        first = PipelineExecutor(adapter, registry=self.registry, store=PipelineStore(self.root / "default-a.sqlite"))
        second = PipelineExecutor(adapter, registry=self.registry, store=PipelineStore(self.root / "default-b.sqlite"))
        self.assertIs(first.coordinator, PROCESS_RUNTIME_COORDINATOR)
        self.assertIs(second.coordinator, PROCESS_RUNTIME_COORDINATOR)
        self.assertIs(first.global_budget, second.global_budget)

    def test_module_limit_is_shared_across_executor_instances(self):
        budget = GlobalExecutionBudget(4)
        coordinator = ProcessRuntimeCoordinator(budget)
        active = peak = 0
        lock = threading.Lock()

        def handler(step, context):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.06)
            with lock:
                active -= 1
            return {"status": "COMPLETED", "product_artifacts": []}

        adapter = FixtureModuleExecutionAdapter({"module-a": handler})
        first = PipelineExecutor(adapter, registry=self.registry, store=PipelineStore(self.root / "module-a.sqlite"), coordinator=coordinator)
        second = PipelineExecutor(adapter, registry=self.registry, store=PipelineStore(self.root / "module-b.sqlite"), coordinator=coordinator)
        policy = {"maxGlobalConcurrency": 4, "maxProductConcurrency": 4, "maxModuleConcurrency": {"module-a": 1}}
        p1 = self.plan("module-limit-one", {"stepId": "a", "moduleId": "module-a", "capability": "fixture"}, product_id="product-a", concurrency=policy)
        p2 = self.plan("module-limit-two", {"stepId": "b", "moduleId": "module-a", "capability": "fixture"}, product_id="product-b", concurrency=policy)
        t1 = threading.Thread(target=first.run, args=(p1,))
        t2 = threading.Thread(target=second.run, args=(p2,))
        t1.start(); t2.start(); t1.join(3); t2.join(3)
        self.assertFalse(t1.is_alive() or t2.is_alive())
        self.assertEqual(peak, 1)

    def test_workspace_lease_is_shared_across_different_pipeline_stores(self):
        active = peak = 0
        lock = threading.Lock()

        def handler(step, context):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.12)
            with lock:
                active -= 1
            return {"status": "COMPLETED", "product_artifacts": []}

        coordinator = ProcessRuntimeCoordinator(GlobalExecutionBudget(2))
        adapter = FixtureModuleExecutionAdapter({"module-a": handler})
        first = PipelineExecutor(adapter, registry=self.registry,
                                 store=PipelineStore(self.root / "workspace-one.sqlite"), coordinator=coordinator)
        second = PipelineExecutor(adapter, registry=self.registry,
                                  store=PipelineStore(self.root / "workspace-two.sqlite"), coordinator=coordinator)
        self.assertIs(first.leases, second.leases)
        policy = {"maxGlobalConcurrency": 2, "maxProductConcurrency": 2,
                  "maxModuleConcurrency": {"module-a": 2}}
        plans = [self.plan("workspace-one", {"stepId": "a", "moduleId": "module-a", "capability": "fixture"},
                           product_id="product-a", concurrency=policy),
                 self.plan("workspace-two", {"stepId": "b", "moduleId": "module-a", "capability": "fixture"},
                           product_id="product-b", concurrency=policy)]
        outcomes = []
        threads = [threading.Thread(target=lambda executor=executor, plan=plan:
                                    outcomes.append(executor.run(plan)))
                   for executor, plan in ((first, plans[0]), (second, plans[1]))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(peak, 1)
        self.assertEqual(len(outcomes), 2)
        self.assertTrue(all(result["pipeline"].status == "COMPLETED" for result in outcomes),
                        [result["pipeline"].status for result in outcomes])
        self.assertEqual(first.leases.inspect(), [])

    def test_cross_pipeline_artifact_handoff_uses_store_and_blocks_cross_product(self):
        store = PipelineStore(self.root / "artifacts.sqlite")
        produced = []

        def produce(step, context):
            (self.roots["module-a"] / "asset.txt").write_text("fixture", encoding="utf-8")
            return {"status": "COMPLETED", "product_artifacts": [ProductArtifact(
                "artifact-a", "module-a", "fixture-file", ("asset.txt",), created_at="2026-10-16T00:00:00Z",
                producer_step_id=step.step_id, pipeline_id=context["plan"].pipeline_id,
                product_id=context["plan"].product_id)]}

        def consume(step, context):
            produced.extend(context["inputArtifacts"])
            return {"status": "COMPLETED", "product_artifacts": []}

        adapter = FixtureModuleExecutionAdapter({"module-a": produce, "module-b": consume})
        coordinator = ProcessRuntimeCoordinator(GlobalExecutionBudget(2))
        PipelineExecutor(adapter, registry=self.registry, store=store, coordinator=coordinator).run(
            self.plan("pipeline-a", {"stepId": "produce", "moduleId": "module-a", "capability": "fixture", "outputRefs": ["artifact-a"]}))
        record = store.artifact_store.get_artifact("artifact-a")
        self.assertEqual(record["artifact"]["productId"], "product-a")
        with self.assertRaisesRegex(PipelineExecutionError, "Cross-product"):
            PipelineExecutor(adapter, registry=self.registry, store=store, coordinator=coordinator).run(
                self.plan("pipeline-b-cross-product", {"stepId": "consume", "moduleId": "module-b", "capability": "fixture", "inputRefs": ["artifact-a"]}, product_id="product-b"))
        result = PipelineExecutor(adapter, registry=self.registry, store=store, coordinator=coordinator).run(
            self.plan("pipeline-b", {"stepId": "consume", "moduleId": "module-b", "capability": "fixture", "inputRefs": ["artifact-a"]}))
        self.assertEqual(result["pipeline"].status, "COMPLETED")
        self.assertEqual(produced[0]["artifact"]["artifactId"], "artifact-a")
        self.assertEqual(produced[0]["artifact"]["pipelineId"], "pipeline-a")
        self.assertTrue(produced[0]["sha256"])

    def test_explicit_product_relation_allows_artifact_reuse_and_integrity_is_rechecked(self):
        store = PipelineStore(self.root / "authorized.sqlite")
        def producer(step, context):
            (self.roots["module-a"] / "asset.txt").write_text("original", encoding="utf-8")
            return {"status": "COMPLETED", "product_artifacts": [ProductArtifact(
                "shareable", "module-a", "fixture-file", ("asset.txt",), created_at="now",
                producer_step_id=step.step_id, pipeline_id=context["plan"].pipeline_id,
                product_id=context["plan"].product_id)]}
        adapter = FixtureModuleExecutionAdapter({"module-a": producer, "module-b": lambda s,c: {"status":"COMPLETED","product_artifacts":[]}})
        coord = ProcessRuntimeCoordinator(GlobalExecutionBudget(2))
        PipelineExecutor(adapter, registry=self.registry, store=store, coordinator=coord).run(
            self.plan("producer", {"stepId":"p","moduleId":"module-a","capability":"fixture","outputRefs":["shareable"]}))
        stored = store.artifact_store.get_artifact("shareable")
        with self.assertRaisesRegex(PipelineExecutionError, "already exists"):
            store.artifact_store.put_artifact(stored["artifact"], producer_workspace=stored["producerWorkspace"],
                                              file_manifest=stored["fileManifest"])
        store.create_product_run(ProductRun("product-b", "explicit fixture consumer", created_at="2026-10-16T00:00:00Z"))
        store.authorize_product_relation("product-a", "product-b", "synthetic shared fixture reference")
        plan = self.plan("consumer-shared", {"stepId":"c","moduleId":"module-b","capability":"fixture","inputRefs":["shareable"]}, product_id="product-b", authorizedProductIds=["product-a"])
        result = PipelineExecutor(adapter, registry=self.registry, store=store, coordinator=coord).run(plan)
        self.assertEqual(result["pipeline"].status, "COMPLETED")
        (self.roots["module-a"] / "asset.txt").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(PipelineExecutionError, "changed after production"):
            store.artifact_store.validate_reference("shareable", product_id="product-b", authorized_product_ids=("product-a",))

    def test_module_context_is_canonical_and_rejects_nested_target_prefix(self):
        module_root = self.roots["module-a"]
        module = ModuleExecutionContext.resolve("module-a", "fixture", resolver=WorkspaceResolver(self.registry))
        self.assertEqual(Path(module.resolved_workspace), module_root.resolve())
        with self.assertRaisesRegex(ValueError, "does not match canonical"):
            ModuleExecutionContext.validate({**module.to_dict(), "resolvedWorkspace": str(module_root / "module-a")}, module_root,
                                            resolver=WorkspaceResolver(self.registry))
        result = execute_tool(module_root, "list_files", '{"path":"module-a/src"}',
                             mutations_allowed=False, module_execution_context=module.to_dict())
        self.assertIn("NESTED_MODULE_PATH_FORBIDDEN", result)
        assigned_root = self.root / "downstream-distribution"
        assigned_root.mkdir()
        result = execute_tool(assigned_root, "list_files", '{"path":"downstream-distribution/src"}',
            mutations_allowed=False,
            workspace_context={"assignedWorkspace": str(assigned_root), "workspaceAuthority": "SYSTEM_AGENTIC_QUEUE"})
        self.assertIn("DUPLICATED_WORKSPACE_ROOT_FORBIDDEN", result)

    def test_module_health_excludes_unavailable_provider(self):
        health = lambda module_id: ModuleHealth.create(module_id, "UNAVAILABLE", {"reason": "fixture offline"})
        providers = CapabilityProviderRegistry(self.registry, health)
        found = providers.find_providers("fixture")
        self.assertTrue(found)
        self.assertTrue(all(not item.availability and item.health == "UNAVAILABLE" for item in found))


if __name__ == "__main__":
    unittest.main()
