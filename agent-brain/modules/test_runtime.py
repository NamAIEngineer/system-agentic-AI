from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from modules.runtime import (
    GSTACK_REVIEW_REQUIRED_PHASES,
    _discover_bun,
    _is_gstack_review_scratch,
    _gstack_runtime_root,
    _gate_scan_evidence,
    _summary_json_line,
    ModuleUnavailable,
    _gstack_skill_status,
    _skill_frontmatter_name,
    load_module_registry,
    module_candidates,
    module_inventory,
    normalize_module_result,
    prepare_module_task,
    run_knowledge_builder_workflow,
)
from providers.runtime import ProviderResult, ProviderTask, QueueProviderAdapter, adapter_for


ROOT = Path(__file__).resolve().parents[2]


def _synthetic_scratch_workspace() -> Path:
    workspace = Path(tempfile.mkdtemp(prefix="system-agentic-gstack-review-fixture-"))
    (workspace / ".system-agentic-synthetic-review").write_text(
        "system-agentic synthetic review workspace v1\n", encoding="utf-8"
    )
    gitdir = workspace / ".gstack-review-state" / "git"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (workspace / ".git").write_text("gitdir: .gstack-review-state/git\n", encoding="utf-8")
    return workspace


class ModuleRegistryTests(unittest.TestCase):
    def test_registered_domain_modules_and_separate_optimization_layer(self) -> None:
        modules = load_module_registry()["modules"]
        self.assertEqual(
            {module["id"] for module in modules},
            {
                "system-one", "orchestration", "gstack", "pedagogy", "knowledge-builder",
                "rea", "agent-desktop",
            },
        )
        for module in modules:
            for field in ("id", "repo", "enabled", "available", "capabilities", "task_types", "invocation_type", "adapter_status", "requires_provider", "verification"):
                self.assertIn(field, module)
            self.assertIsNone(module["available"], "registry defers dynamic availability to runtime")
        executable = [module["id"] for module in modules if module["adapter_status"] == "implemented"]
        self.assertEqual(executable, ["agent-desktop", "gstack", "pedagogy", "knowledge-builder", "rea"])
        from modules.runtime import optimization_layer_inventory
        layers = optimization_layer_inventory()
        self.assertEqual([layer["id"] for layer in layers], ["caveman"])
        self.assertEqual(layers[0]["type"], "optimization-layer")
        self.assertEqual(layers[0]["capabilities"], ["agent.output-shrink"])
        inventory = module_inventory()
        self.assertTrue(all(type(module["available"]) is bool for module in inventory))
        self.assertTrue(all(module["execution_ready"] is False for module in inventory if module["adapter_status"] != "implemented"))

    def test_candidate_queries_match_registered_task_types_and_capabilities(self) -> None:
        by_review = module_candidates(task_type="software.code-review", capabilities=set())
        self.assertEqual([module["id"] for module in by_review], ["gstack"])
        by_pedagogy = module_candidates(capabilities={"pedagogy.rehearse"})
        self.assertEqual([module["id"] for module in by_pedagogy], ["pedagogy"])
        by_knowledge = module_candidates(task_type="source-validation", capabilities=set())
        self.assertEqual([module["id"] for module in by_knowledge], ["knowledge-builder"])
        self.assertEqual(module_candidates(task_type="ordinary-software-task", capabilities=set()), [])
        self.assertEqual(module_candidates(capabilities={"software.publish"}), [])
        self.assertEqual(module_candidates(capabilities={"task.submit"}), [])
        self.assertEqual(module_candidates(task_type="software.code-review", capabilities={"education.rehearsal"}), [])
        with self.assertRaisesRegex(ValueError, "task type or at least one capability"):
            module_candidates()

    def test_host_skill_routes_natural_code_review_and_excludes_other_software_tasks(self) -> None:
        skill = (ROOT / "agent-brain/skills/system-agentic-ai/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("natural-language code review request", skill)
        self.assertIn("modules candidates --task-type software.code-review", skill)
        self.assertIn("dispatch-module --module gstack --capability software.code-review", skill)
        self.assertIn("For software tasks that are not code review, use ordinary provider dispatch", skill)
        self.assertIn("knowledge-pack", skill)
        self.assertIn("dispatch-module --module knowledge-builder --capability knowledge.pack-compile", skill)
        self.assertIn("local providers", skill)

    def test_knowledge_builder_task_uses_local_provider_and_new_workspace_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            (workspace / "source.md").write_text("Observed: local fixture source.\n", encoding="utf-8")
            task = ProviderTask(
                workspace,
                "Compile source.md into a validated agent knowledge pack.",
                execution_policy="WORKSPACE_WRITE",
                context={"knowledge_sources": ["source.md"]},
            )
            prepared = prepare_module_task(task, "knowledge-builder", "knowledge.pack-compile")
            module = prepared.execution_context["module"]
            workflow = module["knowledge_workflow"]
            self.assertEqual(module["capability"], "knowledge.pack-compile")
            self.assertEqual(prepared.provider_policy["allowed_providers"], ["local-qwen"])
            self.assertEqual(prepared.execution_policy, "WORKSPACE_WRITE")
            self.assertEqual(workflow["execution_policy"], "WORKSPACE_WRITE")
            self.assertTrue(Path(workflow["output_root"]).is_relative_to(workspace.resolve()))
            self.assertFalse(Path(workflow["output_root"]).exists())
            self.assertTrue(any("verify --claims" in item for item in prepared.constraints))
            self.assertTrue(any("does not ingest Markdown" in item for item in prepared.constraints))

    def test_knowledge_builder_refuses_overwrite_and_workspace_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            existing = workspace / "existing"
            existing.mkdir()
            base = ProviderTask(workspace, "Compile.", execution_policy="WORKSPACE_WRITE")
            with self.assertRaisesRegex(ModuleUnavailable, "refusing to overwrite"):
                prepare_module_task(
                    replace(base, context={"knowledge_output": str(existing)}),
                    "knowledge-builder", "knowledge.pack-compile",
                )
            with self.assertRaisesRegex(ModuleUnavailable, "contained in the declared workspace"):
                prepare_module_task(
                    replace(base, context={"knowledge_output": str(Path(temp) / "outside")}),
                    "knowledge-builder", "knowledge.pack-compile",
                )

    def test_knowledge_validation_uses_existing_pack_with_read_only_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            pack = workspace / "existing-pack"
            pack.mkdir()
            task = ProviderTask(
                workspace, "Validate the existing pack.", execution_policy="READ_ONLY",
                context={"knowledge_output": "existing-pack"},
            )
            prepared = prepare_module_task(task, "knowledge-builder", "knowledge.validation")
            workflow = prepared.execution_context["module"]["knowledge_workflow"]
            self.assertEqual(prepared.execution_policy, "READ_ONLY")
            self.assertFalse(workflow["write_index"])
            self.assertEqual(workflow["workflow"], ["lint", "verify_claims"])
            self.assertEqual(workflow["output_root"], str(pack.resolve()))

    def test_knowledge_result_rejects_artifacts_outside_declared_pack_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            output = workspace / "pack"
            output.mkdir()
            outside_artifact = workspace / "unrelated.md"
            outside_artifact.write_text("unrelated", encoding="utf-8")
            payload = {
                "execution_status": "COMPLETED", "validation_status": "NOT_RUN",
                "sources": [{"uri": "source", "sha256": "a" * 64}],
                "artifacts": [{"path": str(outside_artifact)}], "warnings": [], "errors": [],
            }
            module = {"id": "knowledge-builder", "knowledge_workflow": {"output_root": str(output), "workspace": str(workspace), "write_index": True}}
            result = ProviderResult(
                "task-2", "local-qwen", None, "COMPLETED", "KNOWLEDGE_BUILDER_RESULT=" + json.dumps(payload),
                (), (), (), None, None, "task-2", "worker", ("task.submit",), "fixture",
                execution_policy="WORKSPACE_WRITE", execution_context={"module": module},
            )
            normalized = normalize_module_result(result)
            self.assertEqual(normalized.status, "PARTIAL")
            self.assertEqual(normalized.artifacts, ())
            self.assertIn("outside the declared Knowledge Builder output root", normalized.execution_context["module"]["knowledge_workflow"]["normalization_error"])

    def test_knowledge_result_normalization_separates_execution_from_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            output = workspace / "pack"
            output.mkdir()
            (output / "node.md").write_text("UKMC fixture\n", encoding="utf-8")
            (output / "index.json").write_text("[]\n", encoding="utf-8")
            source = {"uri": "_evidence/run/sources/source.md", "sha256": "a" * 64, "trust_tier": "draft", "provenance": "local fixture"}
            result_payload = {
                "execution_status": "SUCCESS", "validation_status": "VALID", "sources": [source],
                "artifacts": [{"path": str(output / "node.md")}], "warnings": [], "errors": [],
            }
            module = {"id": "knowledge-builder", "knowledge_workflow": {"output_root": str(output), "workspace": str(workspace), "write_index": True}}
            result = ProviderResult(
                "task-1", "local-qwen", None, "COMPLETED",
                "KNOWLEDGE_BUILDER_RESULT=" + json.dumps(result_payload), (), (), (), None, None,
                "task-1", "worker", ("task.submit",), "fixture", execution_policy="WORKSPACE_WRITE",
                execution_context={"workspace": str(workspace), "module": module},
            )
            with patch("modules.runtime._knowledge_builder_executable", return_value=Path("/bin/true")), patch(
                "modules.runtime.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="files: 1\n", stderr="")
            ) as run_cli:
                valid = normalize_module_result(run_knowledge_builder_workflow(result, include_index=True))
                self.assertEqual(len(run_cli.call_args_list), 3)
            workflow = valid.execution_context["module"]["knowledge_workflow"]
            self.assertEqual(valid.status, "COMPLETED")
            self.assertEqual(workflow["execution_status"], "COMPLETED")
            self.assertEqual(workflow["validation_status"], "VALID")
            self.assertEqual(workflow["sources"], [source])
            self.assertEqual(valid.artifacts[0]["path"], str((output / "node.md").resolve()))
            self.assertEqual(valid.artifacts[1]["type"], "knowledge-index")

            with patch("modules.runtime._knowledge_builder_executable", return_value=Path("/bin/true")), patch(
                "modules.runtime.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="failures: 1\n", stderr="[ERROR] source hash mismatch")
            ):
                invalid = normalize_module_result(run_knowledge_builder_workflow(result, include_index=True))
            invalid_workflow = invalid.execution_context["module"]["knowledge_workflow"]
            self.assertEqual(invalid.status, "COMPLETED")
            self.assertEqual(invalid_workflow["execution_status"], "COMPLETED")
            self.assertEqual(invalid_workflow["validation_status"], "INVALID")

            with patch("modules.runtime._knowledge_builder_executable", return_value=Path("/bin/true")), patch(
                "modules.runtime.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
            ) as status_run:
                run_knowledge_builder_workflow(result, include_index=False)
            self.assertEqual(len(status_run.call_args_list), 2)
            self.assertTrue(all("index" not in call.args[0] for call in status_run.call_args_list))

    def test_non_executable_registration_is_visible_but_execution_is_refused(self) -> None:
        candidate = module_candidates(capabilities={"agent.dispatch-design"})[0]
        self.assertEqual(candidate["id"], "orchestration")
        self.assertEqual(candidate["adapter_status"], "not_implemented")
        self.assertFalse(candidate["execution_ready"])
        with self.assertRaises(ModuleUnavailable):
            prepare_module_task(ProviderTask(ROOT, "Plan delegated work."), "orchestration", "agent.dispatch-design")

    def test_disabled_or_unavailable_module_cannot_execute(self) -> None:
        task = ProviderTask(ROOT, "Review the current diff.")
        ready = next(module for module in module_inventory() if module["id"] == "gstack")
        for field, value in (("enabled", False), ("available", False), ("execution_ready", False)):
            record = dict(ready)
            record[field] = value
            with self.subTest(field=field), patch("modules.runtime.module_inventory", return_value=[record]):
                with self.assertRaises(ModuleUnavailable):
                    prepare_module_task(task, "gstack", "software.code-review")

    def test_gstack_write_policy_is_capability_scoped_to_marked_synthetic_scratch(self) -> None:
        workspace = _synthetic_scratch_workspace()
        try:
            task = ProviderTask(
                workspace, "Review this synthetic fixture.", execution_policy="WORKSPACE_WRITE",
                context={"gstack_review_mode": "full", "gstack_review_workspace": "synthetic-scratch-v1"},
            )
            prepared = prepare_module_task(task, "gstack", "software.code-review")
        finally:
            shutil.rmtree(workspace)
        self.assertEqual(prepared.execution_policy, "WORKSPACE_WRITE")
        workflow = prepared.execution_context["module"]["review_workflow"]
        self.assertEqual(workflow["mode"], "full")
        self.assertEqual(workflow["required_phases"], list(GSTACK_REVIEW_REQUIRED_PHASES))
        self.assertEqual(
            workflow["write_boundary"],
            "WORKSPACE_WRITE is confined to .gstack-review-state/ and .tmp/ in this attested synthetic scratch workspace; "
            "Git metadata writes (including fetch/FETCH_HEAD) go only under .gstack-review-state/git/ via GIT_DIR; "
            "gstack-wtree fingerprint objects go under the separate .gstack-review-state/wtree-objects/ directory; "
            ".git is a read-only pointer file; source/fixture content is report-only and unchanged; "
            "no remote writes or writes outside these directories",
        )
        constraints = "\n".join(prepared.constraints)
        self.assertIn("GSTACK_SESSION_KIND=spawned", constraints)
        self.assertNotIn("GSTACK_SESSION_KIND=unattended", constraints)
        self.assertIn("Report-only review: do not edit, create, delete, or reformat fixture/source files", constraints)
        self.assertIn("do not apply or request fixes", constraints)
        self.assertIn("WORKSPACE_WRITE is confined to .gstack-review-state/ and .tmp/", constraints)
        self.assertIn(".git file is a read-only pointer to .gstack-review-state/git", constraints)
        self.assertIn("including startup, fetch, diff, gates, QA, and review bookkeeping", constraints)
        self.assertIn("GSTACK_REVIEW_RESULT=<JSON object>", constraints)
        self.assertIn("candidate (the full <merge-base-short>+<working-tree-fingerprint-short> value copied unchanged", constraints)
        self.assertIn("fingerprint (the final hex suffix of candidate, separately when possible", constraints)
        self.assertIn("Do not push, alter remote state, write source/fixture content, or write elsewhere", constraints)
        self.assertIn("write source/fixture content", constraints)
        self.assertIn("Git metadata and gstack state stay under .gstack-review-state/", constraints)
        self.assertIn("before the gate scan, create .gstack-review-state/wtree-objects/ under the scratch state root", constraints)
        self.assertIn("GSTACK_WTREE_OBJECT_DIR=\"$PWD/.gstack-review-state/wtree-objects\"", constraints)
        self.assertIn("separate from .gstack-review-state/git", constraints)
        self.assertIn("temporary writes stay under .tmp/", constraints)

        with self.assertRaisesRegex(ModuleUnavailable, "attested synthetic scratch"):
            prepare_module_task(
                ProviderTask(ROOT, "Review project.", execution_policy="WORKSPACE_WRITE"),
                "gstack", "software.code-review",
            )
        with self.assertRaisesRegex(ValueError, "not registered"):
            prepare_module_task(task, "gstack", "task.submit")

    def test_full_gstack_review_fails_closed_without_attested_scratch_or_write_policy(self) -> None:
        full_context = {"gstack_review_mode": "full", "gstack_review_workspace": "synthetic-scratch-v1"}
        with self.assertRaisesRegex(ModuleUnavailable, "dedicated synthetic scratch"):
            prepare_module_task(ProviderTask(ROOT, "Review.", context=full_context), "gstack", "software.code-review")
        workspace = Path(tempfile.mkdtemp(prefix="unmarked-name-"))
        try:
            (workspace / ".system-agentic-synthetic-review").write_text(
                "system-agentic synthetic review workspace v1\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ModuleUnavailable, "attested synthetic scratch"):
                prepare_module_task(
                    ProviderTask(workspace, "Review.", execution_policy="WORKSPACE_WRITE", context=full_context),
                    "gstack", "software.code-review",
                )
        finally:
            shutil.rmtree(workspace)
        workspace = _synthetic_scratch_workspace()
        try:
            with self.assertRaisesRegex(ModuleUnavailable, "explicit WORKSPACE_WRITE"):
                prepare_module_task(ProviderTask(workspace, "Review.", context=full_context), "gstack", "software.code-review")
        finally:
            shutil.rmtree(workspace)

    def test_scratch_attestation_requires_exact_non_symlink_git_pointer(self) -> None:
        workspace = _synthetic_scratch_workspace()
        external_gitdir = Path(tempfile.mkdtemp(prefix="external-gitdir-"))
        try:
            valid_task = ProviderTask(
                workspace, "Review.", execution_policy="WORKSPACE_WRITE",
                context={"gstack_review_mode": "full", "gstack_review_workspace": "synthetic-scratch-v1"},
            )
            self.assertTrue(_is_gstack_review_scratch(valid_task))

            root_link = workspace.parent / f"system-agentic-gstack-review-link-{workspace.name}"
            root_link.symlink_to(workspace, target_is_directory=True)
            linked_task = ProviderTask(
                root_link, "Review.", execution_policy="WORKSPACE_WRITE",
                context={"gstack_review_workspace": "synthetic-scratch-v1"},
            )
            self.assertFalse(_is_gstack_review_scratch(linked_task), "a symlinked workspace root must be refused")
            root_link.unlink()

            pointer = workspace / ".git"
            pointer.unlink()
            pointer.mkdir()
            self.assertFalse(_is_gstack_review_scratch(valid_task), "a .git directory must be refused")
            pointer.rmdir()
            pointer.write_text(f"gitdir: {external_gitdir}\n", encoding="utf-8")
            self.assertFalse(_is_gstack_review_scratch(valid_task), "an external gitdir must be refused")
            pointer.unlink()
            target = workspace / "git-pointer-target"
            target.write_text("gitdir: .gstack-review-state/git\n", encoding="utf-8")
            pointer.symlink_to(target)
            self.assertFalse(_is_gstack_review_scratch(valid_task), "a symlinked .git pointer must be refused")
        finally:
            shutil.rmtree(workspace)
            shutil.rmtree(external_gitdir)

    def test_non_gstack_module_policy_and_result_routing_are_unchanged(self) -> None:
        self.assertEqual(ProviderTask(ROOT, "Plan.").execution_policy, "READ_ONLY")
        result = ProviderResult(
            "non-gstack", "codex", None, "COMPLETED", "done", (), (), (), None, None,
            "non-gstack", "worker", ("task.submit",), "test", execution_context={"module": {"id": "orchestration"}},
        )
        self.assertIs(normalize_module_result(result), result)

    def test_optimizer_applies_only_after_supported_module_result_normalization(self) -> None:
        from modules.runtime import optimize_module_result
        from unittest.mock import patch
        for module_id, expected in (("gstack", True), ("knowledge-builder", True), ("orchestration", False)):
            result = ProviderResult(
                f"task-{module_id}", "codex", None, "COMPLETED", "summary", (), (), (), None, None,
                f"job-{module_id}", "worker", ("task.submit",), "test",
                execution_context={"module": {"id": module_id}},
            )
            with patch("modules.runtime.optimize_provider_summary", wraps=lambda value, **_: value) as optimize:
                returned = optimize_module_result(result)
                self.assertEqual(optimize.called, expected)
                self.assertIs(returned, result)


class GstackAdapterTests(unittest.TestCase):
    def test_empty_working_gate_scan_is_canonicalized_to_not_applicable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            state_root = workspace / ".gstack-review-state"
            state_root.mkdir()
            artifact = state_root / "gate.jsonl"
            artifact.write_text("", encoding="utf-8")
            payload = {
                "status": "WORKING", "listed": 0, "eligible": 0, "inspected": 0,
                "unread": 0, "unmatched": 2, "candidate": "b" * 12 + "+" + "a" * 40,
                "fingerprint": "a" * 40, "artifact": str(artifact), "artifact_bytes": 0,
            }

            gate, error = _gate_scan_evidence(
                "GATE_SUMMARY=" + json.dumps(payload), {"workspace": str(workspace)},
            )

            self.assertIsNone(error)
            self.assertEqual(gate["status"], "NOT_APPLICABLE")
            self.assertEqual(gate["coverage_note"], "2 unmatched path(s); no recognized gate edits")

            payload["status"] = "FAILED"
            gate, error = _gate_scan_evidence(
                "GATE_SUMMARY=" + json.dumps(payload), {"workspace": str(workspace)},
            )
            self.assertEqual(gate["status"], "FAILED")
            self.assertIsNotNone(error)

            artifact.write_text('{"path":"src/module.py"}\n', encoding="utf-8")
            payload.update({"status": "WORKING", "artifact_bytes": artifact.stat().st_size})
            gate, error = _gate_scan_evidence(
                "GATE_SUMMARY=" + json.dumps(payload), {"workspace": str(workspace)},
            )
            self.assertEqual(gate["status"], "FAILED")
            self.assertIsNotNone(error)

            payload.update({
                "status": "WORKING", "listed": 1, "eligible": 1, "inspected": 1,
                "artifact_bytes": artifact.stat().st_size,
            })
            gate, error = _gate_scan_evidence(
                "GATE_SUMMARY=" + json.dumps(payload), {"workspace": str(workspace)},
            )
            self.assertIsNone(error)
            self.assertEqual(gate["status"], "WORKING")

            payload["status"] = "NOT_APPLICABLE"
            gate, error = _gate_scan_evidence(
                "GATE_SUMMARY=" + json.dumps(payload), {"workspace": str(workspace)},
            )
            self.assertEqual(gate["status"], "FAILED")
            self.assertIsNotNone(error)

    def test_gate_summary_accepts_single_inline_wrapper_and_rejects_malformed_or_duplicate_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            state_root = workspace / ".gstack-review-state"
            state_root.mkdir()
            artifact = state_root / "gate.jsonl"
            artifact.write_text("", encoding="utf-8")
            payload = {
                "status": "NOT_APPLICABLE", "listed": 0, "eligible": 0, "inspected": 0,
                "unread": 0, "unmatched": 0, "candidate": "b" * 12 + "+" + "a" * 40, "fingerprint": "a" * 40,
                "artifact": str(artifact), "artifact_bytes": 0,
            }
            bare = "GATE_SUMMARY=" + json.dumps(payload)
            wrapped = f"`{bare}`"

            self.assertEqual(_summary_json_line(bare, "GATE_SUMMARY="), payload)
            self.assertEqual(_summary_json_line(wrapped, "GATE_SUMMARY="), payload)
            self.assertEqual(_summary_json_line(wrapped, "GATE_SUMMARY=", must_be_last=True), payload)
            gate, error = _gate_scan_evidence(wrapped, {"workspace": str(workspace)})
            self.assertIsNone(error)
            self.assertEqual(gate["status"], "NOT_APPLICABLE")

            for invalid in (
                f"{bare}\n{wrapped}",
                "`GATE_SUMMARY={invalid}`",
                f"``{bare}``",
            ):
                with self.subTest(summary=invalid):
                    self.assertIsNone(_summary_json_line(invalid, "GATE_SUMMARY="))
                    gate, error = _gate_scan_evidence(invalid, {"workspace": str(workspace)})
                    self.assertEqual(gate["status"], "FAILED")
                    self.assertIsNotNone(error)

            self.assertIsNone(_summary_json_line(f"{wrapped}\ntrailing output", "GATE_SUMMARY=", must_be_last=True))

    def test_gate_scan_normalizes_composed_candidate_and_rejects_invalid_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            state_root = workspace / ".gstack-review-state"
            state_root.mkdir()
            artifact = state_root / "gate.jsonl"
            artifact.write_text("", encoding="utf-8")
            candidate = f"{'b' * 12}+{'a' * 40}"
            summary = "GATE_SUMMARY=" + json.dumps({
                "status": "NOT_APPLICABLE", "listed": 0, "eligible": 0, "inspected": 0,
                "unread": 0, "unmatched": 0, "candidate": candidate, "fingerprint": "a" * 40,
                "artifact": str(artifact), "artifact_bytes": 0,
            })
            gate, error = _gate_scan_evidence(summary, {"workspace": str(workspace)})
            self.assertIsNone(error)
            self.assertEqual(gate["fingerprint"], "a" * 40)
            self.assertEqual(gate["candidate"], candidate)

            composed_fingerprint = json.loads(summary.partition("=")[2])
            composed_fingerprint["candidate"] = None
            composed_fingerprint["fingerprint"] = candidate
            gate, error = _gate_scan_evidence("GATE_SUMMARY=" + json.dumps(composed_fingerprint), {"workspace": str(workspace)})
            self.assertIsNone(error)
            self.assertEqual((gate["fingerprint"], gate["candidate"]), ("a" * 40, candidate))

            for invalid in (f"{'b' * 12}+nowtree", f"bad+{'a' * 40}", f"{'b' * 12}+bad"):
                with self.subTest(candidate=invalid):
                    malformed = json.loads(summary.partition("=")[2])
                    malformed["candidate"] = invalid
                    malformed["fingerprint"] = invalid.rsplit("+", 1)[-1]
                    gate, error = _gate_scan_evidence("GATE_SUMMARY=" + json.dumps(malformed), {"workspace": str(workspace)})
                    self.assertEqual(gate["status"], "FAILED")
                    self.assertIsNotNone(error)

            for field, value in (("listed", -1), ("artifact_bytes", 1)):
                with self.subTest(field=field):
                    malformed = json.loads(summary.partition("=")[2])
                    malformed[field] = value
                    gate, error = _gate_scan_evidence("GATE_SUMMARY=" + json.dumps(malformed), {"workspace": str(workspace)})
                    self.assertEqual(gate["status"], "FAILED")
                    self.assertIsNotNone(error)

    def test_bun_discovery_accepts_installed_version_at_or_above_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "Bun Tools" / "bun"
            executable.parent.mkdir()
            executable.touch()
            executable.chmod(0o755)
            with patch("modules.runtime._bun_candidates", return_value=[executable]), patch(
                "modules.runtime.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="1.4.3\n")
            ) as run:
                resolved, version = _discover_bun()
        self.assertEqual(resolved, executable.resolve())
        self.assertEqual(version, (1, 4, 3))
        run.assert_called_once_with(
            [str(executable.resolve()), "--version"], capture_output=True, text=True, timeout=5, check=False,
        )

    def test_bun_discovery_fails_closed_when_missing_or_too_old(self) -> None:
        with patch("modules.runtime._bun_candidates", return_value=[]):
            with self.assertRaisesRegex(ModuleUnavailable, "no usable Bun executable"):
                _discover_bun()

        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "bun"
            executable.touch()
            executable.chmod(0o755)
            with patch("modules.runtime._bun_candidates", return_value=[executable]), patch(
                "modules.runtime.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="1.3.2")
            ):
                with self.assertRaisesRegex(ModuleUnavailable, "Bun >= 1.3.3; found 1.3.2"):
                    _discover_bun()

    def test_gstack_runtime_root_discovery_resolves_installed_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "gstack runtime"
            (root / "bin").mkdir(parents=True)
            (root / "bin/gstack-skill-start").touch()
            (root / "lib").mkdir()
            with patch.dict(os.environ, {"GSTACK_ROOT": str(root)}, clear=False):
                self.assertEqual(_gstack_runtime_root(), root.resolve())
            with patch.dict(os.environ, {"GSTACK_ROOT": str(Path(temp) / "missing")}, clear=False), patch(
                "modules.runtime.WORKBENCH_ROOT", Path(temp) / "empty-workbench"
            ), patch("modules.runtime._codex_home", return_value=Path(temp) / "empty-codex"):
                self.assertIsNone(_gstack_runtime_root())

    def test_full_review_hands_off_quoted_runtime_environment(self) -> None:
        workspace = _synthetic_scratch_workspace()
        runtime_root = Path("/tmp/gstack runtime with spaces")
        bun = Path("/tmp/Bun Tools/bin/bun")
        try:
            task = ProviderTask(
                workspace, "Review this synthetic fixture.", execution_policy="WORKSPACE_WRITE",
                context={"gstack_review_mode": "full", "gstack_review_workspace": "synthetic-scratch-v1"},
            )
            with patch("modules.runtime._gstack_runtime_root", return_value=runtime_root), patch(
                "modules.runtime._discover_bun", return_value=(bun, (1, 4, 3))
            ):
                prepared = prepare_module_task(task, "gstack", "software.code-review")
        finally:
            shutil.rmtree(workspace)
        workflow = prepared.execution_context["module"]["review_workflow"]
        self.assertEqual(workflow["gstack_root"], str(runtime_root))
        self.assertEqual(workflow["bun_executable"], str(bun))
        self.assertEqual(workflow["bun_bin_directory"], str(bun.parent))
        self.assertEqual(workflow["bun_version"], "1.4.3")
        environment = workflow["command_environment"]
        self.assertIn("GSTACK_ROOT='/tmp/gstack runtime with spaces'", environment)
        self.assertIn("BUN_EXE='/tmp/Bun Tools/bin/bun'", environment)
        self.assertIn("PATH='/tmp/Bun Tools/bin':\"$PATH\"", environment)
        self.assertIn(environment, "\n".join(prepared.constraints))
        self.assertIn('GIT_DIR="$PWD/.gstack-review-state/git"', environment)
        self.assertIn('GIT_WORK_TREE="$PWD"', environment)
        self.assertIn('GSTACK_STATE_ROOT="$PWD/.gstack-review-state"', environment)
        self.assertIn('TMPDIR="$PWD/.tmp"', environment)
        self.assertIn('GSTACK_WTREE_OBJECT_DIR="$PWD/.gstack-review-state/wtree-objects"', environment)
        self.assertNotIn('GSTACK_WTREE_OBJECT_DIR="$PWD/.gstack-review-state/git', environment)
        self.assertEqual(prepared.execution_policy, "WORKSPACE_WRITE")

    def test_full_review_preflight_fails_before_queue_handoff_without_runtime(self) -> None:
        gstack = next(module for module in module_inventory() if module["id"] == "gstack")
        workspace = _synthetic_scratch_workspace()
        try:
            task = ProviderTask(
                workspace, "Review this synthetic fixture.", execution_policy="WORKSPACE_WRITE",
                context={"gstack_review_mode": "full", "gstack_review_workspace": "synthetic-scratch-v1"},
            )
            with patch("modules.runtime.module_inventory", return_value=[gstack]), patch(
                "modules.runtime._gstack_runtime_root", return_value=None
            ), patch(
                "modules.runtime._discover_bun"
            ) as discover_bun:
                with self.assertRaisesRegex(ModuleUnavailable, "installed gstack runtime root"):
                    prepare_module_task(task, "gstack", "software.code-review")
                discover_bun.assert_not_called()
            with patch("modules.runtime.module_inventory", return_value=[gstack]), patch(
                "modules.runtime._gstack_runtime_root", return_value=Path("/tmp/gstack")
            ), patch(
                "modules.runtime._discover_bun", side_effect=ModuleUnavailable("Bun is too old")
            ):
                with self.assertRaisesRegex(ModuleUnavailable, "too old"):
                    prepare_module_task(task, "gstack", "software.code-review")
        finally:
            shutil.rmtree(workspace)

    def test_installed_gstack_skill_metadata_and_codex_invocation_are_review(self) -> None:
        source_skill = (ROOT / "gstack/.agents/skills/gstack-review/SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(_skill_frontmatter_name(source_skill), "review")
        self.assertIn('gstack-skill-start" --skill "review"', source_skill)
        setup = (ROOT / "gstack/setup").read_text(encoding="utf-8")
        self.assertIn("gstack-review's is\n    # `review`", setup)
        readme = (ROOT / "gstack/README.md").read_text(encoding="utf-8")
        self.assertIn("/review", readme)

        with tempfile.TemporaryDirectory() as temp:
            codex_home = Path(temp)
            installed_skill = codex_home / "skills/gstack-review/SKILL.md"
            installed_skill.parent.mkdir(parents=True)
            installed_skill.write_text(source_skill, encoding="utf-8")
            runtime_root = codex_home / "skills/gstack"
            (runtime_root / "bin").mkdir(parents=True)
            (runtime_root / "bin/gstack-skill-start").touch()
            (runtime_root / "lib").mkdir()
            with patch.dict(os.environ, {"CODEX_HOME": temp}, clear=False):
                available, evidence = _gstack_skill_status()
        self.assertTrue(available, "; ".join(evidence))
        self.assertTrue(any("SKILL.md" in item for item in evidence))
        self.assertTrue(any("gstack-skill-start" in item for item in evidence))

    def test_missing_codex_home_does_not_claim_skill_availability(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"CODEX_HOME": temp}, clear=False):
            available, evidence = _gstack_skill_status()
        self.assertFalse(available)
        self.assertIn("missing installed skill", evidence[0])

    def test_gstack_metadata_flows_through_existing_codex_provider_result(self) -> None:
        task = ProviderTask(
            ROOT,
            "Review the current diff for correctness and security issues.",
            task_id="module-review-contract",
            execution_policy="READ_ONLY",
            execution_context={
                "origin": "interactive-host", "host_role": "orchestrator", "dispatch_depth": 1,
                "direct_or_dispatch": "DISPATCH", "framing_result": "code review requested",
            },
        )
        prepared = prepare_module_task(task, "gstack", "software.code-review")
        self.assertEqual(prepared.provider_policy["allowed_providers"], ["codex"])
        self.assertEqual(prepared.required_capabilities, ("task.submit",))
        self.assertEqual(prepared.context["module"]["invocation"]["skill_id"], "review")
        self.assertEqual(prepared.context["module"]["invocation"]["command"], "/review")
        self.assertEqual(prepared.execution_context["module"]["capability"], "software.code-review")
        self.assertEqual(prepared.execution_policy, "READ_ONLY")
        self.assertEqual(prepared.execution_context["module"]["review_workflow"]["mode"], "read-only-partial")

        adapter = adapter_for("codex")
        self.assertIsInstance(adapter, QueueProviderAdapter)
        result = adapter.normalize({
            "id": prepared.task_id,
            "backend": "codex",
            "status": "queued",
            "execution_context": prepared.execution_context,
        }, prepared, "fixture-account", "selected codex by module provider requirement")
        self.assertEqual(result.status, "SUBMITTED")
        self.assertEqual(result.execution_context["module"]["id"], "gstack")
        self.assertEqual(result.execution_context["module"]["capability"], "software.code-review")
        self.assertEqual(result.provider_id, "codex")

    def test_missing_or_failed_mandatory_phase_never_reports_full_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            state_root = workspace / ".gstack-review-state"
            state_root.mkdir()
            artifact = state_root / "gate.jsonl"
            artifact.write_text("", encoding="utf-8")

            def result(*, outcome: str = "CLEAN", phases: dict | None = None, status: str = "COMPLETED", policy: str = "WORKSPACE_WRITE", fingerprint: str | None = "a" * 40, artifact_path: Path = artifact, errors: tuple[dict[str, str], ...] = ()) -> ProviderResult:
                metadata = {
                    "id": "gstack", "capability": "software.code-review",
                    "review_workflow": {
                        "id": "gstack-review-v1", "mode": "full",
                        "required_phases": list(GSTACK_REVIEW_REQUIRED_PHASES), "workspace": str(workspace),
                    },
                }
                gate = {
                    "status": "NOT_APPLICABLE", "listed": 0, "eligible": 0, "inspected": 0,
                    "unread": 0, "unmatched": 2, "fingerprint": fingerprint,
                    "artifact": str(artifact_path), "artifact_bytes": 0,
                }
                review_phases = phases if phases is not None else {
                    name: {"status": "passed", "evidence": f"command output for {name}"}
                    for name in GSTACK_REVIEW_REQUIRED_PHASES
                }
                lines = [
                    "GATE_SUMMARY=" + json.dumps(gate),
                    "GSTACK_REVIEW_PHASES=" + json.dumps(review_phases),
                    "GSTACK_REVIEW_RESULT=" + json.dumps({
                        "review_outcome": outcome, "critical_count": 1 if outcome == "ISSUES_FOUND" else 0,
                        "acceptance_passed": outcome == "CLEAN",
                    }),
                ]
                return ProviderResult(
                    "review", "codex", None, status, "\n".join(lines), (), (), errors, None, None,
                    "review", "worker", ("task.submit",), "test", execution_policy=policy,
                    execution_context={"module": metadata},
                )

            issue_found = normalize_module_result(result(outcome="ISSUES_FOUND"))
            workflow = issue_found.execution_context["module"]["review_workflow"]
            self.assertEqual(issue_found.status, "COMPLETED")
            self.assertEqual(workflow["execution_status"], "COMPLETED")
            self.assertEqual(workflow["review_outcome"], "ISSUES_FOUND")
            self.assertFalse(workflow["acceptance_passed"])
            self.assertEqual(workflow["critical_count"], 1)
            self.assertEqual(issue_found.errors, ())
            self.assertEqual(workflow["gate_scan"], {
                "status": "NOT_APPLICABLE", "listed": 0, "eligible": 0, "inspected": 0,
                "unread": 0, "unmatched": 2, "fingerprint": "a" * 40,
                "artifact": str(artifact.resolve()), "artifact_bytes": 0,
                "coverage_note": "2 unmatched path(s); no recognized gate edits",
            })

            clean = normalize_module_result(result())
            clean_workflow = clean.execution_context["module"]["review_workflow"]
            self.assertEqual((clean.status, clean_workflow["review_outcome"], clean_workflow["acceptance_passed"]), ("COMPLETED", "CLEAN", True))

            inconclusive = normalize_module_result(result(outcome="INCONCLUSIVE"))
            inconclusive_workflow = inconclusive.execution_context["module"]["review_workflow"]
            self.assertEqual(
                (inconclusive.status, inconclusive_workflow["review_outcome"], inconclusive_workflow["acceptance_passed"]),
                ("COMPLETED", "INCONCLUSIVE", False),
            )

            missing = normalize_module_result(result(phases={}))
            self.assertEqual(missing.status, "PARTIAL")
            self.assertEqual(missing.execution_context["module"]["review_workflow"]["missing_phases"], list(GSTACK_REVIEW_REQUIRED_PHASES))

            failed_phases = {
                name: {"status": "passed", "evidence": f"command output for {name}"}
                for name in GSTACK_REVIEW_REQUIRED_PHASES
            }
            failed_phases["exploratory_qa"] = {"status": "failed", "evidence": "probe command exited 2"}
            failed = normalize_module_result(result(phases=failed_phases))
            self.assertEqual(failed.status, "FAILED")
            self.assertEqual(failed.execution_context["module"]["review_workflow"]["execution_status"], "FAILED")

            no_fingerprint = normalize_module_result(result(fingerprint=None))
            self.assertEqual(no_fingerprint.status, "FAILED")
            self.assertEqual(no_fingerprint.execution_context["module"]["review_workflow"]["gate_scan"]["status"], "FAILED")

            no_artifact = normalize_module_result(result(artifact_path=state_root / "missing.jsonl"))
            self.assertEqual(no_artifact.status, "FAILED")
            self.assertEqual(no_artifact.execution_context["module"]["review_workflow"]["gate_scan"]["status"], "FAILED")

            execution_error = normalize_module_result(result(errors=({"code": "COMMAND_FAILED", "message": "mandatory command failed"},)))
            self.assertEqual(execution_error.status, "FAILED")


if __name__ == "__main__":
    unittest.main()
