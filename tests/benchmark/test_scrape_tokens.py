"""scrape_tokens.sh decides which brain-go metrics survive into a run's archive.

Its filter is a prefix allowlist, and both ways of getting it wrong are silent.
A prefix left off the list drops a counter that brain-go emits and nothing
reports the loss: `custom_mcp_breaker_trips_total` was archived nowhere for a
month that way. A pattern that keeps everything instead fills every per-shard,
per-attempt `.prom` file with go_* and process_* series, and that shows up as a
larger archive rather than as an error.

Every test here runs the script with `curl` and `docker` stubbed on PATH and
asserts on the file the script writes. Reading the pattern out of the source
would not separate those cases: a test that greps for the allowlist stays green
when the pattern is reverted to the two metrics it used to keep, because the
string it looks for is still there.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "benchmark" / "scrape_tokens.sh"

# The script needs mktemp, date, grep, awk, head and mv; the stubs supply curl
# and docker. Nothing else on the real PATH is wanted, so a real docker daemon
# or a real endpoint on :9091 cannot influence a result.
_SYSTEM_PATH = ":".join(p for p in ("/usr/bin", "/bin", "/usr/local/bin") if Path(p).is_dir())

# One brain-go /metrics payload, split into the parts a test needs to vary.
# Taken from the real exposition format: HELP and TYPE lines precede each
# family, histograms expand into buckets plus sum and count.
_RUNTIME = """\
# HELP go_gc_duration_seconds A summary of the wall-time pause distribution.
# TYPE go_gc_duration_seconds summary
go_gc_duration_seconds{quantile="0"} 4.1917e-05
go_gc_duration_seconds{quantile="1"} 0.001443666
go_gc_duration_seconds_sum 0.108241266
go_gc_duration_seconds_count 1832
# HELP go_goroutines Number of goroutines that currently exist.
# TYPE go_goroutines gauge
go_goroutines 143
# HELP process_cpu_seconds_total Total user and system CPU time spent in seconds.
# TYPE process_cpu_seconds_total counter
process_cpu_seconds_total 812.44
# HELP promhttp_metric_handler_requests_total Total scrapes by HTTP status code.
# TYPE promhttp_metric_handler_requests_total counter
promhttp_metric_handler_requests_total{code="200"} 37
"""

_THREAD = 'account_id="2",model="us.anthropic.claude-sonnet-4-6",provider="bedrock",thread_id="cli:inv-2bb216a3",user_id="unknown"'

_TOKENS = f"""\
# HELP llm_tokens_total Total LLM tokens consumed
# TYPE llm_tokens_total counter
llm_tokens_total{{{_THREAD},node="diagnosis",token_type="input"}} 412331
llm_tokens_total{{{_THREAD},node="diagnosis",token_type="output"}} 8842
# HELP llm_calls_total Total LLM invocations
# TYPE llm_calls_total counter
llm_calls_total{{{_THREAD},node="diagnosis"}} 11
# HELP llm_call_duration_seconds LLM call latency distribution
# TYPE llm_call_duration_seconds histogram
llm_call_duration_seconds_bucket{{{_THREAD},le="30"}} 9
llm_call_duration_seconds_bucket{{{_THREAD},le="+Inf"}} 11
llm_call_duration_seconds_count{{{_THREAD}}} 11
"""


def _breaker(trips):
    """The CUSTOM_MCP breaker counter, which brain-go serves at 0 before a trip.

    It is registered as a counter with no labels and its child is created
    during wiring, so both services expose the series from their first scrape
    whether or not a breaker ever opened. The guard on the write depends on
    that, so tests need to be able to serve it at 0.
    """
    return (
        "# HELP custom_mcp_breaker_trips_total CUSTOM_MCP circuit breakers opened\n"
        "# TYPE custom_mcp_breaker_trips_total counter\n"
        f"custom_mcp_breaker_trips_total {trips}\n"
    )


_RCI = """\
# HELP rci_signal_agent_all_errored_total Signal agents whose tools all failed
# TYPE rci_signal_agent_all_errored_total counter
rci_signal_agent_all_errored_total{signal="logs"} 3
"""


def _stub_bin(bindir):
    """Stand in for curl and docker so both endpoints answer without a stack.

    curl picks its reply from STUB_CHAT/STUB_TWORKER, each either a file to
    serve or unset for an endpoint that refuses the connection. docker answers
    the two calls the script makes while resolving the tworker container's
    bridge address.
    """
    bindir = Path(bindir)
    bindir.mkdir(parents=True, exist_ok=True)

    curl = bindir / "curl"
    curl.write_text(
        "#!/bin/bash\n"
        # The script passes the URL last; the flags before it are ignored here.
        'url="${@: -1}"\n'
        'case "$url" in\n'
        '  *:9091/*) f="${STUB_CHAT:-}" ;;\n'
        '  *:9092/*) f="${STUB_TWORKER:-}" ;;\n'
        "  *) exit 3 ;;\n"
        "esac\n"
        # 7 is curl's "failed to connect", the status an unpublished port gives.
        '[ -n "$f" ] || exit 7\n'
        'cat "$f"\n'
    )

    docker = bindir / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "ps" ]; then\n'
        '  [ -n "${STUB_TWORKER_ID:-}" ] && printf \'%s\\n\' "$STUB_TWORKER_ID"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "inspect" ]; then\n'
        # A missing container leaves the script's $(docker ps) empty, so the
        # id argument arrives empty and real docker fails. Match that, or the
        # container-not-found branch is never reached.
        '  [ -n "${@: -1}" ] || exit 1\n'
        "  printf '%s \\n' \"${STUB_TWORKER_IP:-172.18.0.9}\"\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )

    for f in (curl, docker):
        f.chmod(f.stat().st_mode | stat.S_IEXEC)

    return bindir


def _run(tmp_path, *, chat=None, tworker=None, container=True, out=None):
    """Run one scrape. chat/tworker are payload strings or None for unreachable."""
    bindir = _stub_bin(tmp_path / "bin")
    env = {
        "PATH": f"{bindir}:{_SYSTEM_PATH}",
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    for name, body in (("STUB_CHAT", chat), ("STUB_TWORKER", tworker)):
        if body is not None:
            p = tmp_path / f"{name.lower()}.txt"
            p.write_text(body)
            env[name] = str(p)
    if container:
        env["STUB_TWORKER_ID"] = "c0ffee123456"

    out = out or (tmp_path / "sregym-tokens.prom")
    proc = subprocess.run(
        ["bash", str(SCRIPT), str(out)],
        capture_output=True,
        text=True,
        env=env,
    )

    return proc, out


def _samples(out):
    """The metric lines the scrape archived, without its own comment header."""
    if not out.exists():
        return []

    return [ln for ln in out.read_text().splitlines() if ln and not ln.startswith("#")]


def _names(out):
    return {ln.split("{")[0].split(" ")[0] for ln in _samples(out)}


def test_script_is_executable_and_present():
    assert SCRIPT.exists()
    assert os.access(SCRIPT, os.X_OK)
    assert shutil.which("bash")


def test_allowlisted_counters_reach_the_file(tmp_path):
    """The counters a run is measured by, including the two the old filter dropped."""
    payload = _RUNTIME + _TOKENS + _breaker(4) + _RCI
    _, out = _run(tmp_path, chat=_RUNTIME, tworker=payload)

    names = _names(out)
    assert "llm_tokens_total" in names
    assert "llm_calls_total" in names
    # Dropped by the pattern this test exists to hold in place. brain-go emits
    # it so breaker trips can be counted from a benchmark run.
    assert "custom_mcp_breaker_trips_total" in names
    assert "rci_signal_agent_all_errored_total" in names
    # The value has to survive too, not just the name: a trip count of 4 is
    # the whole point of archiving the series.
    assert "custom_mcp_breaker_trips_total 4" in out.read_text()


def test_runtime_and_process_metrics_are_dropped(tmp_path):
    """The archive stays application metrics, because it is written per attempt."""
    payload = _RUNTIME + _TOKENS + _breaker(0)
    _, out = _run(tmp_path, chat=_RUNTIME, tworker=payload)

    text = out.read_text()
    for unwanted in ("go_gc_duration_seconds", "go_goroutines", "process_cpu_seconds_total", "promhttp_"):
        assert unwanted not in text, f"{unwanted} reached the archive"

    # HELP and TYPE lines are the other half of the payload's bulk. The only
    # comments in the file should be the script's own scrape header.
    comments = [ln for ln in text.splitlines() if ln.startswith("#")]
    assert comments
    assert all(ln.startswith("# scrape:") for ln in comments), comments


def test_tworker_only_counter_reaches_the_file(tmp_path):
    """A metric served only by tworker has to survive the bridge-address path.

    The breaker increments on the activity path, which runs on tworker, and
    tworker publishes no port: the script resolves its container address
    through docker and tolerates failure. Every archived run's samples come
    from that endpoint, so this is the path that matters for a new counter.
    """
    _, out = _run(
        tmp_path,
        chat=_RUNTIME + _breaker(0),
        tworker=_RUNTIME + _TOKENS + _breaker(7),
    )

    text = out.read_text()
    assert "custom_mcp_breaker_trips_total 7" in text
    assert "tworker ok" in text


def test_missing_tworker_container_is_recorded(tmp_path):
    """No container means no worker-fleet counters, and the file says so."""
    _, out = _run(tmp_path, chat=_TOKENS, tworker=None, container=False)

    text = out.read_text()
    assert "tworker container not found" in text
    # The chat endpoint's samples still land, so a half-reachable stack is
    # not a lost scrape.
    assert "llm_tokens_total" in _names(out)


def test_unreachable_endpoints_keep_the_previous_capture(tmp_path):
    """A transient failure must not overwrite the last good scrape."""
    out = tmp_path / "sregym-tokens.prom"
    previous = "# scrape: at 2026-09-08T08:57:25Z\n" + _TOKENS
    out.write_text(previous)

    _run(tmp_path, chat=None, tworker=None, container=False, out=out)

    assert out.read_text() == previous


def test_breaker_only_scrape_keeps_the_previous_capture(tmp_path):
    """The write guard tests for token samples, not for anything allowlisted.

    brain-go serves custom_mcp_breaker_trips_total at 0 from a service's first
    scrape, so "the allowlist matched something" is true of any answered
    request. If the guard tested the allowlist, this scrape (chat answered,
    tworker unreachable, no token counters anywhere) would replace a file
    holding a whole run's tokens with one zero-valued line.
    """
    out = tmp_path / "sregym-tokens.prom"
    previous = "# scrape: at 2026-09-08T08:57:25Z\n" + _TOKENS
    out.write_text(previous)

    _run(tmp_path, chat=_RUNTIME + _breaker(0), tworker=None, out=out)

    assert out.read_text() == previous


def _section(out, name):
    """The sample lines belonging to one target, by its marker.

    A marker opens its target's samples, so a target's section runs from its
    own marker to the next marker or to end of file. `# scrape: at` is the
    file header and opens nothing.
    """
    current = None
    found = []
    for ln in out.read_text().splitlines():
        if ln.startswith("# scrape: at "):
            current = None
            continue
        if ln.startswith("# scrape: "):
            current = ln.split("# scrape: ", 1)[1].split(" ", 1)[0]
            continue
        if ln and not ln.startswith("#") and current == name:
            found.append(ln)

    return found


def test_samples_follow_the_marker_that_names_their_target(tmp_path):
    """The reading error this ordering exists to stop.

    The marker used to be written after its target's samples, so a reader
    taking it for a header credited every sample to the wrong endpoint. On the
    real archives that inverted the answer to the only question the file is
    for: the chat marker appeared to introduce tworker's tens of thousands of
    llm_ series and the tworker marker appeared to introduce nothing, which
    reads as the chat service doing all the LLM work and the worker fleet
    doing none. It is the other way round.

    Two targets serving disjoint series, so a mis-assignment cannot hide.
    """
    _, out = _run(
        tmp_path,
        chat=_RUNTIME + _breaker(0),
        tworker=_RUNTIME + _TOKENS + _RCI,
    )

    assert _section(out, "chat") == ["custom_mcp_breaker_trips_total 0"]

    tworker = _section(out, "tworker")
    assert any(ln.startswith("llm_tokens_total{") for ln in tworker)
    assert any(ln.startswith("rci_signal_agent_all_errored_total{") for ln in tworker)
    assert not any(ln.startswith("custom_mcp_") for ln in tworker)


def test_marker_reports_how_many_samples_were_kept(tmp_path):
    """ "Reachable" and "had something to report" are different facts.

    Both used to record the bare word "ok". A target that answers with an
    exposition carrying none of the allowlisted series then looks exactly like
    a healthy one, and that is the case worth seeing: it is what a role that
    never registered the instruments, or a process restarted mid-run, produces.
    """
    _, out = _run(
        tmp_path,
        chat=_RUNTIME,
        tworker=_RUNTIME + _TOKENS,
    )

    text = out.read_text()
    assert "chat ok (http://127.0.0.1:9091/metrics) 0 samples" in text
    # _TOKENS carries six allowlisted sample lines; its HELP and TYPE comments
    # are not counted because the allowlist anchors on the metric name.
    assert "tworker ok (http://172.18.0.9:9092/metrics) 6 samples" in text


def test_an_answer_with_no_body_is_not_recorded_as_ok(tmp_path):
    """Zero bytes from a port means it is not the endpoint we think it is.

    A Prometheus handler always serves at least the Go runtime and process
    collectors, so an empty 200 is never a quiet application. Recording it as
    "ok" is how a scrape pointed at the wrong container reads as a service
    that simply spent nothing.
    """
    _, out = _run(tmp_path, chat="", tworker=_RUNTIME + _TOKENS)

    text = out.read_text()
    assert "chat EMPTY BODY (http://127.0.0.1:9091/metrics)" in text
    assert "chat ok" not in text
    # The other endpoint is unaffected, so one dead target is not a lost scrape.
    assert "llm_tokens_total" in _names(out)


def test_first_scrape_without_samples_records_the_attempt(tmp_path):
    """ "We looked and found nothing" has to be distinguishable from never looking.

    An empty scrape is the failure mode that matters: it reads exactly like a
    run that spent nothing. With no previous capture to protect, the header is
    written on its own so the archive shows both endpoints were reached.
    """
    _, out = _run(tmp_path, chat=_RUNTIME, tworker=_RUNTIME)

    text = out.read_text()
    assert "chat ok" in text
    assert "tworker ok" in text
    assert _samples(out) == []
