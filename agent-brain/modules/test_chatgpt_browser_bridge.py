from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest

from modules.chatgpt_browser_bridge import ChatGPTBridge, OrcaChromeTransport


class FakeTransport:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {
            "conversation_id": "conversation-1",
            "url": "https://chatgpt.com/c/conversation-1",
            "created_at": "2026-10-27T00:00:00+00:00",
            "message_id": None,
            "snippet": "harmless response",
        }

    def send(self, prompt, expected_conversation_id):
        self.calls.append((prompt, expected_conversation_id))
        return dict(self.result)


class ChatGPTBrowserBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chatgpt-browser-bridge-")
        self.addCleanup(self.temp.cleanup)
        self.transport = FakeTransport()
        self.bridge = ChatGPTBridge(self.temp.name, transport=self.transport)
        self.payload = {
            "account_id": "account-1",
            "conversation_id": "conversation-1",
            "prompt": "Return a harmless browser bridge marker.",
            "idempotency_key": "idem-1",
        }

    def test_submit_completes_and_persists_bounded_receipt(self):
        first = self.bridge.submit(self.payload)
        deadline = time.time() + 2
        result = None
        while time.time() < deadline:
            result = self.bridge.get(first["request_id"])
            if result and result.get("status") == "COMPLETED":
                break
            time.sleep(0.01)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["source"], "chatgpt-web")
        self.assertEqual(len(self.transport.calls), 1)
        saved = json.loads(Path(self.temp.name, "requests", f"{first['request_id']}.json").read_text())
        self.assertNotIn("prompt", saved)
        self.assertNotIn("cookie", json.dumps(saved).lower())

    def test_same_idempotency_returns_same_request_without_second_send(self):
        first = self.bridge.submit(self.payload)
        second = self.bridge.submit(self.payload)
        self.assertEqual(first["request_id"], second["request_id"])
        deadline = time.time() + 2
        while time.time() < deadline and len(self.transport.calls) == 0:
            time.sleep(0.01)
        self.assertEqual(len(self.transport.calls), 1)

    def test_restart_marks_inflight_unknown_and_does_not_replay(self):
        # Hold the bridge worker out of the transport so the synthetic
        # process-death state cannot race a fast fake completion.
        self.bridge.exchange_lock.acquire()
        first = self.bridge.submit(self.payload)
        path = Path(self.temp.name, "requests", f"{first['request_id']}.json")
        # Simulate a process death before the worker has a terminal receipt.
        path.write_text(json.dumps({
            "schema": 1, "request_id": first["request_id"], "status": "RUNNING",
            "account_id": "account-1", "conversation_id": "conversation-1",
            "idempotency_key": "idem-crash", "created_at": "2026-10-27T00:00:00Z",
        }))
        restarted = ChatGPTBridge(self.temp.name, transport=FakeTransport())
        receipt = restarted.get(first["request_id"])
        self.bridge.exchange_lock.release()
        self.assertEqual(receipt["status"], "UNKNOWN")
        self.assertEqual(receipt["error"], "bridge_restarted_during_exchange")

    def test_last_assistant_snippet_survives_virtualized_heading_count(self):
        before = "10 đầu đề ChatGPT đã nói:\n11 container, Text: old answer"
        after = "10 đầu đề ChatGPT đã nói:\n11 container, Text: new answer"
        self.assertEqual(OrcaChromeTransport._last_assistant_snippet(before), "old answer")
        self.assertEqual(OrcaChromeTransport._last_assistant_snippet(after), "new answer")

    def test_structured_assistant_passage_is_bounded_and_excludes_completion_marker(self):
        tree = """9 đầu đề ChatGPT đã nói:, Value: 4
10 đầu đề REVIEW_V47, Value: 2
11 text Safe review response.
12 đầu đề BLOCKED, Value: 2
13 text External action remains blocked.
14 text Đã hoàn tất phản hồi
"""
        passage = OrcaChromeTransport._last_assistant_snippet(tree)
        self.assertIn("REVIEW_V47", passage)
        self.assertIn("External action remains blocked.", passage)
        self.assertNotIn("Đã hoàn tất phản hồi", passage)

    def test_assistant_passage_preserves_workflow_marker_when_tail_is_bounded(self):
        long_tail = "REVIEW: ACCEPT " + ("bounded evidence " * 200)
        tree = "1 đầu đề ChatGPT đã nói:\n2 text " + long_tail
        passage = OrcaChromeTransport._last_assistant_snippet(tree)
        self.assertTrue(passage.startswith("REVIEW: ACCEPT"))
        self.assertLessEqual(len(passage), 1200)

    def test_send_button_uses_observed_semantic_labels(self):
        vietnamese = "917 nút Gửi"
        english = "42 button Send"
        self.assertEqual(OrcaChromeTransport._send_button_index(vietnamese), "917")
        self.assertEqual(OrcaChromeTransport._send_button_index(english), "42")

    def test_send_button_rejects_unlabeled_controls(self):
        with self.assertRaisesRegex(RuntimeError, "send button not found"):
            OrcaChromeTransport._send_button_index("42 nút")

    def test_address_bar_index_uses_fresh_tree(self):
        tree = "7 trường văn bản (settable) Thanh địa chỉ và tìm kiếm, Value: https://chatgpt.com"
        self.assertEqual(OrcaChromeTransport._address_bar_index(tree), "7")

    def test_snapshot_prefers_address_bar_conversation_over_page_links(self):
        tree = """7 trường văn bản (settable) Thanh địa chỉ và tìm kiếm, Value: chatgpt.com/c/current123
37 Nội dung HTML
38 link https://chatgpt.com/c/sidebar-old
"""
        result = {"result": {"snapshot": {"treeText": tree}}}
        snapshot = OrcaChromeTransport._parse_snapshot("50", result)
        self.assertEqual(snapshot.url, "https://chatgpt.com/c/current123")
        self.assertEqual(snapshot.conversation_id, "current123")

    def test_window_id_requires_expected_conversation_in_address_bar(self):
        transport = OrcaChromeTransport()
        calls = []
        trees = {
            "1": "7 trường văn bản (settable) Thanh địa chỉ và tìm kiếm, Value: chatgpt.com/c/old\n"
                  "38 link https://chatgpt.com/c/expected",
            "2": "7 trường văn bản (settable) Thanh địa chỉ và tìm kiếm, Value: chatgpt.com/c/expected\n"
                  "38 link https://chatgpt.com/c/old",
        }
        def fake_run(args, stdin=None):
            calls.append(args)
            if args[1] == "list-windows":
                return {"result": {"windows": [{"id": "1"}, {"id": "2"}]}}
            return {"result": {"snapshot": {"treeText": trees[args[args.index("--window-id") + 1]]}}}
        transport._run = fake_run
        self.assertEqual(transport._window_id("expected"), "2")

    def test_stable_snapshot_requires_two_matching_routes(self):
        transport = OrcaChromeTransport()
        snapshots = iter([
            type("S", (), {"conversation_id": "expected", "url": "https://chatgpt.com/c/expected"})(),
            type("S", (), {"conversation_id": "expected", "url": "https://chatgpt.com/c/expected"})(),
        ])
        transport.snapshot = lambda _expected: next(snapshots)
        result = transport.stable_snapshot("expected", timeout=1)
        self.assertEqual(result.conversation_id, "expected")

    def test_composer_quota_block_is_explicit(self):
        tree = "530 text Bạn hiện đã hết lượt dùng Công việc\n531 text Nâng cấp gói hoặc thêm credit"
        self.assertEqual(
            OrcaChromeTransport._composer_block_reason(tree),
            "CHATGPT_WORK_QUOTA_EXHAUSTED",
        )


if __name__ == "__main__":
    unittest.main()
