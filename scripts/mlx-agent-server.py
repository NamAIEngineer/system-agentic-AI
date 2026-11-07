#!/usr/bin/env python3
"""Launch the private MLX server with a compatibility guard for tool calls.

Some Qwen generations produce a parsed tool-call object without an
``arguments`` member.  mlx-lm 0.32.0 assumes that member exists while
serializing the OpenAI response and the request handler crashes with a
KeyError.  The queue can recover from a tool error, but it cannot recover from
the HTTP connection being closed.  Keep the guard at this local runtime
boundary so the package remains untouched and the LaunchAgent stays
reproducible.
"""

from __future__ import annotations

import logging

from mlx_lm import server


_original_format = server.ToolCallFormatter._format


def _format_with_missing_arguments(self, tool_call):
    if isinstance(tool_call, dict):
        # Qwen occasionally emits the singular OpenAI-incompatible key
        # ``argument`` while the parser also supplies an empty ``arguments``
        # object. Preserve the useful object before mlx-lm serializes it.
        alternate = tool_call.get("argument")
        arguments = tool_call.get("arguments")
        if isinstance(alternate, dict) and ("arguments" not in tool_call or arguments in (None, {}, "{}", "")):
            logging.warning("Malformed model tool call used singular argument; normalizing it")
            tool_call = dict(tool_call)
            tool_call["arguments"] = alternate
        elif "arguments" not in tool_call:
            logging.warning(
                "Malformed model tool call missing arguments; treating arguments as {}"
            )
            tool_call = dict(tool_call)
            tool_call["arguments"] = {}
        elif arguments is None:
            logging.warning(
                "Malformed model tool call with null arguments; treating arguments as {}"
            )
            tool_call = dict(tool_call)
            tool_call["arguments"] = {}
    return _original_format(self, tool_call)


server.ToolCallFormatter._format = _format_with_missing_arguments


if __name__ == "__main__":
    server.main()
