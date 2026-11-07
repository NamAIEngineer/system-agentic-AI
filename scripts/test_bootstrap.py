#!/usr/bin/env python3
"""Regression tests for the public bootstrap boundary."""
from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class BootstrapContractTests(unittest.TestCase):
    def run_script(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), *args],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )

    def test_manifest_has_seven_pinned_capability_descriptors(self) -> None:
        value = json.loads((ROOT / "sources.manifest.json").read_text(encoding="utf-8"))
        refs = value["repositories"]
        self.assertEqual(len(refs), 7)
        for ref in refs:
            self.assertRegex(ref["commit"], r"^[0-9a-f]{40}$")
            self.assertTrue(ref["capabilities"])

    def test_reference_check_only_is_offline_and_does_not_create_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "references"
            result = self.run_script("install-references.py", "--check-only", "--root", str(root))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(root.exists())
            plan = json.loads(result.stdout)
            self.assertEqual(plan["network"], "NOT_USED")
            self.assertEqual(len(plan["references"]), 7)

    def test_reference_install_requires_explicit_network_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_script("install-references.py", "--install", "--all", "--root", temp)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("APPROVAL_REQUIRED_NETWORK", result.stderr)

    def test_existing_non_git_reference_path_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "references"
            target = root / "system-one"
            target.mkdir(parents=True)
            marker = target / "owner-file.txt"
            marker.write_text("keep\n", encoding="utf-8")
            state = Path(temp) / "state"
            result = self.run_script(
                "install-references.py", "--install", "--id", "system-one",
                "--allow-network", "--root", str(root), "--state-root", str(state),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(marker.is_file())
            receipt = json.loads((state / "references-installation.json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["items"]["system-one"]["status"], "BLOCKED_NON_GIT_PATH")

    def test_quyet_check_only_reports_advisory_boundary(self) -> None:
        result = self.run_script("setup-quyet.py", "--check-only", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["execution_authority"], "ADVISORY_ONLY")
        self.assertIn(value["status"], {"READY", "NOT_INSTALLED", "UNAVAILABLE", "ERROR"})

    def test_quyet_check_discovers_the_scoped_install_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "quyet"
            snapshot = state / "model-cache" / "hub" / "models--chinhnc--Quyet-1.0-Tiny" / "snapshots" / "fba8ca9292a705e21b14a4c5150e79a4ddcc8db3"
            snapshot.mkdir(parents=True)
            for name in ("LICENSE", "NOTICE", "quyet_config.json"):
                (snapshot / name).write_text("fixture\n", encoding="utf-8")
            result = self.run_script_with_env(
                "setup-quyet.py", {"SYSTEM_AGENTIC_QUYET_ROOT": str(state), "SYSTEM_AGENTIC_QUYET_URL": "http://127.0.0.1:1"},
                "--check-only", "--json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout)
            self.assertTrue(value["model_present"])
            self.assertEqual(value["model_cache"], str(snapshot.resolve()))

    def test_quyet_install_without_approval_does_not_create_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            cache = Path(temp) / "cache"
            result = self.run_script(
                "setup-quyet.py", "--install", "--state-root", str(state), "--model-cache", str(cache),
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("APPROVAL_REQUIRED", result.stderr)
            self.assertFalse(state.exists())
            self.assertFalse(cache.exists())

    def test_quyet_install_scopes_pip_cache_to_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            cache = Path(temp) / "cache"
            spec = importlib.util.spec_from_file_location("setup_quyet_for_test", ROOT / "scripts" / "setup-quyet.py")
            self.assertIsNotNone(spec and spec.loader)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            args = type("Args", (), {
                "allow_network": True,
                "allow_model_download": True,
                "state_root": state,
                "model_cache": cache,
            })()
            with patch.object(module.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 0)
                module.install(args)
            install_env = run.call_args_list[1].kwargs["env"]
            self.assertEqual(install_env["PIP_CACHE_DIR"], str((state / "pip-cache").resolve()))

    def test_quyet_server_rejects_non_loopback_binding(self) -> None:
        env = {"SYSTEM_AGENTIC_QUYET_URL": "http://0.0.0.0:18767"}
        result = self.run_script_with_env("quyet-local-server.py", env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must bind to loopback", result.stderr)

    def test_decision_plane_uses_configured_quyet_endpoint(self) -> None:
        sys.path.insert(0, str(ROOT / "agent-brain"))
        spec = importlib.util.spec_from_file_location("decision_plane_for_test", ROOT / "agent-brain" / "decision_plane.py")
        self.assertIsNotNone(spec and spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self):
                return b'{"advisory": true, "answers": {}}'

        with patch.dict(os.environ, {"SYSTEM_AGENTIC_QUYET_URL": "http://127.0.0.1:18767/"}), patch.object(module.urllib.request, "urlopen", return_value=Response()) as opened:
            module._quyet_decide("synthetic", ["none"], ["READ_ONLY"])
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:18767/decision")

    def test_doctor_separates_core_and_optional_status(self) -> None:
        result = self.run_script("doctor.py", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        for field in ("core_status", "references_status", "quyet_status", "optional_providers", "host_execution_status"):
            self.assertIn(field, value)
        self.assertIn(value["core_status"], {"READY", "BLOCKED"})
        self.assertIsInstance(value["optional_providers"], dict)

    def run_script_with_env(self, script: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
        child_env = os.environ.copy()
        child_env.update(env)
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), *args],
            cwd=ROOT, text=True, capture_output=True, check=False, env=child_env,
        )


if __name__ == "__main__":
    unittest.main()
