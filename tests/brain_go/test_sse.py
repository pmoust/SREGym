"""Parser tests for the brain-go driver's SSE reader.

The wire format is brain-go's internal/chat/server/sse.go, which writes
"event: <name>\\ndata: <json>\\n\\n" per frame.
"""

from clients.brain_go.sse import iter_sse_events


def frames(*lines: str):
    return list(iter_sse_events(iter(lines)))


def test_parses_a_named_event():
    assert frames("event: token", 'data: {"accumulated":"hi"}', "") == [("token", {"accumulated": "hi"})]


def test_skips_keepalive_comments():
    """Comment lines are keepalives, not events.

    brain-go sends one every 15s on an idle stream (NOFireAI/brain-go#2250),
    matching Python's sse_starlette default. Either way a keepalive must not
    surface as an event.
    """
    assert frames(": keepalive", "event: done", "data: {}", "") == [("done", {})]


def test_keepalive_between_frames_does_not_split_them():
    assert frames("event: token", ": keepalive", 'data: {"accumulated":"x"}', "") == [("token", {"accumulated": "x"})]


def test_multiple_frames_in_one_stream():
    assert frames(
        "event: tool_start",
        'data: {"tool":"kubectl"}',
        "",
        "event: tool_end",
        'data: {"tool":"kubectl"}',
        "",
    ) == [("tool_start", {"tool": "kubectl"}), ("tool_end", {"tool": "kubectl"})]


def test_data_only_frame_defaults_to_message():
    """The AG-UI wire puts its discriminator inside the JSON, so frames there
    carry no event line (sse.go's sendData)."""
    assert frames('data: {"type":"RUN_STARTED"}', "") == [("message", {"type": "RUN_STARTED"})]


def test_multiline_data_is_joined():
    assert frames("event: response", 'data: {"content":', 'data: "split"}', "") == [("response", {"content": "split"})]


def test_unparseable_data_is_preserved_not_dropped():
    assert frames("event: error", "data: not json", "") == [("error", {"raw": "not json"})]


def test_bare_scalar_payload_does_not_break_callers():
    """A non-object payload would otherwise propagate and break .get()."""
    assert frames("event: token", "data: 42", "") == [("token", {"raw": "42"})]


def test_trailing_carriage_returns_are_stripped():
    assert frames("event: done\r", "data: {}\r", "\r") == [("done", {})]


def test_truncated_stream_still_yields_what_arrived():
    """No trailing blank line, e.g. a connection cut mid-frame."""
    assert frames("event: token", 'data: {"accumulated":"partial"}') == [("token", {"accumulated": "partial"})]


def test_none_lines_are_ignored():
    """requests' iter_lines can yield None on keepalive chunks."""
    assert list(iter_sse_events(iter(["event: done", None, "data: {}", ""]))) == [("done", {})]


def test_empty_stream_yields_nothing():
    assert frames() == []


def test_blank_line_alone_yields_nothing():
    assert frames("", "", "") == []
