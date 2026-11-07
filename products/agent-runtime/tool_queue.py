"""Typed execution backend adapter over the existing per-user runtime queue."""

from __future__ import annotations

from typing import Any

import agentic_runtime


class WorkerExecutionBackend:
    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        return agentic_runtime.enqueue_tool_execution(
            request["workspace"], request["execution_policy"], request["tool"],
            request["arguments"], request["wait_seconds"],
        )
