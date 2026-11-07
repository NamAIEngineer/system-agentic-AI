from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import local_agent


class LocalAgentArtifactSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = {
            "artifact_kind": "copy",
            "artifact_paths": [".system-agentic-goals/run/team/copy.json"],
        }

    def test_declared_copy_typo_is_rejected_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = {
                "source_refs": ["README.md"],
                "draft_text": "bounded draft",
                "audience": "UNKNOWN",
                "claim": ["UNKNOWN"],
                "DRAFT_ONLY": True,
                "rehearsal": True,
                "liveEligible": False,
                "externalSideEffects": False,
                "unknownRetryAllowed": False,
            }
            result = local_agent.execute_tool(
                root,
                "write_file",
                json.dumps({"path": self.context["artifact_paths"][0], "content": json.dumps(payload)}),
                mutations_allowed=True,
                workspace_context={"assignedWorkspace": str(root), "workspaceAuthority": "SYSTEM_AGENTIC_QUEUE"},
                artifact_context=self.context,
            )
            self.assertIn("ARTIFACT_SCHEMA_INVALID", result)
            self.assertFalse((root / self.context["artifact_paths"][0]).exists())

    def test_declared_measurement_exact_shape_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = {
                "artifact_kind": "measurement",
                "artifact_paths": [".system-agentic-goals/run/team/measurement.json"],
            }
            payload = {
                "source_refs": ["README.md"],
                "verified_publication_count": "UNKNOWN",
                "channel_metrics": "UNKNOWN",
                "attribution": "UNKNOWN",
                "evidence_refs": "UNKNOWN",
                "live_eligibility": "UNKNOWN",
                "DRAFT_ONLY": True,
                "rehearsal": True,
                "liveEligible": False,
                "externalSideEffects": False,
                "unknownRetryAllowed": False,
            }
            result = local_agent.execute_tool(
                root,
                "write_file",
                json.dumps({"path": context["artifact_paths"][0], "content": json.dumps(payload)}),
                mutations_allowed=True,
                workspace_context={"assignedWorkspace": str(root), "workspaceAuthority": "SYSTEM_AGENTIC_QUEUE"},
                artifact_context=context,
            )
            self.assertIn('"written"', result)
            self.assertEqual(json.loads((root / context["artifact_paths"][0]).read_text()), payload)

    def test_declared_integration_exact_shape_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = {
                "artifact_kind": "integration",
                "artifact_paths": [".system-agentic-goals/run/team/integration.json"],
            }
            payload = {
                "input_refs": ["copy.json", "measurement.json"],
                "source_refs": ["README.md", "package.json", "src/server.mjs"],
                "DRAFT_ONLY": True,
                "rehearsal": True,
                "liveEligible": False,
                "externalSideEffects": False,
                "unknownRetryAllowed": False,
            }
            result = local_agent.execute_tool(
                root,
                "write_file",
                json.dumps({"path": context["artifact_paths"][0], "content": json.dumps(payload)}),
                mutations_allowed=True,
                workspace_context={"assignedWorkspace": str(root), "workspaceAuthority": "SYSTEM_AGENTIC_QUEUE"},
                artifact_context=context,
            )
            self.assertIn('"written"', result)
            self.assertEqual(json.loads((root / context["artifact_paths"][0]).read_text()), payload)

    def test_integration_schema_recovery_message_has_input_refs(self) -> None:
        message = local_agent.artifact_schema_recovery_message(
            {"artifact_kind": "integration", "artifact_paths": ["integration.json"]},
            "ARTIFACT_SCHEMA_INVALID: content must be one JSON object",
        )
        self.assertIsNotNone(message)
        self.assertIn('"input_refs"', message)
        self.assertIn("each quoted once", message)

    def test_declared_copy_omitted_safety_flags_are_filled_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = {
                "source_refs": ["README.md", "package.json", "src/server.mjs"],
                "draft_text": "Grounded draft",
                "audience": "Operators",
                "claims": ["README.md claim", "package.json claim", "src/server.mjs claim"],
            }
            result = local_agent.execute_tool(
                root,
                "write_file",
                json.dumps({"path": self.context["artifact_paths"][0], "content": json.dumps(payload)}),
                mutations_allowed=True,
                workspace_context={"assignedWorkspace": str(root), "workspaceAuthority": "SYSTEM_AGENTIC_QUEUE"},
                artifact_context=self.context,
            )
            self.assertIn('"written"', result)
            written = json.loads((root / self.context["artifact_paths"][0]).read_text())
            self.assertEqual(
                {key: written[key] for key in ("DRAFT_ONLY", "rehearsal", "liveEligible", "externalSideEffects", "unknownRetryAllowed")},
                {"DRAFT_ONLY": True, "rehearsal": True, "liveEligible": False, "externalSideEffects": False, "unknownRetryAllowed": False},
            )

    def test_declared_copy_explicit_unsafe_safety_flag_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = {
                "source_refs": ["README.md"],
                "draft_text": "Grounded draft",
                "audience": "Operators",
                "claims": ["README.md claim"],
                "liveEligible": True,
            }
            result = local_agent.execute_tool(
                root,
                "write_file",
                json.dumps({"path": self.context["artifact_paths"][0], "content": json.dumps(payload)}),
                mutations_allowed=True,
                workspace_context={"assignedWorkspace": str(root), "workspaceAuthority": "SYSTEM_AGENTIC_QUEUE"},
                artifact_context=self.context,
            )
            self.assertIn("liveEligible must be False", result)
            self.assertFalse((root / self.context["artifact_paths"][0]).exists())

    def test_schema_recovery_message_contains_canonical_keys_only(self) -> None:
        message = local_agent.artifact_schema_recovery_message(
            {"artifact_kind": "copy", "artifact_paths": [".system-agentic-goals/run/team/copy.json"]},
            '{"error":"ARTIFACT_SCHEMA_INVALID: missing claims"}',
        )
        self.assertIsNotNone(message)
        self.assertIn('"claims"', message)
        self.assertNotIn('"claim"', message)
        self.assertIn("channel_metrics", local_agent.artifact_schema_recovery_message(
            {"artifact_kind": "measurement", "artifact_paths": ["measurement.json"]},
            "ARTIFACT_SCHEMA_INVALID",
        ))

    def test_schema_recovery_message_does_not_normalize_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = {"artifact_kind": "copy", "artifact_paths": ["copy.json"]}
            message = local_agent.artifact_schema_recovery_message(context, "ARTIFACT_SCHEMA_INVALID")
            self.assertTrue(message)
            self.assertFalse((root / "copy.json").exists())

    def test_declared_verification_receipt_uses_verifier_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = {
                "artifact_kind": "verification",
                "artifact_paths": [".system-agentic-goals/run/team/verification.json"],
            }
            payload = {
                "verdict": "PASS_BOUNDED",
                "checked_artifacts": ["copy.json", "integration.json"],
                "evidenceRefs": ["README.md", "package.json", "src/server.mjs"],
                "findings": ["bounded source check passed"],
            }
            result = local_agent.execute_tool(
                root,
                "write_file",
                json.dumps({"path": context["artifact_paths"][0], "content": json.dumps(payload)}),
                mutations_allowed=True,
                workspace_context={"assignedWorkspace": str(root), "workspaceAuthority": "SYSTEM_AGENTIC_QUEUE"},
                artifact_context=context,
            )
            self.assertIn('"written"', result)
            self.assertEqual(json.loads((root / context["artifact_paths"][0]).read_text()), payload)

    def test_declared_verification_fail_receipt_is_persistable_for_ledger_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = {"artifact_kind": "verification", "artifact_paths": ["verification.json"]}
            payload = {
                "verdict": "FAIL",
                "checked_artifacts": ["copy.json"],
                "evidenceRefs": ["README.md"],
                "findings": ["bounded finding recorded"],
            }
            result = local_agent.execute_tool(
                root,
                "write_file",
                json.dumps({"path": "verification.json", "content": json.dumps(payload)}),
                mutations_allowed=True,
                workspace_context={"assignedWorkspace": str(root), "workspaceAuthority": "SYSTEM_AGENTIC_QUEUE"},
                artifact_context=context,
            )
            self.assertIn('"written"', result)

    def test_declared_verification_receipt_repairs_one_trailing_brace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = {"artifact_kind": "verification", "artifact_paths": ["verification.json"]}
            payload = {
                "verdict": "PASS_BOUNDED",
                "checked_artifacts": ["copy.json"],
                "evidenceRefs": ["README.md"],
                "findings": ["bounded finding recorded"],
            }
            malformed = json.dumps(payload) + "}"
            result = local_agent.execute_tool(
                root,
                "write_file",
                json.dumps({"path": "verification.json", "content": malformed}),
                mutations_allowed=True,
                workspace_context={"assignedWorkspace": str(root), "workspaceAuthority": "SYSTEM_AGENTIC_QUEUE"},
                artifact_context=context,
            )
            self.assertIn('"written"', result)
            self.assertEqual(json.loads((root / "verification.json").read_text()), payload)

    def test_declared_verification_receipt_normalizes_bounded_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = {"artifact_kind": "verification", "artifact_paths": ["verification.json"]}
            payload = {
                "verdict": "PASS_BOUNDED",
                "checked_artifact": ["copy.json"],
                "evidence_refs": ["README.md"],
                "findings": ["bounded finding recorded"],
            }
            result = local_agent.execute_tool(
                root,
                "write_file",
                json.dumps({"path": "verification.json", "content": json.dumps(payload) + "}"}),
                mutations_allowed=True,
                workspace_context={"assignedWorkspace": str(root), "workspaceAuthority": "SYSTEM_AGENTIC_QUEUE"},
                artifact_context=context,
            )
            self.assertIn('"written"', result)
            written = json.loads((root / "verification.json").read_text())
            self.assertEqual(written["checked_artifacts"], ["copy.json"])
            self.assertEqual(written["evidenceRefs"], ["README.md"])

    def test_tool_allowlist_hides_and_rejects_unlisted_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = json.dumps({"path": "."})
            result = local_agent.execute_tool(root, "list_files", raw, tool_allowlist={"read_file", "write_file"})
            self.assertIn("TOOL_NOT_ALLOWED", result)


if __name__ == "__main__":
    unittest.main()
