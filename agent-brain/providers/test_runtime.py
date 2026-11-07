from __future__ import annotations

import json
import os
import tempfile
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from providers.runtime import (
    ProviderStatus,
    ProviderTask,
    ProviderUnavailable,
    QueueProviderAdapter,
    UnavailableProviderAdapter,
    _legacy_system_brain_copy,
    _sync_tool_mcp,
    _ensure_skill_link,
    discover_codex_homes,
    eligible_providers,
    load_registry,
    select_provider,
)


def status(provider_id: str, *, enabled: bool, available: bool, healthy: bool | None, roles: tuple[str, ...], capabilities: tuple[str, ...]) -> ProviderStatus:
    return ProviderStatus(
        provider_id=provider_id,
        name=provider_id,
        enabled=enabled,
        adapter_implemented=True,
        installed=True,
        authenticated=True,
        authentication_state="authenticated",
        available=available,
        healthy=healthy,
        execution_runtime_ready=available,
        health_signal="test fixture",
        roles=roles,
        capabilities=capabilities,
        model=None,
        fallback_priority=0,
    )


class ProviderFoundationTests(unittest.TestCase):
    def test_codex_tool_mcp_registration_is_per_discovered_home_and_conflict_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            config = home / "config.toml"
            config.write_text('[profiles.safe]\nmodel = "fixture"\n', encoding="utf-8")
            self.assertEqual(_sync_tool_mcp(home), "registered")
            first = config.read_text(encoding="utf-8")
            self.assertIn("[profiles.safe]", first)
            self.assertIn("[mcp_servers.system-agentic-tools]", first)
            self.assertIn('"SYSTEM_AGENTIC_TOOL_CALLER" = "codex"', first)
            self.assertIn('default_tools_approval_mode = "writes"', first)
            self.assertNotIn("account-id", first)
            self.assertEqual(_sync_tool_mcp(home), "already-registered")
            legacy = first.replace(', "SYSTEM_AGENTIC_TOOL_CALLER" = "codex"', "").replace('default_tools_approval_mode = "writes"\n', "")
            config.write_text(legacy, encoding="utf-8")
            self.assertEqual(_sync_tool_mcp(home), "updated-tool-policy")
            updated = config.read_text(encoding="utf-8")
            self.assertIn('default_tools_approval_mode = "writes"', updated)
            self.assertIn('"SYSTEM_AGENTIC_TOOL_CALLER" = "codex"', updated)
            config.write_text(first.replace("system-agentic-tool-mcp", "other-server"), encoding="utf-8")
            self.assertEqual(_sync_tool_mcp(home), "conflict")

    def test_registry_models_codex_active_and_unavailable_providers_without_credentials(self) -> None:
        registry = load_registry()
        providers = {item["id"]: item for item in registry["providers"]}
        self.assertTrue(providers["codex"]["enabled"])
        self.assertFalse(providers["claude"]["enabled"])
        self.assertFalse(providers["gemini"]["enabled"])
        self.assertFalse(providers["claude"]["capabilities"])
        self.assertFalse(providers["gemini"]["capabilities"])
        self.assertFalse(registry["account_policy"]["automatic_failover"])
        serialized = json.dumps(registry).lower()
        for forbidden in ("api_key", "access_token", "refresh_token", "password"):
            self.assertNotIn(forbidden, serialized)

    def test_candidate_query_filters_disabled_unavailable_unhealthy_and_missing_capability(self) -> None:
        available = status("codex", enabled=True, available=True, healthy=True, roles=("reviewer",), capabilities=("task.submit", "task.review"))
        disabled = status("claude", enabled=False, available=True, healthy=True, roles=("reviewer",), capabilities=("task.review",))
        unavailable = status("gemini", enabled=True, available=False, healthy=None, roles=("reviewer",), capabilities=("task.review",))
        unhealthy = status("local", enabled=True, available=True, healthy=False, roles=("reviewer",), capabilities=("task.review",))
        self.assertEqual([item.provider_id for item in eligible_providers("reviewer", {"task.review"}, [available, disabled, unavailable, unhealthy])], ["codex"])
        self.assertEqual(eligible_providers("reviewer", {"task.publish"}, [available]), [])

    def test_unavailable_adapter_refuses_execution_instead_of_faking_result(self) -> None:
        adapter = UnavailableProviderAdapter("claude")
        with self.assertRaises(ProviderUnavailable):
            adapter.execute(ProviderTask(Path.cwd(), "no-op"))

    def test_codex_adapter_keeps_ambiguous_queue_result_unknown_without_retry(self) -> None:
        adapter = QueueProviderAdapter("codex", "codex")
        ready = status("codex", enabled=True, available=True, healthy=True, roles=("worker",), capabilities=("task.submit",))
        with tempfile.TemporaryDirectory() as temp:
            task = ProviderTask(Path(temp), "safe synthetic verification")
            with patch.object(adapter, "detect", return_value=ready), \
                 patch("providers.runtime.subprocess.run", side_effect=subprocess.TimeoutExpired("queue", 15)) as run:
                receipt = adapter.execute(task)
        self.assertEqual(receipt.status, "UNKNOWN")
        self.assertEqual(receipt.queue_job_id, task.task_id)
        self.assertEqual(receipt.errors[0]["code"], "QUEUE_RECEIPT_AMBIGUOUS")
        submit_calls = [call for call in run.call_args_list if "submit" in call.args[0]]
        self.assertEqual(len(submit_calls), 1)

    def test_codex_adapter_normalizes_only_confirmed_queue_receipts(self) -> None:
        adapter = QueueProviderAdapter("codex", "codex")
        task = ProviderTask(Path.cwd(), "inspect fixture", task_id="test-job")
        receipt = adapter.normalize({"id": "test-job", "status": "queued", "backend": "codex"}, task, "fixture-account", "test")
        self.assertEqual(receipt.status, "SUBMITTED")
        self.assertEqual(receipt.queue_job_id, "test-job")
        completed = adapter.normalize({
            "id": "test-job", "status": "completed", "backend": "codex",
            "result_summary": "alpha.txt\nbeta.txt", "started_at": "2026-10-14T10:00:00+00:00",
            "finished_at": "2026-10-14T10:00:02+00:00", "attempts": [{}],
        }, task, "fixture-account", "test")
        self.assertEqual(completed.status, "COMPLETED")
        self.assertEqual(completed.summary, "alpha.txt\nbeta.txt")
        self.assertEqual(completed.duration_seconds, 2)
        self.assertEqual(completed.evidence[0]["attempt_count"], 1)
        self.assertEqual(completed.execution_policy, "READ_ONLY")
        self.assertEqual(adapter.normalize({"status": "failed"}, task, None, "test").status, "UNKNOWN")
        for queue_state, normalized in (("running", "RUNNING"), ("failed", "FAILED"), ("interrupted", "FAILED_LOST"), ("unrecognized", "UNKNOWN")):
            row = {"id": task.task_id, "backend": "codex", "status": queue_state}
            self.assertEqual(adapter.normalize(row, task, None, "test").status, normalized)

    def test_selection_uses_registry_priority_and_policy_instead_of_fixed_provider(self) -> None:
        codex = status("codex", enabled=True, available=True, healthy=True, roles=("worker",), capabilities=("task.submit",))
        codex = ProviderStatus(**(codex.__dict__ | {"fallback_priority": 10}))
        qwen = status("local-qwen", enabled=True, available=True, healthy=True, roles=("worker",), capabilities=("task.submit",))
        qwen = ProviderStatus(**(qwen.__dict__ | {"fallback_priority": 1}))
        task = ProviderTask(Path.cwd(), "inspect fixture")
        adapter, selection = select_provider(task, [codex, qwen])
        self.assertEqual(adapter.provider_id, "local-qwen")
        self.assertEqual(selection.candidates, ("codex", "local-qwen"))
        restricted = ProviderTask(Path.cwd(), "inspect fixture", provider_policy={"allowed_providers": ["codex"]})
        adapter, _ = select_provider(restricted, [codex, qwen])
        self.assertEqual(adapter.provider_id, "codex")

    def test_no_eligible_capability_refuses_selection(self) -> None:
        available = status("codex", enabled=True, available=True, healthy=True, roles=("worker",), capabilities=("task.submit",))
        task = ProviderTask(Path.cwd(), "read a fixture", required_capabilities=("credential.rotate",))
        with self.assertRaisesRegex(ProviderUnavailable, "NO_PROVIDER"):
            select_provider(task, [available])

    def test_live_local_qwen_selection_rechecks_transient_preflight(self) -> None:
        unavailable = status("local-qwen", enabled=True, available=False, healthy=False, roles=("worker",), capabilities=("task.submit",))
        ready = status("local-qwen", enabled=True, available=True, healthy=True, roles=("worker",), capabilities=("task.submit",))
        task = ProviderTask(Path.cwd(), "read a fixture", provider_policy={"allowed_providers": ["local-qwen"]})
        with patch("providers.runtime.discover_providers", side_effect=[[unavailable], [ready]]), \
             patch("providers.runtime.time.sleep") as sleep:
            adapter, selection = select_provider(task)
        self.assertEqual(adapter.provider_id, "local-qwen")
        self.assertEqual(selection.candidates, ("local-qwen",))
        sleep.assert_called_once()

    def test_live_provider_failure_stays_fail_closed_after_bounded_preflight(self) -> None:
        unavailable = status("local-qwen", enabled=True, available=False, healthy=False, roles=("worker",), capabilities=("task.submit",))
        task = ProviderTask(Path.cwd(), "read a fixture", provider_policy={"allowed_providers": ["local-qwen"]})
        with patch("providers.runtime.discover_providers", return_value=[unavailable]) as discover, \
             patch("providers.runtime.time.sleep") as sleep:
            with self.assertRaisesRegex(ProviderUnavailable, r"NO_PROVIDER.*preflight_attempts=3"):
                select_provider(task)
        self.assertEqual(discover.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_confirmed_queue_submission_is_followed_by_existing_queue_status_without_retry(self) -> None:
        adapter = QueueProviderAdapter("codex", "codex")
        ready = status("codex", enabled=True, available=True, healthy=True, roles=("worker",), capabilities=("task.submit",))
        task = ProviderTask(
            Path.cwd(), "read fixture", task_id="fixture-task", execution_policy="WORKSPACE_WRITE",
            execution_context={"origin": "interactive-host", "host_role": "orchestrator", "dispatch_depth": 1, "direct_or_dispatch": "DISPATCH"},
        )
        queued = subprocess.CompletedProcess([], 0, json.dumps({"id": task.task_id, "status": "queued", "backend": "codex"}), "")
        completed = subprocess.CompletedProcess([], 0, json.dumps({"id": task.task_id, "status": "completed", "backend": "codex", "result_summary": "a.txt", "attempts": []}), "")
        with patch.object(adapter, "detect", return_value=ready), \
             patch.object(adapter, "_account_id", return_value="fixture-account"), \
             patch("providers.runtime.subprocess.run", side_effect=[queued, completed]) as run, \
             patch("providers.runtime.time.sleep"):
            result = adapter.execute(task, wait_timeout_seconds=10)
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.summary, "a.txt")
        self.assertEqual(run.call_count, 2)
        self.assertIn("--task-id", run.call_args_list[0].args[0])
        self.assertIn("--execution-policy", run.call_args_list[0].args[0])
        self.assertIn("WORKSPACE_WRITE", run.call_args_list[0].args[0])
        self.assertNotIn("codex", run.call_args_list[1].args[0])

    def test_worker_task_prompt_carries_structured_no_recursion_context(self) -> None:
        task = ProviderTask(
            Path.cwd(), "inspect fixture",
            execution_context={"origin": "interactive-host", "host_role": "orchestrator", "dispatch_depth": 1, "direct_or_dispatch": "DISPATCH"},
        )
        prompt = task.prompt()
        self.assertIn('"dispatch_depth": 1', prompt)
        self.assertIn('"host_role": "orchestrator"', prompt)
        self.assertIn("Do not invoke system-agentic dispatch", prompt)

    def test_elevated_policy_never_reaches_queue_adapter(self) -> None:
        adapter = QueueProviderAdapter("codex", "codex")
        ready = status("codex", enabled=True, available=True, healthy=True, roles=("worker",), capabilities=("task.submit",))
        task = ProviderTask(Path.cwd(), "external write", execution_policy="ELEVATED")
        with patch.object(adapter, "detect", return_value=ready), patch("providers.runtime.subprocess.run") as run:
            with self.assertRaisesRegex(ProviderUnavailable, "ELEVATED_REQUIRES_APPROVAL"):
                adapter.execute(task)
        run.assert_not_called()

    def test_codex_summary_is_extracted_only_from_queue_owned_event_log(self) -> None:
        adapter = QueueProviderAdapter("codex", "codex")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = root / "logs" / "job.jsonl"
            log.parent.mkdir()
            log.write_text(json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "alpha.txt\nbeta.txt"},
            }) + "\n", encoding="utf-8")
            with patch("providers.runtime.QUEUE_LOGS_ROOT", log.parent):
                summary = adapter._summary_from_attempts([{"log_path": str(log)}])
                rejected = adapter._summary_from_attempts([{"log_path": str(root / "outside.jsonl")}])
        self.assertEqual(summary, "alpha.txt\nbeta.txt")
        self.assertIsNone(rejected)

    def test_local_qwen_summary_is_extracted_from_queue_owned_completion_log(self) -> None:
        adapter = QueueProviderAdapter("local-qwen", "local")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = root / "logs" / "job.jsonl"
            log.parent.mkdir()
            log.write_text(json.dumps({
                "runtime": "local_task_complete",
                "answer": "SYSTEM_AGENTIC_LOCAL_QWEN_OK",
            }) + "\n", encoding="utf-8")
            with patch("providers.runtime.QUEUE_LOGS_ROOT", log.parent):
                summary = adapter._summary_from_attempts([{"log_path": str(log)}])
        self.assertEqual(summary, "SYSTEM_AGENTIC_LOCAL_QWEN_OK")

    def test_symlink_sync_is_idempotent_and_preserves_custom_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, target = root / "source", root / "home" / "skills" / "test-skill"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text("source\n", encoding="utf-8")
            self.assertEqual(_ensure_skill_link(source, target), "linked")
            self.assertEqual(_ensure_skill_link(source, target), "already-linked")
            self.assertEqual(target.resolve(), source.resolve())

            custom = root / "home" / "skills" / "custom-skill"
            custom.mkdir(parents=True)
            (custom / "SKILL.md").write_text("custom\n", encoding="utf-8")
            self.assertEqual(_ensure_skill_link(source, custom), "conflict")
            self.assertEqual((custom / "SKILL.md").read_text(encoding="utf-8"), "custom\n")

    def test_sync_adopts_only_the_known_legacy_system_brain_copy_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            target = root / "home" / "skills" / "system-agentic-ai"
            for base in (source, target):
                (base / "agents").mkdir(parents=True)
                (base / "references").mkdir()
                (base / "SKILL.md").write_text("skill\n", encoding="utf-8")
                (base / "agents/openai.yaml").write_text("agent\n", encoding="utf-8")
                (base / "references/system-knowledge.md").write_text("old generated copy\n", encoding="utf-8")
            self.assertTrue(_legacy_system_brain_copy(target))
            self.assertEqual(_ensure_skill_link(source, target, adopt_old_brain_copy=True), "linked-managed-copy")
            self.assertEqual(target.resolve(), source.resolve())

    def test_orca_account_discovery_reads_marker_and_active_home_without_fixed_account_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            account_id = "fixture-account-id"
            account_home = home / "Library/Application Support/orca/codex-accounts" / account_id / "home"
            account_home.mkdir(parents=True)
            (account_home / ".orca-managed-home").write_text(account_id, encoding="utf-8")
            (home / ".codex").mkdir()
            with patch("providers.runtime.Path.home", return_value=home), \
                 patch("providers.runtime.ORCA_ACCOUNTS_ROOT", account_home.parents[1]), \
                 patch("providers.runtime._runtime_service_info", return_value=(True, str(account_home))), \
                 patch.dict(os.environ, {"CODEX_HOME": str(account_home)}), \
                 patch("providers.runtime._find_executable", return_value="/bin/codex"), \
                 patch("providers.runtime._codex_login", return_value=True):
                accounts = discover_codex_homes()
            self.assertEqual(len(accounts), 2)
            orca_account = next(item for item in accounts if item["account_id"] == account_id)
            self.assertTrue(orca_account["active"])
            self.assertTrue(orca_account["authenticated"])


if __name__ == "__main__":
    unittest.main()
