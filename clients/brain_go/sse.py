"""SSE parsing for brain-go's chat stream.

Split out from the driver so the parser is testable without a live server.

The wire format is defined by brain-go's internal/chat/server/sse.go, which
carries the same event names as the Python cli_endpoint.py it was ported from:
connected, token, tool_start, tool_end, execute_local_tool,
tool_result_received, response, done, error.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any


def iter_sse_events(lines: Iterable[str | None]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Parse SSE lines into (event_name, data) pairs.

    Comment lines (`:` prefix) are skipped. Those are keepalives: brain-go
    sends one every 15 seconds on an idle stream (NOFireAI/brain-go#2250),
    matching Python's sse_starlette default. Skipping them is required either
    way, since a keepalive is not an event.
    """
    event_name: str | None = None
    data_lines: list[str] = []

    for raw_line in lines:
        if raw_line is None:
            continue

        line = raw_line.rstrip("\r")

        if line.startswith(":"):
            continue

        if line == "":
            if event_name is not None or data_lines:
                yield event_name or "message", _decode("\n".join(data_lines))

            event_name = None
            data_lines = []

            continue

        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())

    # A stream that ends without a trailing blank line still has one event in
    # hand. brain-go always terminates frames properly, but a truncated read
    # should surface what arrived rather than silently dropping it.
    if event_name is not None or data_lines:
        yield event_name or "message", _decode("\n".join(data_lines))


def _decode(raw: str) -> dict[str, Any]:
    """Decode an event's data payload, preserving unparseable text."""
    if not raw:
        return {}

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}

    # brain-go always sends a JSON object, but a bare scalar would otherwise
    # propagate as a non-dict and break every caller's .get().
    return decoded if isinstance(decoded, dict) else {"raw": raw}
