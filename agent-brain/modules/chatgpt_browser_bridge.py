"""Approval-gated loopback bridge for an already signed-in Chrome ChatGPT tab.

The bridge owns browser transport only. It drives the visible tab through the
installed Orca Computer Use provider and stores bounded receipts locally. It
never receives or reads cookies, browser storage, bearer tokens, or passwords.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import tempfile
import time
import uuid
from typing import Any
from urllib.parse import urlparse


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
CHATGPT_URL_RE = re.compile(r"(?:https?://)?chatgpt\.com/[^\s,]+", re.IGNORECASE)
CONVERSATION_RE = re.compile(r"/c/([A-Za-z0-9-]{8,})")
MAX_BODY_BYTES = 32 * 1024
MAX_PROMPT_CHARS = 12_000
MAX_SNIPPET_CHARS = 1_200
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8891
DEFAULT_TIMEOUT = 180.0
ORCA = "/usr/local/bin/orca"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError(f"{field} must be a path-safe identifier")
    return value


def _json_load(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _json_save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


@dataclass(frozen=True)
class BrowserSnapshot:
    window_id: str
    tree: str
    url: str | None
    conversation_id: str | None
    width: int = 1920
    height: int = 966


class OrcaChromeTransport:
    """Small subprocess transport. Every operation observes fresh UI state."""

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT):
        self.timeout = max(5.0, min(float(timeout), 180.0))

    def _run(self, args: list[str], *, stdin: str | None = None) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                [ORCA, *args], input=stdin, text=True, capture_output=True,
                timeout=self.timeout, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"orca transport unavailable: {type(exc).__name__}") from exc
        raw = completed.stdout.strip()
        if not raw:
            raise RuntimeError(f"orca transport returned no JSON (exit={completed.returncode})")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("orca transport returned invalid JSON") from exc
        if not isinstance(value, dict) or value.get("ok") is not True:
            error = value.get("error") if isinstance(value, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            raise RuntimeError(f"orca transport action failed: {code or 'UNKNOWN'}")
        return value

    def _window_id(self, expected_conversation_id: str | None = None) -> str:
        result = self._run(["computer", "list-windows", "--app", "com.google.Chrome", "--json"])
        windows = result.get("result", {}).get("windows", [])
        usable = [item for item in windows if isinstance(item, dict) and not item.get("isMinimized")]
        if not usable:
            raise RuntimeError("Chrome window not available")
        # Chrome can expose a dashboard popup before the normal browser window.
        # Inspect each visible window and select the one whose fresh AX tree
        # contains the requested ChatGPT conversation. Never guess by title or
        # by list order.
        candidates = [str(item.get("id")) for item in usable if item.get("id") is not None]
        fallback: str | None = None
        for window_id in candidates:
            try:
                state = self._run([
                    "computer", "get-app-state", "--app", "com.google.Chrome",
                    "--window-id", window_id, "--restore-window", "--json",
                ])
                tree = str(state.get("result", {}).get("snapshot", {}).get("treeText", ""))
            except RuntimeError:
                continue
            if expected_conversation_id:
                address_url = self._address_url(tree)
                address_conversation = CONVERSATION_RE.search(address_url or "")
                if address_conversation and address_conversation.group(1) == expected_conversation_id:
                    return window_id
            if "chatgpt.com" in tree.lower() and ("vùng nhập văn bản" in tree or "ChatGPT đã nói:" in tree):
                fallback = window_id
        if fallback:
            return fallback
        return candidates[0]

    @staticmethod
    def _parse_snapshot(window_id: str, result: dict[str, Any]) -> BrowserSnapshot:
        snapshot = result.get("result", {}).get("snapshot", {})
        tree = str(snapshot.get("treeText", ""))
        # The accessibility tree can contain ChatGPT links in conversation
        # content and sidebar history. The address bar is the authoritative
        # route for the visible tab; prefer it before scanning page text.
        url = OrcaChromeTransport._address_url(tree)
        if not url:
            url_match = CHATGPT_URL_RE.search(tree)
            url = url_match.group(0) if url_match else None
        if url and not url.lower().startswith("http"):
            url = "https://" + url
        conversation = CONVERSATION_RE.search(url or "")
        window = snapshot.get("window") if isinstance(snapshot.get("window"), dict) else {}
        try:
            width = max(320, int(window.get("width", 1920)))
        except (TypeError, ValueError):
            width = 1920
        try:
            height = max(240, int(window.get("height", 966)))
        except (TypeError, ValueError):
            height = 966
        return BrowserSnapshot(window_id, tree, url, conversation.group(1) if conversation else None,
                               width, height)

    @staticmethod
    def _address_url(tree: str) -> str | None:
        """Extract the visible browser route from a fresh AX tree."""
        for line in tree.splitlines():
            if "Thanh địa chỉ và tìm kiếm" not in line and "Address and search bar" not in line:
                continue
            match = re.search(r"Value:\s*(\S+)", line)
            if not match:
                continue
            value = match.group(1).rstrip(",")
            if value.lower().startswith(("http://", "https://")):
                return value
            if value.lower().startswith("chatgpt.com/"):
                return "https://" + value
        return None

    def snapshot(self, expected_conversation_id: str | None = None) -> BrowserSnapshot:
        window_id = self._window_id(expected_conversation_id)
        result = self._run([
            "computer", "get-app-state", "--app", "com.google.Chrome",
            "--window-id", window_id, "--restore-window", "--json",
        ])
        return self._parse_snapshot(window_id, result)

    def stable_snapshot(self, expected_conversation_id: str, *, timeout: float = 12.0) -> BrowserSnapshot:
        """Require two consecutive fresh AX snapshots on the exact route.

        Chrome can switch the active tab while a browser extension or another
        visible action is finishing. A single matching snapshot is insufficient
        proof that the composer belongs to the requested conversation.
        """
        deadline = time.monotonic() + max(1.0, min(float(timeout), 12.0))
        previous: BrowserSnapshot | None = None
        while time.monotonic() < deadline:
            current = self.snapshot(expected_conversation_id)
            if (current.conversation_id == expected_conversation_id
                    and current.url
                    and previous is not None
                    and previous.conversation_id == current.conversation_id
                    and previous.url == current.url):
                return current
            previous = current
            time.sleep(0.35)
        raise RuntimeError("CONVERSATION_NOT_STABLE")

    @staticmethod
    def _input_index(tree: str) -> str:
        candidates: list[tuple[int, str]] = []
        for line in tree.splitlines():
            if "vùng nhập văn bản (settable)" not in line or "Thanh địa chỉ" in line:
                continue
            match = re.match(r"\s*(\d+)\s+", line)
            if match:
                candidates.append((int(match.group(1)), match.group(1)))
        if not candidates:
            raise RuntimeError("ChatGPT composer not found in Chrome accessibility tree")
        return max(candidates)[1]

    @staticmethod
    def _address_bar_index(tree: str) -> str:
        """Find Chrome's address bar from the fresh accessibility tree."""
        for line in tree.splitlines():
            if "Thanh địa chỉ và tìm kiếm" not in line and "Address and search bar" not in line:
                continue
            match = re.match(r"\s*(\d+)\s+", line)
            if match:
                return match.group(1)
        raise RuntimeError("Chrome address bar not found in accessibility tree")

    def _restore_conversation(self, snapshot: BrowserSnapshot,
                              expected_conversation_id: str,
                              target_url: str | None = None) -> BrowserSnapshot:
        """Navigate the visible tab back to the exact conversation route.

        ChatGPT project navigation can temporarily expose the project shell
        after a send. Restoration is navigation only; it never re-enters or
        resends the prompt. The returned snapshot must prove the expected
        conversation before response parsing continues.
        """
        index = self._address_bar_index(snapshot.tree)
        url = target_url or f"https://chatgpt.com/c/{expected_conversation_id}"
        self._run([
            "computer", "set-value", "--app", "com.google.Chrome",
            "--window-id", snapshot.window_id, "--element-index", index,
            "--value", url, "--restore-window", "--no-screenshot", "--json",
        ])
        self._run([
            "computer", "press-key", "--app", "com.google.Chrome",
            "--window-id", snapshot.window_id, "--key", "Return",
            "--restore-window", "--no-screenshot", "--json",
        ])
        deadline = time.monotonic() + 12.0
        latest = snapshot
        while time.monotonic() < deadline:
            time.sleep(0.5)
            latest = self.snapshot(expected_conversation_id)
            if latest.conversation_id == expected_conversation_id:
                return latest
        raise RuntimeError("CONVERSATION_RESTORE_TIMEOUT")

    @staticmethod
    def _send_button_index(tree: str) -> str:
        candidates: list[int] = []
        for line in tree.splitlines():
            # Orca exposes the localized accessible name, not a stable DOM
            # selector. Keep this semantic and bounded: accept the known send
            # labels in the supported UI languages, never an unlabeled or
            # generic button.
            if not re.search(
                r"(?:\bnút\b\s+(?:Gửi|Gửi tin nhắn)|\bbutton\b\s+(?:Send|Send message|Submit))\s*$",
                line,
                re.IGNORECASE,
            ):
                continue
            match = re.match(r"\s*(\d+)\s+", line)
            if match:
                candidates.append(int(match.group(1)))
        if not candidates:
            raise RuntimeError("ChatGPT send button not found in Chrome accessibility tree")
        return str(max(candidates))

    def _wait_for_send_button(self, window_id: str, expected_conversation_id: str | None = None,
                              *, timeout: float = 5.0) -> tuple[BrowserSnapshot, str]:
        """Re-observe until the semantic send control is materialized.

        ChatGPT updates the composer asynchronously after AXSetValue. A fresh
        snapshot avoids clicking a stale element index while keeping the
        action fail-closed when no recognized send control appears.
        """
        deadline = time.monotonic() + max(0.5, min(timeout, 10.0))
        latest = self.snapshot(expected_conversation_id)
        focused = False
        while True:
            try:
                return latest, self._send_button_index(latest.tree)
            except RuntimeError as exc:
                if str(exc) != "ChatGPT send button not found in Chrome accessibility tree":
                    raise
                # A long conversation with the sidebar open can exhaust Orca's
                # bounded AX tree before the composer. Focus the visible
                # composer once, then re-observe; this is input preparation,
                # never a submit action.
                if not focused:
                    latest = self._focus_composer(latest, expected_conversation_id)
                    focused = True
                    continue
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
                latest = self.snapshot(expected_conversation_id)

    def _focus_composer(self, snapshot: BrowserSnapshot,
                        expected_conversation_id: str | None = None) -> BrowserSnapshot:
        """Expose a composer hidden by a bounded accessibility-tree snapshot.

        ChatGPT can render the composer at the bottom of a long conversation
        while Orca truncates the tree at the message list. A coordinate click
        is used only at the observed bottom-center of the current window to
        focus the composer; the caller must still discover and verify the
        semantic composer and send controls from a fresh tree.
        """
        try:
            self._run([
                "computer", "click", "--app", "com.google.Chrome",
                "--window-id", snapshot.window_id,
                "--x", str(snapshot.width // 2),
                "--y", str(max(1, snapshot.height - 50)),
                "--restore-window", "--no-screenshot", "--json",
            ])
        except RuntimeError:
            # The focus click is a bounded recovery aid. Preserve the original
            # tree error if the provider cannot perform it.
            raise RuntimeError("ChatGPT composer not found in Chrome accessibility tree")
        refreshed = self.snapshot(expected_conversation_id)
        if expected_conversation_id and refreshed.conversation_id != expected_conversation_id:
            raise RuntimeError("CONVERSATION_MISMATCH")
        return refreshed

    @staticmethod
    def _prompt_in_composer(tree: str, prompt: str) -> bool:
        marker = prompt[: min(96, len(prompt))]
        if not marker:
            return False
        for line in tree.splitlines():
            if "vùng nhập văn bản (settable)" in line or "text entry area (settable)" in line.lower():
                return marker in line
        return False

    @staticmethod
    def _prompt_is_latest_user_message(tree: str, prompt: str) -> bool:
        marker = prompt[: min(96, len(prompt))]
        if not marker:
            return False
        positions = [match.end() for match in re.finditer(r"(?:Bạn đã nói:|You said:|You:)", tree, re.IGNORECASE)]
        if not positions:
            return False
        segment = tree[positions[-1]:]
        assistant = re.search(r"(?:ChatGPT đã nói:|ChatGPT said:|Assistant:)", segment, re.IGNORECASE)
        if assistant:
            segment = segment[:assistant.start()]
        return marker in segment

    @staticmethod
    def _composer_block_reason(tree: str) -> str | None:
        """Return a bounded, actionable browser-side block reason."""
        lowered = tree.lower()
        if "hết lượt dùng công việc" in lowered or "usage limit" in lowered or "rate limit" in lowered:
            return "CHATGPT_WORK_QUOTA_EXHAUSTED"
        if "đã hết lượt" in lowered and ("nâng cấp" in lowered or "thêm credit" in lowered):
            return "CHATGPT_WORK_QUOTA_EXHAUSTED"
        return None

    def send(self, prompt: str, expected_conversation_id: str | None) -> dict[str, Any]:
        if expected_conversation_id:
            before = self.stable_snapshot(expected_conversation_id)
        else:
            before = self.snapshot(expected_conversation_id)
        if expected_conversation_id and before.conversation_id != expected_conversation_id:
            raise RuntimeError("CONVERSATION_MISMATCH")
        if not before.conversation_id:
            raise RuntimeError("CHATGPT_CONVERSATION_NOT_DETECTED")
        block_reason = self._composer_block_reason(before.tree)
        if block_reason:
            raise RuntimeError(block_reason)
        baseline_count = before.tree.count("ChatGPT đã nói:")
        baseline_snippet = self._last_assistant_snippet(before.tree)
        try:
            input_index = self._input_index(before.tree)
        except RuntimeError as exc:
            if str(exc) != "ChatGPT composer not found in Chrome accessibility tree":
                raise
            before = self._focus_composer(before, expected_conversation_id)
            input_index = self._input_index(before.tree)
        set_args = [
            "computer", "set-value", "--app", "com.google.Chrome", "--window-id", before.window_id,
            "--element-index", input_index, "--value-stdin", "--restore-window", "--no-screenshot", "--json",
        ]
        try:
            set_result = self._run(set_args, stdin=prompt)
        except RuntimeError as exc:
            if "element_not_found" not in str(exc):
                raise
            # AX indexes are short-lived. Re-observe and retry only this
            # idempotent composer replacement, never a send action.
            refreshed = self.snapshot(expected_conversation_id)
            if expected_conversation_id and refreshed.conversation_id != expected_conversation_id:
                raise RuntimeError("CONVERSATION_MISMATCH") from exc
            try:
                refreshed_index = self._input_index(refreshed.tree)
            except RuntimeError as input_exc:
                if str(input_exc) != "ChatGPT composer not found in Chrome accessibility tree":
                    raise
                refreshed = self._focus_composer(refreshed, expected_conversation_id)
                refreshed_index = self._input_index(refreshed.tree)
            set_args[set_args.index("--element-index") + 1] = refreshed_index
            set_result = self._run(set_args, stdin=prompt)
        action = set_result.get("result", {}).get("action", {})
        verification = action.get("verification", {}) if isinstance(action, dict) else {}
        set_verified = verification.get("state") == "verified" and verification.get("property") == "value"
        if not set_verified:
            # Chrome's contenteditable ChatGPT composer can acknowledge
            # AXSetValue while leaving its placeholder unchanged. Use the
            # provider's paste path after a fresh route check; this only fills
            # the composer and never submits the exchange.
            refreshed = self.snapshot(expected_conversation_id)
            if expected_conversation_id and refreshed.conversation_id != expected_conversation_id:
                raise RuntimeError("CONVERSATION_MISMATCH")
            if not self._prompt_in_composer(refreshed.tree, prompt):
                try:
                    composer_index = self._input_index(refreshed.tree)
                except RuntimeError as input_exc:
                    if str(input_exc) != "ChatGPT composer not found in Chrome accessibility tree":
                        raise
                    refreshed = self._focus_composer(refreshed, expected_conversation_id)
                    composer_index = self._input_index(refreshed.tree)
                self._run([
                    "computer", "click", "--app", "com.google.Chrome", "--window-id", refreshed.window_id,
                    "--element-index", composer_index, "--restore-window", "--no-screenshot", "--json",
                ])
                self._run([
                    "computer", "paste-text", "--app", "com.google.Chrome", "--window-id", refreshed.window_id,
                    "--text-stdin", "--restore-window", "--no-screenshot", "--json",
                ], stdin=prompt)
            pasted = self.snapshot(expected_conversation_id)
            if not self._prompt_in_composer(pasted.tree, prompt):
                raise RuntimeError("CHATGPT_COMPOSER_CONTENT_UNVERIFIED")
            set_verified = True
        # set-value may leave focus on a browser toolbar action. Re-observe,
        # then press the page's semantic Send control rather than relying on
        # a synthetic Return key.
        after_input, send_index = self._wait_for_send_button(before.window_id, expected_conversation_id)
        block_reason = self._composer_block_reason(after_input.tree)
        if block_reason:
            raise RuntimeError(block_reason)
        if not self._prompt_in_composer(after_input.tree, prompt):
            raise RuntimeError("CHATGPT_COMPOSER_SET_UNVERIFIED" if not set_verified else "CHATGPT_COMPOSER_CONTENT_UNVERIFIED")
        click_args = [
            "computer", "click", "--app", "com.google.Chrome", "--window-id", after_input.window_id,
            "--element-index", send_index, "--restore-window", "--no-screenshot", "--json",
        ]
        try:
            self._run(click_args)
        except RuntimeError as exc:
            if "element_not_found" not in str(exc):
                raise
            refreshed = self.snapshot(expected_conversation_id)
            if expected_conversation_id and refreshed.conversation_id != expected_conversation_id:
                raise RuntimeError("CONVERSATION_MISMATCH") from exc
            # A stale click index can fail after ChatGPT accepted the click.
            # Detect the prompt in the conversation before considering one
            # fresh-index click; never submit the same prompt twice.
            if not self._prompt_is_latest_user_message(refreshed.tree, prompt):
                if not self._prompt_in_composer(refreshed.tree, prompt):
                    raise RuntimeError("CHATGPT_SEND_OUTCOME_UNKNOWN") from exc
                refreshed, send_index = self._wait_for_send_button(
                    refreshed.window_id, expected_conversation_id
                )
                if not self._prompt_in_composer(refreshed.tree, prompt):
                    raise RuntimeError("CHATGPT_COMPOSER_CONTENT_UNVERIFIED") from exc
                click_args[click_args.index("--element-index") + 1] = send_index
                self._run(click_args)

        deadline = time.monotonic() + self.timeout
        stable = 0
        last_snippet = ""
        latest = before
        restore_attempts = 0
        last_restore_at = 0.0
        while time.monotonic() < deadline:
            time.sleep(0.75)
            latest = self.snapshot(expected_conversation_id)
            if expected_conversation_id and latest.conversation_id != expected_conversation_id:
                # ChatGPT project pages can route through the project shell
                # after a send. Restore the exact route once per interval;
                # never accept a response from another conversation and never
                # resend the prompt.
                now_monotonic = time.monotonic()
                if restore_attempts >= 2 or now_monotonic - last_restore_at < 2.0:
                    continue
                restore_attempts += 1
                last_restore_at = now_monotonic
                latest = self._restore_conversation(latest, expected_conversation_id, before.url)
            count = latest.tree.count("ChatGPT đã nói:")
            # Chrome's accessibility tree can reuse the same heading count
            # after virtualized conversation updates. Compare the rendered
            # latest assistant passage as well as the count, so a completed
            # response is not lost merely because the UI recycled nodes.
            snippet = self._last_assistant_snippet(latest.tree)
            # A completed exchange can be represented as the newest assistant
            # heading with structured child text rather than a plain `Text:`
            # node. Accept the bounded heading passage when the UI has marked
            # the response complete; never use the user prompt as a response.
            if not snippet and count > baseline_count:
                snippet = self._assistant_passage(latest.tree)
            completed_marker = "Đã hoàn tất phản hồi" in latest.tree
            changed = count > baseline_count or (snippet and snippet != baseline_snippet)
            if changed and snippet and completed_marker:
                if snippet == last_snippet:
                    stable += 1
                else:
                    stable = 0
                last_snippet = snippet
                if stable >= 1:
                    return {
                        "conversation_id": latest.conversation_id,
                        "url": latest.url,
                        "created_at": now(),
                        "message_id": None,
                        "snippet": snippet[:MAX_SNIPPET_CHARS],
                    }
        raise RuntimeError("CHATGPT_RESPONSE_TIMEOUT")

    @staticmethod
    def _assistant_snippet(tree: str, baseline_count: int) -> str:
        positions = [m.start() for m in re.finditer(r"ChatGPT đã nói:", tree)]
        if len(positions) <= baseline_count:
            return ""
        segment = tree[positions[-1]:]
        texts = re.findall(r"\n\s*\d+\s+(?:container,\s+)?Text:\s*(.+)", segment)
        cleaned: list[str] = []
        for value in texts:
            value = re.sub(r"\s+", " ", value).strip()
            if value and value not in cleaned and value not in {"Đã hoàn tất phản hồi"}:
                cleaned.append(value)
        joined = " ".join(cleaned)
        # Preserve an explicit workflow marker when the assistant passage is
        # longer than the bounded receipt. Review parsing depends on the marker
        # proving that this is a fresh review decision, while the remainder is
        # still clipped to the same limit.
        for marker in ("REVIEW:", "PLAN:"):
            index = joined.upper().find(marker)
            if index >= 0:
                return joined[index:index + MAX_SNIPPET_CHARS]
        return joined[-MAX_SNIPPET_CHARS:]

    @staticmethod
    def _last_assistant_snippet(tree: str) -> str:
        """Return the bounded latest assistant passage from a Chrome tree."""
        passage = OrcaChromeTransport._assistant_passage(tree)
        return passage

    @staticmethod
    def _assistant_passage(tree: str) -> str:
        """Extract bounded text after the newest assistant heading."""
        positions = [m.start() for m in re.finditer(r"ChatGPT đã nói:", tree)]
        if not positions:
            return ""
        segment = tree[positions[-1]:]
        cleaned: list[str] = []
        for line in segment.splitlines():
            match = re.match(r"\s*\d+\s+(?:container,\s+)?Text:\s*(.+)", line)
            if not match:
                match = re.match(r"\s*\d+\s+text\s+(.+)", line, re.IGNORECASE)
            if not match:
                match = re.match(r"\s*\d+\s+đầu đề\s+(.+?)(?:,\s*Value:\s*\d+)?$", line, re.IGNORECASE)
            if not match:
                continue
            value = re.sub(r"\s+", " ", match.group(1)).strip()
            if value and value not in cleaned and value not in {"Đã hoàn tất phản hồi", "Đầu ra", "Nguồn"}:
                cleaned.append(value)
        joined = " ".join(cleaned)
        for marker in ("REVIEW:", "PLAN:"):
            index = joined.upper().find(marker)
            if index >= 0:
                return joined[index:index + MAX_SNIPPET_CHARS]
        return joined[-MAX_SNIPPET_CHARS:]


class ChatGPTBridge:
    def __init__(self, state_root: str | Path, *, transport: OrcaChromeTransport | None = None,
                 timeout: float = DEFAULT_TIMEOUT, auth_file: str | Path | None = None):
        self.root = Path(state_root).expanduser()
        self.requests = self.root / "requests"
        self.root.mkdir(parents=True, exist_ok=True)
        self.requests.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "idempotency.json"
        self.transport = transport or OrcaChromeTransport(timeout=timeout)
        self.timeout = timeout
        self.auth_file = Path(auth_file or os.environ.get("SYSTEM_AGENTIC_CHATGPT_BRIDGE_AUTH_FILE", "")).expanduser() if (auth_file or os.environ.get("SYSTEM_AGENTIC_CHATGPT_BRIDGE_AUTH_FILE")) else None
        self.lock = threading.RLock()
        self.exchange_lock = threading.Lock()
        self.index: dict[str, str] = _json_load(self.index_path) or {}
        # Prompts live only in memory while an exchange is running. This keeps
        # persistent receipts free of task text and full chat content.
        self._prompts: dict[str, str] = {}
        self._recover_inflight()

    def _recover_inflight(self) -> None:
        for path in self.requests.glob("*.json"):
            state = _json_load(path)
            if state and state.get("status") in {"SUBMITTED", "RUNNING"}:
                state.update({"status": "UNKNOWN", "error": "bridge_restarted_during_exchange", "updated_at": now()})
                _json_save(path, state)

    def _path(self, request_id: str) -> Path:
        return self.requests / f"{safe_id(request_id, 'request_id')}.json"

    @staticmethod
    def _validate(payload: dict[str, Any]) -> tuple[str, str | None, str, str]:
        account_id = safe_id(payload.get("account_id"), "account_id")
        conversation_id = payload.get("conversation_id")
        if conversation_id is not None:
            conversation_id = safe_id(conversation_id, "conversation_id")
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError(f"prompt exceeds {MAX_PROMPT_CHARS} characters")
        idempotency = safe_id(payload.get("idempotency_key"), "idempotency_key")
        return account_id, conversation_id, prompt, idempotency

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        account_id, conversation_id, prompt, idempotency = self._validate(payload)
        with self.lock:
            existing_id = self.index.get(idempotency)
            if existing_id:
                existing = _json_load(self._path(existing_id))
                if existing:
                    # A preflight conversation mismatch happens before the
                    # prompt is entered or sent. Re-arm that same durable
                    # request once after the caller has restored the tab.
                    # Keep the request ID and idempotency key: this is a
                    # bounded recovery, never a second exchange.
                    retryable_preflight = {
                        "CONVERSATION_MISMATCH",
                        "orca transport action failed: element_not_found",
                    }
                    if (
                        existing.get("status") == "FAILED"
                        and existing.get("error") in retryable_preflight
                        and not existing.get("url")
                        and not existing.get("message_id")
                        and not existing.get("snippet")
                        and int(existing.get("retry_count", 0) or 0) < 3
                    ):
                        existing.update({
                            "status": "SUBMITTED",
                            "error": None,
                            "retry_count": int(existing.get("retry_count", 0) or 0) + 1,
                            "updated_at": now(),
                        })
                        _json_save(self._path(existing_id), existing)
                        self._prompts[existing_id] = prompt
                        threading.Thread(
                            target=self._run, args=(existing_id, prompt), daemon=True,
                            name=f"chatgpt-bridge-{existing_id}-recovery",
                        ).start()
                    return self._public(existing)
            request_id = f"xchg-{uuid.uuid4().hex[:20]}"
            state = {
                "schema": 1, "request_id": request_id, "status": "SUBMITTED",
                "account_id": account_id, "conversation_id": conversation_id,
                "idempotency_key": idempotency,
                "created_at": now(), "updated_at": now(),
            }
            self.index[idempotency] = request_id
            _json_save(self.index_path, self.index)
            _json_save(self._path(request_id), state)
            self._prompts[request_id] = prompt
            threading.Thread(target=self._run, args=(request_id, prompt), daemon=True,
                             name=f"chatgpt-bridge-{request_id}").start()
            return self._public(state)

    def _run(self, request_id: str, prompt: str) -> None:
        if not self.exchange_lock.acquire(blocking=False):
            self._finish(request_id, "BUSY", error="another_exchange_is_running")
            return
        try:
            state = _json_load(self._path(request_id))
            if not state or state.get("status") != "SUBMITTED":
                return
            state.update({"status": "RUNNING", "updated_at": now()})
            _json_save(self._path(request_id), state)
            try:
                result = self.transport.send(prompt, state.get("conversation_id"))
                state.update(result)
                state.update({"status": "COMPLETED", "source": "chatgpt-web",
                              "source_id": f"chatgpt-web:{state['account_id']}", "updated_at": now()})
            except Exception as exc:  # bounded receipt; never expose prompt or command output
                state.update({"status": "FAILED", "error": str(exc)[:200], "updated_at": now()})
            _json_save(self._path(request_id), state)
            self._prompts.pop(request_id, None)
        finally:
            self.exchange_lock.release()

    def _finish(self, request_id: str, status: str, *, error: str) -> None:
        path = self._path(request_id)
        state = _json_load(path) or {"request_id": request_id}
        # Startup recovery owns an in-flight request once it has marked it
        # UNKNOWN. A pre-restart worker may still wake and report BUSY; never
        # let that late observation overwrite the durable crash boundary.
        if state.get("status") == "UNKNOWN" and state.get("error") == "bridge_restarted_during_exchange":
            return
        state.update({"status": status, "error": error, "updated_at": now()})
        _json_save(path, state)

    @staticmethod
    def _public(state: dict[str, Any]) -> dict[str, Any]:
        keys = ("request_id", "status", "conversation_id", "url", "message_id", "created_at",
                "updated_at", "source", "source_id", "snippet", "idempotency_key", "error",
                "retry_count")
        return {key: state[key] for key in keys if key in state}

    def get(self, request_id: str) -> dict[str, Any] | None:
        state = _json_load(self._path(request_id))
        return self._public(state) if state else None

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "service": "chatgpt-browser-bridge", "transport": "orca-computer-use",
                "loopback_only": True, "state_root": str(self.root), "approval_enforced_by": "chatgpt-web-adapter"}

    def authorized(self, headers: Any) -> bool:
        if self.auth_file is None:
            return False
        try:
            expected = self.auth_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return False
        supplied = str(headers.get("Authorization", ""))
        return bool(expected) and supplied == f"Bearer {expected}"


