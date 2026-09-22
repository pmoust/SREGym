"""seed_connections.py tags each observation endpoint with the signal it answers.

Bucket alone maps prometheus, jaeger and loki onto the one observation bucket,
so brain-go's metrics, logs and traces agents held the same nine tools in
run-20260921-1250. The `signals` key is what splits them.
"""

from scripts.benchmark.seed_connections import ENDPOINTS, SIGNALS, build_config

# The values brain-go's custom-tool scoping accepts for the three observation
# nodes this benchmark exercises (customScopeSignals in
# internal/chat/tools/registry.go). A value outside this set is dropped there
# with a warning and leaves the connection unscoped.
ACCEPTED = {"metrics", "logs", "traces"}


def test_every_observation_endpoint_declares_one_accepted_signal():
    for _, path, buckets, _ in ENDPOINTS:
        if buckets == ["observation"]:
            assert path in SIGNALS, f"{path} is in the observation bucket and declares no signal"
            assert len(SIGNALS[path]) == 1, f"{path} must answer exactly one evidence domain"
            assert set(SIGNALS[path]) <= ACCEPTED, f"{path} declares a signal brain-go would drop"


def test_the_three_observation_endpoints_declare_three_distinct_signals():
    declared = [SIGNALS[path][0] for _, path, buckets, _ in ENDPOINTS if buckets == ["observation"]]

    assert sorted(declared) == ["logs", "metrics", "traces"]


def test_the_state_endpoint_declares_no_signal():
    """The state node applies no signal filter and "state" is not an accepted
    value, so a key there would only produce brain-go's warning."""
    for _, path, buckets, _ in ENDPOINTS:
        if buckets == ["state"]:
            assert path not in SIGNALS


def test_build_config_writes_signals_only_when_given():
    tools = [{"name": "get_metrics", "inputSchema": {"type": "object"}}]

    scoped = build_config("http://10.0.1.5:9954", "prometheus", False, "sid", tools, ["metrics"])
    assert scoped["signals"] == ["metrics"]

    unscoped = build_config("http://10.0.1.5:9954", "kubectl", True, "sid", tools, None)
    assert "signals" not in unscoped

    empty = build_config("http://10.0.1.5:9954", "kubectl", True, "sid", tools, [])
    assert "signals" not in empty


def test_build_config_keeps_the_rest_of_the_config_unchanged():
    tools = [{"name": "get_metrics", "inputSchema": {"type": "object"}}]

    with_signals = build_config("http://10.0.1.5:9954", "prometheus", False, "sid", tools, ["metrics"])
    without = build_config("http://10.0.1.5:9954", "prometheus", False, "sid", tools)
    del with_signals["signals"]

    assert with_signals == without
