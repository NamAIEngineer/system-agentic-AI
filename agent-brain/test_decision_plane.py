from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent-brain"))
sys.path.insert(0, str(ROOT / "scripts"))

from decision_plane import DecisionPlane  # noqa: E402
from providers.runtime import _instruction_block  # noqa: E402
from classifier_contract import decision_questions  # noqa: E402


def answers(*, route="DIRECT", module="none", execution_class="READ_ONLY", confidence=0.91):
    return {
        "route": {"choice": route, "confidence": confidence},
        "module": {"choice": module, "confidence": confidence},
        "execution_class": {"choice": execution_class, "confidence": confidence},
    }


class DecisionPlaneTests(unittest.TestCase):
    def test_high_confidence_quyet_returns_common_advisory_envelope(self):
        plane = DecisionPlane(quyet=lambda _, module_options=None, execution_class_options=None: {"model": "Quyet-1.0-Tiny", "answers": answers()})
        result = plane.decide("Explain a simple concept.")
        self.assertEqual(result["final_source"], "mixed")
        self.assertEqual(result["choices"]["route"], "DIRECT")
        self.assertEqual(result["thresholds"]["quyet_tiny"]["route"], 0.65)
        self.assertEqual(result["thresholds"]["quyet_tiny"]["execution_class"], 0.80)
        self.assertEqual(result["engines"][0]["confidence"]["route"], 0.91)
        self.assertFalse(result["execution_authorized"])

    def test_low_confidence_escalates_only_after_explicit_typesafe_opt_in(self):
        calls = []
        low = answers(confidence=0.4)
        jev_result = {"route": {"choice": "DIRECT"}, "module": {"choice": "none"}, "execution_class": {"choice": "READ_ONLY"}}
        plane = DecisionPlane(
            quyet=lambda _, module_options=None, execution_class_options=None: {"answers": low},
            jev=lambda prompt, fields: calls.append((prompt, fields)) or {"model": "jev-latest", "answers": jev_result},
        )
        without_opt_in = plane.decide("Synthetic public prompt")
        self.assertEqual(calls, [])
        self.assertEqual(without_opt_in["final_source"], "mixed")
        self.assertEqual(without_opt_in["choices"], {"route": None, "module": "none", "execution_class": None})

        with_opt_in = plane.decide("Synthetic public prompt", allow_typesafe=True)
        self.assertEqual(calls, [("Synthetic public prompt", ["execution_class", "route"])])
        self.assertEqual(with_opt_in["final_source"], "mixed")
        self.assertIsNone(with_opt_in["engines"][1]["confidence"]["route"])
        self.assertIn("missing_route_confidence", with_opt_in["engines"][1]["reasons"]["route"])
        self.assertEqual(with_opt_in["host_reasoning_required"], ["execution_class", "route"])

    def test_confident_jev_result_is_normalized_without_authorizing_execution(self):
        plane = DecisionPlane(
            quyet=lambda _, module_options=None, execution_class_options=None: {"answers": {"route": {"choice": "wrong"}}},
            jev=lambda _, fields: {"model": "jev-latest", "answers": answers(route="DISPATCH", execution_class="ELEVATED")},
        )
        result = plane.decide("Synthetic request", allow_typesafe=True)
        self.assertEqual(result["final_source"], "mixed")
        self.assertEqual(result["choices"]["execution_class"], "ELEVATED")
        self.assertEqual(result["choices"]["module"], "none")
        self.assertFalse(result["execution_authorized"])

    def test_quyet_and_jev_outages_fall_back_to_host_without_failure(self):
        def unavailable(*_, **__):
            raise OSError("unavailable")

        result = DecisionPlane(quyet=unavailable, jev=unavailable).decide("Synthetic prompt", allow_typesafe=True)
        self.assertEqual(result["final_source"], "mixed")
        self.assertEqual(result["fallback"], "host_reasoning_required")
        self.assertEqual([engine["status"] for engine in result["engines"]], ["unavailable", "unavailable"])
        self.assertEqual(result["host_reasoning_required"], ["execution_class", "route"])

    def test_high_confidence_quyet_fields_are_kept_while_only_ambiguous_fields_escalate(self):
        local_answers = answers(confidence=0.9)
        local_answers["module"]["confidence"] = 0.2
        local_answers["execution_class"]["confidence"] = 0.3
        calls = []
        remote_answers = answers(route="DIRECT", module="none", execution_class="READ_ONLY", confidence=0.92)
        plane = DecisionPlane(
            quyet=lambda _, module_options=None, execution_class_options=None: {"model": "Quyet-1.0-Tiny", "answers": local_answers},
            jev=lambda _, fields: calls.append(fields) or {"model": "jev-latest", "answers": remote_answers},
        )
        result = plane.decide("Synthetic ambiguous module and policy task", allow_typesafe=True)
        self.assertEqual(calls, [["execution_class"]])
        self.assertEqual(result["choices"]["route"], "DIRECT")
        self.assertEqual(result["field_sources"]["route"], "quyet-tiny")
        self.assertEqual(result["field_sources"]["module"], "registry-prefilter")
        self.assertEqual(result["final_source"], "mixed")
        self.assertEqual(result["host_reasoning_required"], [])

    def test_weak_elevated_advice_is_left_for_host_and_never_authorizes_execution(self):
        local_answers = answers(route="DISPATCH", execution_class="ELEVATED", confidence=0.9)
        local_answers["execution_class"]["confidence"] = 0.54
        result = DecisionPlane(quyet=lambda _, module_options=None, execution_class_options=None: {"answers": local_answers}).decide("Synthetic privileged request")
        self.assertIsNone(result["choices"]["execution_class"])
        self.assertEqual(result["host_reasoning_required"], ["execution_class"])
        self.assertFalse(result["execution_authorized"])

    def test_invalid_threshold_rejected_and_gstack_must_be_execution_ready(self):
        with patch.dict(os.environ, {"SYSTEM_AGENTIC_DECISION_QUYET_THRESHOLD": "1.2"}):
            with self.assertRaisesRegex(ValueError, "between 0 and 1"):
                DecisionPlane().decide("Synthetic prompt")

        plane = DecisionPlane(
            quyet=lambda _, module_options=None, execution_class_options=None: {"answers": answers(module="gstack")},
            modules=lambda **_: [{"id": "gstack", "execution_ready": False}],
        )
        result = plane.decide("Review code.")
        self.assertEqual(result["choices"]["module"], "none")
        self.assertEqual(result["module_prefilter"]["status"], "no_execution_ready_candidate")

    def test_module_recommendations_are_registry_and_execution_readiness_gated(self):
        for module_id, task_type, capability, prompt in (
            ("system-one", "structured-decision", "structured.decisions", "Classify this request into categories."),
            ("orchestration", "agent-orchestration", "agent.dispatch-design", "Coordinate agents on independent tasks."),
            ("knowledge-builder", "knowledge-pack", "knowledge.pack-compile", "Compile approved sources into a knowledge pack."),
            ("pedagogy", "teaching-rehearsal", "pedagogy.rehearse", "I want a teaching rehearsal to practise teaching fractions."),
        ):
            with self.subTest(module=module_id):
                seen = []
                plane = DecisionPlane(
                    quyet=lambda _, module_options=None, execution_class_options=None, selected=module_id: {"answers": answers(module=selected)},
                    modules=lambda **kwargs: seen.append(kwargs) or [{"id": module_id, "execution_ready": False}],
                )
                result = plane.decide(prompt)
                self.assertEqual(result["choices"]["module"], "none")
                self.assertEqual(seen, [{"task_type": task_type, "capabilities": {capability}}])

    def test_natural_knowledge_pack_prompt_shortlists_only_execution_ready_builder(self):
        seen = []
        plane = DecisionPlane(
            quyet=lambda _, module_options=None, execution_class_options=None: {"answers": answers(route="DISPATCH", module="knowledge-builder")},
            modules=lambda **kwargs: seen.append(kwargs) or [{"id": "knowledge-builder", "execution_ready": True}],
        )
        result = plane.decide("Biến các nguồn này thành knowledge pack đã kiểm chứng cho agent.")
        self.assertEqual(seen, [
            {"task_type": "knowledge-pack", "capabilities": {"knowledge.pack-compile"}},
            {"task_type": "knowledge-pack", "capabilities": {"knowledge.pack-compile"}},
        ])
        self.assertEqual(result["choices"]["module"], "knowledge-builder")
        self.assertEqual(result["module_prefilter"]["status"], "shortlisted")

    def test_reverse_engineering_intent_selects_read_only_rea_capability(self):
        plane = DecisionPlane(
            quyet=lambda _, module_options=None, execution_class_options=None: {"answers": answers(module="rea", route="DIRECT")},
            modules=lambda **kwargs: [{"id": "rea", "execution_ready": True}],
        )
        result = plane.decide("Analyze this Electron application and show evidence for how search works.")
        self.assertEqual(result["choices"]["route"], "DISPATCH")
        self.assertEqual(result["choices"]["module"], "rea")
        self.assertEqual(result["capability"], "REVERSE_ENGINEERING")
        self.assertEqual(result["provider"], "rea")
        self.assertFalse(result["execution_authorized"])

    def test_direct_decision_never_calls_jev_or_queue_and_traces_have_no_prompt(self):
        prompt = "Synthetic DIRECT prompt: explain a calculator."
        plane = DecisionPlane(
            quyet=lambda _, module_options=None, execution_class_options=None: {"answers": answers()},
            jev=lambda *_: self.fail("unexpected JEV request"),
        )
        result = plane.decide(prompt)
        self.assertEqual(result["choices"]["route"], "DIRECT")
        self.assertNotIn(prompt, str(result))
        self.assertEqual(result["decision_type"], "task_routing")
        self.assertGreaterEqual(result["latency_ms"], 0)

    def test_host_sync_instructions_expose_decision_plane_and_privacy_gate(self):
        block = _instruction_block()
        self.assertIn("scripts/system-agentic decision", block)
        self.assertIn("--allow-typesafe", block)
        self.assertIn("never send private prompts", block)

    def test_module_schema_is_shortened_to_registry_options(self):
        questions = decision_questions(["none", "gstack"])
        self.assertEqual(list(questions["module"]["criteria"]), ["none", "gstack"])
        with self.assertRaisesRegex(ValueError, "registered module IDs"):
            decision_questions(["none", "unknown"])
        without_module = decision_questions(["none"])
        self.assertNotIn("module", without_module)
        risk = decision_questions(["none", "gstack"], ["READ_ONLY", "WORKSPACE_WRITE"])
        self.assertEqual(list(risk["execution_class"]["criteria"]), ["READ_ONLY", "WORKSPACE_WRITE"])

    def test_non_elevated_prompt_cannot_accept_elevated_model_choice(self):
        seen = []
        plane = DecisionPlane(
            quyet=lambda prompt, module_options=None, execution_class_options=None: seen.append(execution_class_options) or {
                "answers": answers(execution_class="ELEVATED", confidence=0.99)
            },
        )
        result = plane.decide("Explain what a database index is.")
        self.assertEqual(seen, [["READ_ONLY", "WORKSPACE_WRITE"]])
        self.assertIsNone(result["choices"]["execution_class"])
        self.assertEqual(result["host_reasoning_required"], ["execution_class"])
        self.assertFalse(result["execution_authorized"])

    def test_explicit_elevated_signal_only_advises_and_never_authorizes(self):
        seen = []
        plane = DecisionPlane(
            quyet=lambda prompt, module_options=None, execution_class_options=None: seen.append(execution_class_options) or {
                "answers": answers(route="DISPATCH", execution_class="ELEVATED", confidence=0.95)
            },
        )
        result = plane.decide("Deploy this service to production.")
        self.assertIn("ELEVATED", seen[0])
        self.assertEqual(result["choices"]["execution_class"], "ELEVATED")
        self.assertFalse(result["execution_authorized"])

    def test_jev_failure_after_low_quyet_confidence_returns_unresolved_host_fields(self):
        def failed_jev(*_):
            raise OSError("synthetic unavailable")

        plane = DecisionPlane(
            quyet=lambda prompt, module_options=None, execution_class_options=None: {
                "answers": answers(confidence=0.4)
            },
            jev=failed_jev,
        )
        result = plane.decide("Synthetic harmless prompt", allow_typesafe=True)
        self.assertEqual(result["engines"][1]["status"], "unavailable")
        self.assertEqual(result["host_reasoning_required"], ["execution_class", "route"])
        self.assertFalse(result["execution_authorized"])

    def test_gstack_registry_shortlist_and_confident_candidate_are_traced(self):
        seen = []
        plane = DecisionPlane(
            quyet=lambda prompt, module_options=None, execution_class_options=None: seen.append(module_options) or {
                "answers": answers(route="DISPATCH", module="gstack", confidence=0.88)
            },
            modules=lambda **kwargs: [{"id": "gstack", "execution_ready": True}],
        )
        result = plane.decide("Review a code diff for SQL injection and regressions.")
        self.assertEqual(seen, [["none", "gstack"]])
        self.assertEqual(result["choices"]["module"], "gstack")
        self.assertEqual(result["module_prefilter"]["options"], ["none", "gstack"])

    def test_unrelated_prompt_uses_deterministic_none_without_module_candidate_guessing(self):
        seen = []
        plane = DecisionPlane(
            quyet=lambda prompt, module_options=None, execution_class_options=None: seen.append(module_options) or {"answers": answers()},
        )
        result = plane.decide("Explain what a database index is.")
        self.assertEqual(seen, [["none"]])
        self.assertEqual(result["choices"]["module"], "none")
        self.assertEqual(result["field_sources"]["module"], "registry-prefilter")

    def test_underspecified_prompt_overrides_overconfident_route_and_stays_private_by_default(self):
        calls = []
        plane = DecisionPlane(
            quyet=lambda prompt, module_options=None, execution_class_options=None: {"answers": answers(route="DISPATCH", confidence=0.9)},
            jev=lambda prompt, fields: calls.append((prompt, fields)) or {},
        )
        result = plane.decide("Take care of this issue.")
        self.assertEqual(result["choices"]["route"], None)
        self.assertEqual(result["route_ambiguity_guard"], "requires_clarification_or_secondary_decision")
        self.assertEqual(result["host_reasoning_required"], ["route"])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