class _Handler(BaseHTTPRequestHandler):
    bridge: ChatGPTBridge

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send(self, status: int, value: dict[str, Any]) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if not self.bridge.authorized(self.headers):
            self._send(401, {"status": "UNAUTHORIZED"})
            return
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, self.bridge.health())
            return
        prefix = "/v1/chatgpt/exchanges/"
        if parsed.path.startswith(prefix):
            item = self.bridge.get(parsed.path[len(prefix):])
            self._send(200, item) if item else self._send(404, {"status": "NOT_FOUND"})
            return
        self._send(404, {"status": "NOT_FOUND"})

    def do_POST(self) -> None:  # noqa: N802
        if not self.bridge.authorized(self.headers):
            self._send(401, {"status": "UNAUTHORIZED"})
            return
        if urlparse(self.path).path != "/v1/chatgpt/exchanges":
            self._send(404, {"status": "NOT_FOUND"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("request body exceeds bounded size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
            self._send(202, self.bridge.submit(payload))
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            self._send(400, {"status": "INPUT_INVALID", "error": str(exc)})
        except Exception:
            self._send(503, {"status": "UNAVAILABLE", "error": "bridge_unavailable"})


def serve(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
          state_root: str | Path | None = None) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("ChatGPT bridge only permits loopback binding")
    root = state_root or os.environ.get(
        "SYSTEM_AGENTIC_CHATGPT_BRIDGE_STATE_ROOT",
        str(Path.home() / "Library" / "Application Support" / "SystemAgenticAI" / "chatgpt-bridge"),
    )
    try:
        timeout = max(10.0, min(float(os.environ.get("SYSTEM_AGENTIC_CHATGPT_BRIDGE_TIMEOUT", "180")), 180.0))
    except (TypeError, ValueError):
        timeout = 120.0
    bridge = ChatGPTBridge(root, timeout=timeout)
    handler = type("ChatGPTBridgeHandler", (_Handler,), {"bridge": bridge})
    server = ThreadingHTTPServer((host, port), handler)
    service_path = Path(root) / "service.json"
    _json_save(service_path, {"status": "RUNNING", "pid": os.getpid(), "host": host,
                              "port": port, "updated_at": now()})
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        _json_save(service_path, {"status": "STOPPED", "pid": os.getpid(), "host": host,
                                  "port": port, "updated_at": now()})


if __name__ == "__main__":
    serve(host=os.environ.get("SYSTEM_AGENTIC_CHATGPT_BRIDGE_HOST", DEFAULT_HOST),
          port=int(os.environ.get("SYSTEM_AGENTIC_CHATGPT_BRIDGE_PORT", str(DEFAULT_PORT))))
