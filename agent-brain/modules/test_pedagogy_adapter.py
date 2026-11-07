from __future__ import annotations

import os
import sys
import tempfile
import threading
import uuid
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from modules.pedagogy_adapter import PedagogyUnavailable, execute, request
from modules import pedagogy_runtime
from modules.runtime import ModuleUnavailable, module_candidates, module_inventory, prepare_module_task
from providers.runtime import ProviderTask


PEDAGOGY_ROOT = Path(__file__).resolve().parents[2] / "design-os-pedagogy"


class PedagogyAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        sys.path.insert(0, str(PEDAGOGY_ROOT))
        from rehearsal.catalog import Catalog
        from rehearsal.http_server import create_server
        from rehearsal.store import Store

        self.temp = tempfile.TemporaryDirectory(prefix="system-agentic-pedagogy-")
        self.data_dir = Path(self.temp.name)
        catalog = Catalog(PEDAGOGY_ROOT / "rehearsal/data/fractions.vi.json")
        self.server = create_server(Store(self.data_dir / "sessions.sqlite3", catalog), catalog,
                                    PEDAGOGY_ROOT / "rehearsal/static", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.old_url = os.environ.get("SYSTEM_AGENTIC_PEDAGOGY_URL")
        os.environ["SYSTEM_AGENTIC_PEDAGOGY_URL"] = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.old_url is None:
            os.environ.pop("SYSTEM_AGENTIC_PEDAGOGY_URL", None)
        else:
            os.environ["SYSTEM_AGENTIC_PEDAGOGY_URL"] = self.old_url
        self.temp.cleanup()

    def test_live_api_session_feedback_and_persistence_use_official_domain_state(self) -> None:
        self.assertTrue(any(item["execution_ready"] for item in module_candidates(
            task_type="teaching-rehearsal", capabilities={"pedagogy.rehearse"}
        )))
        self.assertEqual(module_candidates(task_type="software.code-review", capabilities={"pedagogy.rehearse"}), [])

        created = execute("synthetic-task", "pedagogy.rehearse", {
            "pedagogy_request": {"action": "create", "case_id": "fraction-magnitude", "title": "Scratch rehearsal"}
        })
        self.assertEqual(created["execution_status"], "COMPLETED")
        self.assertEqual(created["domain_outcome"], "IN_PROGRESS")
        session_id = created["session_id"]
        self.assertEqual(created["domain_state"]["revision"], 0)
        self.assertEqual(created["artifacts"][0]["session_id"], session_id)
        rejected = execute("invalid-choice", "pedagogy.rehearse", {
            "pedagogy_request": {"action": "advance", "session_id": session_id,
                                 "revision": 0, "type": "probe", "choice": "not-an-official-choice"}
        })
        self.assertEqual(rejected["execution_status"], "COMPLETED")
        self.assertEqual(rejected["domain_outcome"], "INPUT_REJECTED")
        self.assertTrue(rejected["errors"])

        # Use the official phase/options and event API; deliberately choose a weak authored strategy.
        choices = {"probe": "rule", "interpret": "certain", "teach": "rule_only",
                   "check": "praise", "review": "continue", "transfer": "accept", "reflect": "save"}
        current = created
        for _ in range(7):
            from modules.pedagogy_adapter import request
            _, response = request("GET", f"/api/sessions/{session_id}")
            state = response["session"]
            phase = state["phase"]
            current = execute("synthetic-task", "pedagogy.rehearse", {
                "pedagogy_request": {
                    "action": "advance", "session_id": session_id, "revision": state["revision"],
                    "type": phase, "choice": choices[phase],
                    "text": "Tôi sẽ thử cách giải thích này." if phase == "reflect" else ""
                }
            })

        self.assertEqual(current["execution_status"], "COMPLETED")
        self.assertEqual(current["domain_outcome"], "AUTHORED_FEEDBACK_AVAILABLE")
        self.assertEqual(current["session_id"], session_id)
        self.assertEqual(current["domain_state"]["phase"], "complete")
        self.assertEqual(current["automatic_evaluation"], "NOT_APPLICABLE")

        feedback = execute("feedback-task", "pedagogy.feedback", {
            "pedagogy_request": {"action": "feedback", "session_id": session_id}
        })
        self.assertEqual(feedback["execution_status"], "COMPLETED")
        self.assertIn("review", feedback["feedback"])
        self.assertIn("transfer_feedback", feedback["feedback"])

        from modules.pedagogy_adapter import request
        _, replay = request("GET", f"/api/sessions/{session_id}/replay")
        self.assertTrue(replay["matches_saved_state"])
        _, reopened = request("GET", f"/api/sessions/{session_id}")
        self.assertEqual(reopened["session"]["revision"], 7)
        exported = execute("export-task", "pedagogy.session", {
            "pedagogy_request": {"action": "export", "session_id": session_id, "format": "json"}
        })
        self.assertEqual(exported["domain_outcome"], "ARTIFACT_EXPORTED")
        self.assertEqual(exported["artifacts"][1]["format"], "json")

    def test_pedagogy_write_actions_require_explicit_workspace_write(self) -> None:
        candidate = next(item for item in module_inventory() if item["id"] == "pedagogy")
        with patch("modules.runtime.module_inventory", return_value=[candidate]):
            task = ProviderTask(Path(self.temp.name), "Start a synthetic rehearsal.",
                                context={"pedagogy_request": {"action": "create", "case_id": "fraction-magnitude"}})
            with self.assertRaisesRegex(ModuleUnavailable, "WORKSPACE_WRITE"):
                prepare_module_task(task, "pedagogy", "pedagogy.rehearse")

    def test_unavailable_service_fails_closed_without_local_execution(self) -> None:
        with patch.dict(os.environ, {"SYSTEM_AGENTIC_PEDAGOGY_URL": "http://127.0.0.1:1"}):
            with self.assertRaises(PedagogyUnavailable):
                execute("synthetic-task", "pedagogy.rehearse", {
                    "pedagogy_request": {"action": "create", "case_id": "fraction-magnitude"}
                })

    def test_registry_readiness_is_not_assumed_from_installed_files(self) -> None:
        with patch.dict(os.environ, {"SYSTEM_AGENTIC_PEDAGOGY_URL": "http://127.0.0.1:1"}):
            candidate = next(item for item in module_inventory() if item["id"] == "pedagogy")
        self.assertFalse(candidate["available"])
        self.assertFalse(candidate["execution_ready"])

    def test_non_loopback_service_override_is_never_execution_ready(self) -> None:
        with patch.dict(os.environ, {"SYSTEM_AGENTIC_PEDAGOGY_URL": "https://example.com"}):
            candidate = next(item for item in module_inventory() if item["id"] == "pedagogy")
        self.assertFalse(candidate["available"])
        self.assertFalse(candidate["execution_ready"])

    def test_managed_start_failure_is_normalized_as_unavailable(self) -> None:
        with patch.dict(os.environ, {"SYSTEM_AGENTIC_PEDAGOGY_URL": ""}), patch(
            "modules.pedagogy_adapter.ensure_runtime",
            side_effect=pedagogy_runtime.PedagogyRuntimeUnavailable("synthetic startup failure"),
        ):
            with self.assertRaisesRegex(PedagogyUnavailable, "synthetic startup failure"):
                execute("synthetic-task", "pedagogy.rehearse", {
                    "pedagogy_request": {"action": "create", "case_id": "fraction-magnitude"}
                })


class ManagedPedagogyRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="system-agentic-managed-pedagogy-")
        self.state_root = Path(self.temp.name) / "state"

    def tearDown(self) -> None:
        pedagogy_runtime.stop_managed(self.state_root)
        self.temp.cleanup()

    def test_official_service_starts_reuses_persists_and_restarts_under_state_root(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            concurrent = list(pool.map(lambda _: pedagogy_runtime.ensure_runtime(self.state_root), range(2)))
        first = next(item for item in concurrent if not item["reused"])
        second = next(item for item in concurrent if item["reused"])
        self.assertFalse(first["reused"])
        self.assertTrue(first["managed"])
        self.assertEqual(first["host"], "127.0.0.1")
        self.assertIn("-B", first["command"])
        self.assertTrue(Path(first["interpreter"]).is_file())
        state_root = Path(first["state_root"])
        database = Path(first["db_path"])
        self.assertEqual(database, state_root / "rehearsal.sqlite3")
        self.assertTrue(state_root.is_relative_to(self.state_root.resolve()))

        self.assertTrue(second["reused"])
        self.assertEqual(second["pid"], first["pid"])
        self.assertEqual(second["endpoint"], first["endpoint"])

        status, created = request("POST", "/api/sessions", {
            "case_id": "fraction-magnitude", "request_id": str(uuid.uuid4()),
        }, base_url=str(first["endpoint"]))
        self.assertEqual(status, 201)
        session_id = created["session"]["id"]
        self.assertTrue(database.is_file())
        self.assertTrue(database.resolve().is_relative_to(state_root.resolve()))

        self.assertTrue(pedagogy_runtime.stop_managed(self.state_root))
        restarted = pedagogy_runtime.ensure_runtime(self.state_root)
        self.assertNotEqual(restarted["pid"], first["pid"])
        status, reopened = request("GET", f"/api/sessions/{session_id}", base_url=str(restarted["endpoint"]))
        self.assertEqual(status, 200)
        self.assertEqual(reopened["session"]["id"], session_id)
        self.assertEqual(Path(restarted["db_path"]), database)

    def test_start_failure_fails_closed_without_spawning_unverified_interpreter(self) -> None:
        with patch.object(pedagogy_runtime, "_compatible_python", return_value=Path(self.temp.name) / "missing-python"):
            with self.assertRaises(pedagogy_runtime.PedagogyRuntimeUnavailable):
                pedagogy_runtime.ensure_runtime(self.state_root)
        self.assertFalse((self.state_root / "runtime.json").exists())

    def test_stale_or_unowned_pid_is_not_terminated(self) -> None:
        metadata = {
            "module": "pedagogy", "pid": os.getpid(), "state_root": str(self.state_root.resolve()),
            "interpreter": sys.executable,
        }
        self.assertFalse(pedagogy_runtime._owned(metadata))
        self.assertFalse(pedagogy_runtime._stop_owned(metadata))
        self.assertIsNone(pedagogy_runtime._read_metadata(self.state_root))

    def test_down_but_startable_pedagogy_remains_execution_ready_candidate(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SYSTEM_AGENTIC_PEDAGOGY_URL", None)
            with patch("modules.pedagogy_runtime.inspect_runtime", return_value={
                "health": "inactive", "endpoint": None, "managed": False,
            }), patch("modules.pedagogy_runtime.is_startable", return_value=(True, "/python3.12")):
                candidate = next(item for item in module_candidates(
                    task_type="teaching-rehearsal", capabilities={"pedagogy.rehearse"}
                ) if item["id"] == "pedagogy")
        self.assertFalse(candidate["available"])
        self.assertEqual(candidate["runtime_state"], "inactive")
        self.assertTrue(candidate["managed_startable"])
        self.assertTrue(candidate["execution_ready"])


if __name__ == "__main__":
    unittest.main()
