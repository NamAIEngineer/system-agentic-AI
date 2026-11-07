from datetime import datetime, timedelta, timezone
import unittest

from modules.platform_contracts import (
    PlatformContractError, PublicationReceipt, ReadinessEvidence, SessionHealth,
    SubmitEvidence, UnknownQuarantine, execution_identity, normalize_session_state,
)


STAMP = "2026-10-17T00:00:00Z"


class PlatformContractTests(unittest.TestCase):
    def test_session_aliases_and_ready_gate(self):
        self.assertEqual(normalize_session_state("human_required"), "AUTH_REQUIRED")
        self.assertEqual(normalize_session_state("blocked"), "UNKNOWN")
        self.assertTrue(SessionHealth("linkedin", "a", "p", "identity", "READY", STAMP).eligible)
        self.assertFalse(SessionHealth("reddit", "a", "p", "identity", "AUTH_REQUIRED", STAMP).eligible)

    def test_session_rejects_secrets(self):
        with self.assertRaises(PlatformContractError):
            SessionHealth("reddit", "a", "p", "identity", "READY", STAMP, {"cookie": "x"})

    def test_readiness_is_bound_and_expires(self):
        readiness = ReadinessEvidence("reddit", "a", "p", "target", "READY", STAMP, "2026-10-17T00:05:00Z", {"composer": True})
        self.assertTrue(readiness.fresh(now=datetime(2026, 10, 17, 0, 1, tzinfo=timezone.utc)))
        self.assertFalse(readiness.fresh(now=datetime(2026, 10, 17, 0, 6, tzinfo=timezone.utc)))

    def test_acceptance_activation_are_distinct(self):
        evidence = SubmitEvidence("UNKNOWN", "CONTROL_ACTIVATED", "SENT", STAMP)
        self.assertEqual(evidence.acceptance, "UNKNOWN")
        with self.assertRaises(PlatformContractError):
            SubmitEvidence("ACCEPTED", "CLICK_COMMAND_SENT", "SENT", STAMP)

    def test_verified_requires_stable_publication_identity(self):
        with self.assertRaises(PlatformContractError):
            PublicationReceipt("VERIFIED", "p", "c", "j", "v", "reddit", "a", "target")
        receipt = PublicationReceipt("VERIFIED", "p", "c", "j", "v", "reddit", "a", "target", "post-1", "https://reddit.com/r/x/comments/1", STAMP)
        self.assertEqual(receipt.state, "VERIFIED")

    def test_pending_moderation_is_not_verified(self):
        pending = PublicationReceipt("PENDING_MODERATION", "p", "c", "j", "v", "reddit", "a", "target", moderation_evidence="visible pending state")
        self.assertNotEqual(pending.state, "VERIFIED")

    def test_identity_is_stable_and_bound_to_all_dimensions(self):
        first = execution_identity(campaign_id="c", content_variant_id="v", platform="reddit", account_id="a", target="r/x")
        second = execution_identity(campaign_id="c", content_variant_id="v", platform="reddit", account_id="a", target="r/y")
        self.assertNotEqual(first, second)
        self.assertEqual(first, execution_identity(campaign_id="c", content_variant_id="v", platform="reddit", account_id="a", target="r/x"))

    def test_unknown_quarantine_never_retries(self):
        quarantine = UnknownQuarantine("exec-1", "submit_result_unknown", True)
        self.assertFalse(quarantine.retry_allowed)
        with self.assertRaises(PlatformContractError):
            UnknownQuarantine("exec-1", "unknown", True, True)


if __name__ == "__main__":
    unittest.main()
