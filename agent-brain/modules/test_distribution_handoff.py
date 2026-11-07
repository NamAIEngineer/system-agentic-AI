from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.distribution_handoff import (
    AccountSessionState, DistributionHandoffAuthorization, DistributionHandoffRequest,
    HandoffError, MarketingSeedingHandoffAdapter, PublicationMetricIdentity,
    capability_matrix,
)


NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)
STAMP = NOW.isoformat().replace("+00:00", "Z")


def auth(mode="DRY_RUN"):
    return DistributionHandoffAuthorization("auth-1", "product-1", "intent-1", ("variant-a", "variant-b"), ("media-1",), ("linkedin", "reddit", "facebook", "threads"), mode, "2026-10-18T00:00:00Z", STAMP)


def request(mode="DRY_RUN"):
    return DistributionHandoffRequest("request-1", "product-1", "intent-1", "package-1", ("variant-a", "variant-b"), ("media-1",), {"media-1": "a" * 64}, ("linkedin", "reddit"), mode, {"pipelineId": "p-1", "sourceRefs": ["artifact-1"]}, STAMP)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Path(self.temp.name) / "dry-run.json"
        self.adapter = MarketingSeedingHandoffAdapter(self.store)
        self.variants = [
            {"contentVariantId": "variant-a", "productId": "product-1", "text": "A", "sourceArtifactRefs": ["artifact-1"], "metadata": {"provenance": "product-1"}},
            {"contentVariantId": "variant-b", "productId": "product-1", "text": "B", "sourceArtifactRefs": ["artifact-1"], "metadata": {"provenance": "product-1"}},
        ]
        self.media = [{"mediaArtifactId": "media-1", "productId": "product-1", "metadata": {"sha256": "a" * 64}}]

    def tearDown(self): self.temp.cleanup()

    def test_dry_run_maps_variants_products_and_isolates_store(self):
        result = self.adapter.handoff(request(), auth(), content_variants=self.variants, media_artifacts=self.media, now=NOW)
        self.assertEqual(result["mode"], "DRY_RUN")
        self.assertEqual(len(result["jobIds"]), 4)
        self.assertFalse(result["canonicalStateTouched"])
        state = json.loads(self.store.read_text())
        self.assertEqual({j["contentVariantId"] for j in state["jobs"]}, {"variant-a", "variant-b"})
        self.assertTrue(all(not j["liveDispatch"] and not j["publishClicked"] for j in state["jobs"]))

    def test_repeat_is_idempotent_without_duplicate_jobs(self):
        self.adapter.handoff(request(), auth(), content_variants=self.variants, media_artifacts=self.media, now=NOW)
        self.adapter.handoff(request(), auth(), content_variants=self.variants, media_artifacts=self.media, now=NOW)
        state = json.loads(self.store.read_text())
        self.assertEqual(len(state["campaigns"]), 1)
        self.assertEqual(len(state["jobs"]), 4)

    def test_distinct_products_get_distinct_campaign_and_job_identities(self):
        first = self.adapter.handoff(request(), auth(), content_variants=self.variants, media_artifacts=self.media, now=NOW)
        second_request = DistributionHandoffRequest(
            "request-2", "product-2", "intent-2", "package-2", ("variant-c",), (), {}, ("linkedin",), created_at=STAMP,
            provenance={"pipelineId": "pipeline-2"})
        second_auth = DistributionHandoffAuthorization(
            "auth-2", "product-2", "intent-2", ("variant-c",), (), ("linkedin",), "DRY_RUN", "2026-10-18T00:00:00Z", STAMP)
        second = self.adapter.handoff(second_request, second_auth,
                                      content_variants=[{"contentVariantId": "variant-c", "productId": "product-2", "text": "C"}],
                                      media_artifacts=(), now=NOW)
        self.assertNotEqual(first["campaignId"], second["campaignId"])
        state = json.loads(self.store.read_text())
        self.assertEqual({c["productId"] for c in state["campaigns"]}, {"product-1", "product-2"})
        self.assertEqual({j["productId"] for j in state["jobs"]}, {"product-1", "product-2"})

    def test_live_fails_closed(self):
        with self.assertRaisesRegex(HandoffError, "LIVE"):
            self.adapter.handoff(request("LIVE"), auth("LIVE"), content_variants=self.variants, media_artifacts=self.media, now=NOW)

    def test_authorization_and_provenance_integrity(self):
        with self.assertRaises(HandoffError):
            self.adapter.handoff(request(), auth(), content_variants=[{**self.variants[0], "productId": "product-2"}, self.variants[1]], media_artifacts=self.media, now=NOW)
        with self.assertRaises(HandoffError):
            self.adapter.handoff(request(), auth(), content_variants=self.variants, media_artifacts=[{"mediaArtifactId": "media-1", "productId": "product-1", "metadata": {"sha256": "b" * 64}}], now=NOW)

    def test_secret_and_social_details_rejected(self):
        with self.assertRaises(HandoffError):
            DistributionHandoffRequest("r", "p", "i", "pkg", ("v",), (), {}, ("linkedin",), provenance={"browserProfileId": "x"}, created_at=STAMP)
        with self.assertRaises(HandoffError):
            self.adapter.handoff(request(), auth(), content_variants=[{**self.variants[0], "metadata": {"selector": "button"}}, self.variants[1]], media_artifacts=self.media, now=NOW)

    def test_session_states_only_ready_is_eligible(self):
        for state in ("READY", "AUTH_REQUIRED", "CHALLENGED", "LOCKED", "COOLDOWN", "DISABLED", "UNKNOWN"):
            record = AccountSessionState("reddit", "acct", "profile", "https://reddit.com/u/acct", state, STAMP)
            self.assertEqual(record.eligible(), state == "READY")

    def test_capability_matrix_does_not_overstate_blocked_platforms(self):
        rows = {row["platform"]: row for row in capability_matrix(session_states={"linkedin": "READY", "reddit": "AUTH_REQUIRED", "facebook": "CHALLENGED", "threads": "UNKNOWN"})}
        self.assertEqual(rows["linkedin"]["adapterStatus"], "PRODUCTION_READY")
        self.assertEqual(rows["reddit"]["adapterStatus"], "HUMAN_INTERVENTION_REQUIRED")
        self.assertEqual(rows["facebook"]["adapterStatus"], "HUMAN_INTERVENTION_REQUIRED")
        self.assertEqual(rows["threads"]["adapterStatus"], "DISCOVERY")
        self.assertFalse(rows["reddit"]["liveExactDispatch"])

    def test_publication_metric_identity(self):
        identity = PublicationMetricIdentity("p", "c", "j", "v", "linkedin", "acct", "target", "post", "https://linkedin.com/feed/update/x", STAMP)
        self.assertEqual(identity.to_dict()["jobId"], "j")


if __name__ == "__main__": unittest.main()
