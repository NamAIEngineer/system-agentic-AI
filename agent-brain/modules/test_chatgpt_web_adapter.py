from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from modules.chatgpt_web_adapter import ChatGPTWebAdapter


class ChatGPTWebAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="chatgpt-adapter-")
        self.addCleanup(self.temp.cleanup)
        self.adapter = ChatGPTWebAdapter(state_root=self.temp.name)
        self.request = {
            "run_id": "run-1",
            "account_id": "chatgpt-account-1",
            "conversation_id": "conversation-1",
            "prompt": "Return a harmless adapter smoke marker.",
        }

    def test_dry_run_requires_approval_and_persists_bounded_receipt(self):
        result = self.adapter.dry_run(self.request)
        self.assertEqual(result["status"], "DRY_RUN")
        self.assertTrue(result["approval_required"])
        self.assertFalse(result["content_sent"])
        self.assertTrue(Path(result["receipt_ref"]).is_file())
        saved = json.loads(Path(result["receipt_ref"]).read_text())
        self.assertNotIn("token", json.dumps(saved).lower())

    def test_send_fails_closed_without_approval_or_bridge(self):
        result = self.adapter.send(self.request)
        self.assertEqual(result["status"], "APPROVAL_REQUIRED")
        self.assertFalse(result["content_sent"])

    def test_approval_binding_prevents_cross_run_reuse(self):
        approval = self.adapter.record_approval(self.request, approval_id="approval-1", approved_by="human")
        self.assertEqual(approval["status"], "APPROVED")
        altered = dict(self.request, prompt="different prompt", approval_id="approval-1")
        result = self.adapter.send(altered)
        self.assertEqual(result["status"], "APPROVAL_REQUIRED")

    def test_live_exchange_is_bounded_and_persists_provenance(self):
        request = dict(self.request, approval_id="approval-2")
        self.adapter.record_approval(request, approval_id="approval-2", approved_by="human")
        responses = iter([
            {"request_id": "request-1", "status": "SUBMITTED"},
            {"request_id": "request-1", "status": "COMPLETED", "conversation_id": "conversation-1",
             "message_id": "message-1", "url": "https://chatgpt.com/c/conversation-1",
             "source_id": "chatgpt-web:account-1", "created_at": "2026-10-27T00:00:00Z", "snippet": "smoke marker"},
        ])
        with patch.object(self.adapter, "_request", side_effect=lambda *_args, **_kwargs: next(responses)):
            result = self.adapter.send(request)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["source"], "chatgpt-web")
        self.assertEqual(result["source_id"], "chatgpt-web:account-1")
        self.assertEqual(result["url"], "https://chatgpt.com/c/conversation-1")
        self.assertTrue(result["receipt_ref"].endswith("exchange-run-1.json"))
        self.assertEqual(result["message_id"], "message-1")
        self.assertTrue(Path(self.temp.name, "exchange-run-1.json").is_file())

    def test_invalid_channel_is_rejected(self):
        result = self.adapter.dry_run(dict(self.request, channel="unknown"))
        self.assertEqual(result["status"], "INPUT_INVALID")
        self.assertIn("channel must be regular or work", result["error"])

    def test_idempotency_separates_regular_and_work_routes(self):
        regular = self.adapter.validate(self.request)
        work = self.adapter.validate(dict(self.request, channel="work"))
        self.assertNotEqual(regular.idempotency_key, work.idempotency_key)

    def test_routing_status_keeps_work_account_separate(self):
        routing = Path(self.temp.name, "routing.json")
        routing.write_text(json.dumps({
            "default_route": "regular", "fallback_routes": ["work"],
            "routes": {
                "regular": {"enabled": True, "bridge_url_env": "REGULAR_URL",
                             "default_account_id": "regular"},
                "work": {"enabled": True, "bridge_url_env": "WORK_URL",
                          "default_account_id": "work"},
            },
        }), encoding="utf-8")
        with patch.dict("os.environ", {"REGULAR_URL": "http://127.0.0.1:8891",
                                        "WORK_URL": "http://127.0.0.1:8892",
                                        "SYSTEM_AGENTIC_REGULAR_ACCOUNT_ID": "",
                                        "SYSTEM_AGENTIC_WORK_ACCOUNT_ID": ""}, clear=False):
            status = ChatGPTWebAdapter(state_root=self.temp.name, routing_config=routing).routing_status()
        self.assertEqual(status["routes"]["regular"]["account_id"], "regular")
        self.assertEqual(status["routes"]["work"]["account_id"], "work")

    def test_conversation_mismatch_does_not_fallback(self):
        routing = Path(self.temp.name, "routing.json")
        routing.write_text(json.dumps({
            "default_route": "regular", "fallback_routes": ["work"],
            "fallback_errors": ["UNAVAILABLE", "CHATGPT_WORK_QUOTA_EXHAUSTED"],
            "routes": {
                "regular": {"enabled": True, "bridge_url_env": "REGULAR_URL", "default_account_id": "regular"},
                "work": {"enabled": True, "bridge_url_env": "WORK_URL", "default_account_id": "work"},
            },
        }), encoding="utf-8")
        with patch.dict("os.environ", {"REGULAR_URL": "http://127.0.0.1:8891",
                                        "WORK_URL": "http://127.0.0.1:8892"}, clear=False):
            adapter = ChatGPTWebAdapter(state_root=self.temp.name, routing_config=routing)
            request = dict(self.request, approval_id="approval-mismatch", work_account_id="work",
                           fallback_enabled=True, fallback_idempotency_key="idem-work-mismatch",
                           fallback_approval_id="approval-work-mismatch")
            adapter.record_approval(request, approval_id="approval-mismatch", approved_by="human")
            adapter.record_approval(dict(request, account_id="work", channel="work", idempotency_key="idem-work-mismatch"),
                                    approval_id="approval-work-mismatch", approved_by="human")
            responses = iter([
                {"request_id": "regular-request", "status": "SUBMITTED"},
                {"request_id": "regular-request", "status": "FAILED",
                 "error": "CONVERSATION_MISMATCH"},
            ])
            with patch.object(adapter, "_request", side_effect=lambda *_args, **_kwargs: next(responses)):
                result = adapter.send(request)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"], "CONVERSATION_MISMATCH")
        self.assertNotIn("fallback_from", result)

    def test_regular_first_work_fallback_is_bounded_and_receipted(self):
        routing = Path(self.temp.name, "routing.json")
        routing.write_text(json.dumps({
            "default_route": "regular", "fallback_routes": ["work"],
            "fallback_errors": ["UNAVAILABLE", "URLError"],
            "routes": {
                "regular": {"enabled": True, "bridge_url_env": "REGULAR_URL", "auth_file_env": "REGULAR_AUTH", "default_account_id": "regular"},
                "work": {"enabled": True, "bridge_url_env": "WORK_URL", "auth_file_env": "WORK_AUTH", "default_account_id": "work"},
            },
        }), encoding="utf-8")
        with patch.dict("os.environ", {"REGULAR_URL": "http://127.0.0.1:8891", "WORK_URL": "http://127.0.0.1:8892"}, clear=False):
            adapter = ChatGPTWebAdapter(state_root=self.temp.name, routing_config=routing)
            request = dict(self.request, approval_id="approval-2", work_account_id="work", fallback_enabled=True,
                           fallback_idempotency_key="idem-work", fallback_approval_id="approval-work")
            adapter.record_approval(request, approval_id="approval-2", approved_by="human")
            adapter.record_approval(dict(request, account_id="work", channel="work", idempotency_key="idem-work"),
                                    approval_id="approval-work", approved_by="human")
            responses = iter([
                urllib.error.URLError("regular unavailable"),
                {"request_id": "work-request", "status": "SUBMITTED"},
                {"request_id": "work-request", "status": "COMPLETED", "conversation_id": "conversation-1",
                 "source_id": "chatgpt-web:work", "created_at": "2026-10-27T00:00:00Z", "snippet": "work response"},
            ])
            def request_call(*_args, **_kwargs):
                value = next(responses)
                if isinstance(value, Exception):
                    raise value
                return value
            with patch.object(adapter, "_request", side_effect=request_call):
                result = adapter.send(request)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["channel"], "work")
        self.assertEqual(result["fallback_from"], "regular")
        self.assertEqual([item["channel"] for item in result["route_attempts"]], ["regular", "work"])

    def test_unknown_regular_result_does_not_fallback(self):
        routing = Path(self.temp.name, "routing.json")
        routing.write_text(json.dumps({
            "default_route": "regular", "fallback_routes": ["work"], "fallback_errors": ["UNAVAILABLE"],
            "routes": {"regular": {"enabled": True, "bridge_url_env": "REGULAR_URL", "default_account_id": "regular"},
                       "work": {"enabled": True, "bridge_url_env": "WORK_URL", "default_account_id": "work"}},
        }), encoding="utf-8")
        with patch.dict("os.environ", {"REGULAR_URL": "http://127.0.0.1:8891", "WORK_URL": "http://127.0.0.1:8892"}, clear=False):
            adapter = ChatGPTWebAdapter(state_root=self.temp.name, routing_config=routing)
            request = dict(self.request, approval_id="approval-3", work_account_id="work", fallback_enabled=True,
                           fallback_idempotency_key="idem-work", fallback_approval_id="approval-work")
            adapter.record_approval(request, approval_id="approval-3", approved_by="human")
            with patch.object(adapter, "_request", side_effect=urllib.error.URLError("unknown")):
                result = adapter.send(request)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertNotIn("fallback_from", result)

    def test_http_429_is_classified_as_quota_and_can_fallback(self):
        routing = Path(self.temp.name, "routing.json")
        routing.write_text(json.dumps({
            "default_route": "regular", "fallback_routes": ["work"],
            "fallback_errors": ["CHATGPT_WORK_QUOTA_EXHAUSTED"],
            "routes": {
                "regular": {"enabled": True, "bridge_url_env": "REGULAR_URL", "default_account_id": "regular"},
                "work": {"enabled": True, "bridge_url_env": "WORK_URL", "default_account_id": "work"},
            },
        }), encoding="utf-8")
        with patch.dict("os.environ", {"REGULAR_URL": "http://127.0.0.1:8891", "WORK_URL": "http://127.0.0.1:8892"}, clear=False):
            adapter = ChatGPTWebAdapter(state_root=self.temp.name, routing_config=routing)
            request = dict(self.request, approval_id="approval-429", work_account_id="work",
                           fallback_enabled=True, fallback_idempotency_key="idem-work-429",
                           fallback_approval_id="approval-work-429")
            adapter.record_approval(request, approval_id="approval-429", approved_by="human")
            adapter.record_approval(dict(request, account_id="work", channel="work", idempotency_key="idem-work-429"),
                                    approval_id="approval-work-429", approved_by="human")
            responses = iter([
                urllib.error.HTTPError("http://127.0.0.1:8891", 429, "quota", {}, None),
                {"request_id": "work-429", "status": "SUBMITTED"},
                {"request_id": "work-429", "status": "COMPLETED", "conversation_id": "conversation-1",
                 "source_id": "chatgpt-web:work", "created_at": "2026-10-27T00:00:00Z", "snippet": "work response"},
            ])
            def request_call(*_args, **_kwargs):
                value = next(responses)
                if isinstance(value, BaseException):
                    raise value
                return value
            with patch.object(adapter, "_request", side_effect=request_call):
                result = adapter.send(request)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["fallback_from"], "regular")
        self.assertEqual(result["route_attempts"][0]["error"], "CHATGPT_WORK_QUOTA_EXHAUSTED")

    def test_http_auth_errors_never_fallback(self):
        routing = Path(self.temp.name, "routing.json")
        routing.write_text(json.dumps({
            "default_route": "regular", "fallback_routes": ["work"],
            "fallback_errors": ["CHATGPT_WORK_QUOTA_EXHAUSTED", "UNAVAILABLE", "URLError"],
            "routes": {
                "regular": {"enabled": True, "bridge_url_env": "REGULAR_URL", "default_account_id": "regular"},
                "work": {"enabled": True, "bridge_url_env": "WORK_URL", "default_account_id": "work"},
            },
        }), encoding="utf-8")
        with patch.dict("os.environ", {"REGULAR_URL": "http://127.0.0.1:8891", "WORK_URL": "http://127.0.0.1:8892"}, clear=False):
            adapter = ChatGPTWebAdapter(state_root=self.temp.name, routing_config=routing)
            request = dict(self.request, approval_id="approval-401", work_account_id="work",
                           fallback_enabled=True, fallback_idempotency_key="idem-work-401",
                           fallback_approval_id="approval-work-401")
            adapter.record_approval(request, approval_id="approval-401", approved_by="human")
            adapter.record_approval(dict(request, account_id="work", channel="work", idempotency_key="idem-work-401"),
                                    approval_id="approval-work-401", approved_by="human")
            with patch.object(adapter, "_request", side_effect=urllib.error.HTTPError(
                    "http://127.0.0.1:8891", 401, "unauthorized", {}, None)):
                result = adapter.send(request)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["error"], "HTTP_401")
        self.assertNotIn("fallback_from", result)


if __name__ == "__main__":
    unittest.main()
