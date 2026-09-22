"""The MCP port-forward probe accepts any HTTP answer and still rejects silence.

run-20260914-0912 produced zero results: all 12 slots logged "MCP port-forward
never served" and self-terminated without running a single problem. The probe
added in #94 required HTTP 200 from /healthz, a route that entered the source
in that same commit. The pod did not run the source: `kubectl apply -k` applied
a kustomization whose `images:` transform rewrote `sregym` to
ghcr.io/sregym/sregym-mcp, pulled fresh from GHCR on every pod start by
imagePullPolicy: Always. That published image was built 2026-09-06, has the
five SSE mounts and no /healthz, and no Mount("/") to absorb the path, so
Starlette answered 404. The slot logs agree that the app answered rather than
hanging: 29 clean "Handling connection" lines per forward with no forwarding
errors, 29 probe cycles inside a 60s deadline whose sleep alone is 2s, i.e.
curl returned in milliseconds.

The benchmark now builds the image from the checkout and loads it into each
slot's kind node, so a benchmark pod serves /healthz and answers 200. Both
statuses stay pinned below: the deploy path applies the committed
kustomization, published image and all, whenever SREGYM_MCP_IMAGE is unset,
and neither caller of the predicate can act on the difference anyway.

These run the real shell functions against real sockets, because the bug was
in what curl's exit behaviour means, not in anything a mock of curl would
reproduce. The functions are extracted out of run_shard.sh and run as shell,
the same technique test_pf_restart_recording.py uses for record_pf_event():
run_shard.sh is sourced rather than executed, and `bash -n` cannot exercise
behaviour.
"""

import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import NamedTuple

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUN_SHARD = ROOT / "scripts" / "benchmark" / "run_shard.sh"

PROBE_FNS = ("mcp_probe_code", "mcp_probe_answered", "pf_port_accepts", "pf_forward_alive")
PROBE_CONSTANTS = ("MCP_PROBE_TIMEOUT_SECONDS", "PF_CONNECT_TIMEOUT_SECONDS")


def _probe_constants() -> str:
    """The probe timeouts, verbatim from run_shard.sh.

    Matched at column 0, which is what picks out the file-scope constants and
    not the PF_* ones run_slot assigns, indented, inside itself. A harness that
    pastes only function bodies has to carry these or curl runs with an empty
    --max-time.
    """
    lines = RUN_SHARD.read_text().splitlines()
    out = []
    for name in PROBE_CONSTANTS:
        found = [line for line in lines if line.startswith(f"{name}=")]
        assert len(found) == 1, f"{name} is not assigned exactly once at file scope: {found}"
        out.append(found[0])
    return "\n".join(out) + "\n"


def _probe_fns() -> str:
    """The probe function bodies, verbatim from run_shard.sh."""
    text = RUN_SHARD.read_text()
    out = [_probe_constants()]
    for name in PROBE_FNS:
        opener = f"{name}() {{"
        assert text.count(opener) == 1, f"{name} is not defined exactly once in run_shard.sh"
        body = text.split(opener, 1)[1].split("\n}\n", 1)[0]
        out.append(f"{opener}{body}\n}}\n")
    return "\n".join(out)


class Probe(NamedTuple):
    code: str
    answered: bool
    seconds: float


def _probe(port: int) -> Probe:
    """Run both functions against `port`; report curl's code, the verdict and
    how long it took.

    The subprocess is bounded well above mcp_probe_code's own --max-time. That
    bound is what turns "somebody deleted --max-time" into a failing test
    rather than a suite that hangs forever on the one case that exercises it.
    """
    script = _probe_fns() + (
        'code="$(mcp_probe_code)"\n'
        'printf "code=%s\\n" "$code"\n'
        'if mcp_probe_answered "$code"; then printf "answered=yes\\n"; else printf "answered=no\\n"; fi\n'
    )
    started = time.monotonic()
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "MCP_SERVER_PORT": str(port)},
        timeout=15,
    )
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, proc.stderr
    lines = dict(line.split("=", 1) for line in proc.stdout.strip().splitlines())
    return Probe(lines["code"], lines["answered"] == "yes", elapsed)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def status_server():
    """An HTTP server on 127.0.0.1 answering /healthz with a fixed status."""
    servers = []

    def start(status: int) -> int:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's own name
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server.server_address[1]

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_404_counts_as_serving(status_server):
    """The regression that cost run-20260914-0912 its entire run.

    This is not a hypothetical status. It is what ghcr.io/sregym/sregym-mcp
    returns for /healthz, and the old `= "200"` gate turned it into "MCP
    port-forward never served" on all 12 slots. A benchmark pod no longer runs
    that image, but the deploy path still applies the committed kustomization
    when SREGYM_MCP_IMAGE is unset, so the status is still reachable.
    """
    probe = _probe(status_server(404))

    assert probe.code == "404"
    assert probe.answered is True


def test_200_still_counts_as_serving(status_server):
    """What a benchmark pod sends now that it runs an image built from this
    checkout. The route exists there, so the probe must keep passing rather
    than start failing the other way."""
    probe = _probe(status_server(200))

    assert probe.code == "200"
    assert probe.answered is True


def test_nothing_listening_is_not_serving():
    """Connection refused: the forward never bound the port at all."""
    probe = _probe(_free_port())

    assert probe.code == "000"
    assert probe.answered is False


def test_a_socket_that_never_answers_is_not_serving():
    """The failure #94 exists to catch, and the one a weaker fix would lose.

    `kubectl port-forward` stays up while forwarding to a pod that never
    becomes ready, so the port accepts and then nothing comes back. A TCP-level
    probe would call this healthy; every later tool call would pay a full
    timeout against it. Only mcp_probe_code's --max-time ends this one.

    The accepted socket has to stay referenced for the whole probe. An earlier
    version of this test dropped it as soon as the accepting thread returned,
    so CPython closed it and curl got an empty reply in 0.01s (exit 56) -- the
    same 000 by a different route, and not this failure at all. It passed with
    --max-time deleted from mcp_probe_code entirely, which in production means
    curl blocking with no timeout: the gate below only re-reads its deadline
    between iterations, so the slot would hang forever instead of aborting at
    60s, and the supervisor would stop probing. A genuine hang takes the full
    --max-time and reports exit 28, which is what the duration assertion pins.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    held = []
    threading.Thread(target=lambda: held.append(listener.accept()[0]), daemon=True).start()
    try:
        probe = _probe(port)
    finally:
        for sock in held:
            sock.close()
        listener.close()

    assert probe.code == "000"
    assert probe.answered is False
    # Distinguishes a --max-time expiry from an instant empty reply, which is
    # the whole point of this case. mcp_probe_code allows 3s.
    assert probe.seconds >= 2, f"expected a --max-time expiry, got {probe.seconds:.2f}s (socket closed early?)"


def _predicate(snippet: str, port: int) -> bool:
    """Run one shell predicate from run_shard.sh against `port` and report it.

    Real sockets, same reason the probe tests above use them: what is under
    test is which curl outcomes count as a connect, and a mocked curl would
    only re-state the answer the test already assumed.
    """
    script = _probe_fns() + f'if {snippet}; then printf "yes\\n"; else printf "no\\n"; fi\n'
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "MCP_SERVER_PORT": str(port)},
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip() == "yes"


def test_a_port_nothing_listens_on_does_not_accept():
    """Connection refused, curl exit 7: nothing is bound, the forward is gone."""
    assert _predicate("pf_port_accepts", _free_port()) is False


def test_a_port_that_accepts_and_closes_does_not_accept():
    """A listener that accepts and closes at once, curl exit 52 or 56.

    This is `kubectl port-forward` with its process still up and the pod
    behind it unable to take the stream: every connect succeeds and every
    request then fails. The process check alone would call it alive, and
    the supervisor would then never restart a forward on which every tool
    call fails. It has to read as not accepting so the restart branch is
    reached.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def accept_and_close():
        conn, _ = listener.accept()
        conn.close()

    threading.Thread(target=accept_and_close, daemon=True).start()
    try:
        accepts = _predicate("pf_port_accepts", port)
    finally:
        listener.close()

    assert accepts is False


def test_a_port_that_accepts_and_never_answers_still_accepts():
    """The false positive that cost run-20260921-1250-532577fe 43 restarts.

    The MCP server's event loop was blocked by a `kubectl rollout status
    --timeout=60s`, so the handshake completed and no HTTP reply followed.
    mcp_probe_code reads that as 000, identical to a dead forward.
    pf_port_accepts is what separates them, so it must say yes here: the
    forward is up and carrying whatever brain-go has on it.

    The accepted socket is held for the whole call. Dropping it would let
    CPython close it and turn this into curl exit 52 rather than the timeout
    this case is about, and 52 reads as not accepting, so the test would fail
    for the wrong reason.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    held = []
    threading.Thread(target=lambda: held.append(listener.accept()[0]), daemon=True).start()
    try:
        accepts = _predicate("pf_port_accepts", port)
    finally:
        for sock in held:
            sock.close()
        listener.close()

    assert accepts is True


def test_a_port_that_answers_accepts(status_server):
    """A healthy forward. Any status is an answer, so the connect plainly
    happened."""
    assert _predicate("pf_port_accepts", status_server(404)) is True


def test_the_forward_is_alive_only_when_both_the_process_and_the_port_are(status_server):
    """pf_forward_alive is the supervisor's kill gate: both halves must hold.

    $$ is this bash's own pid, which is running by definition, so it stands in
    for a live `kubectl port-forward`. 2**22 is above every default
    /proc/sys/kernel/pid_max and above macOS's pid ceiling, so no process can
    own it.
    """
    port = status_server(200)

    assert _predicate('pf_forward_alive "$$"', port) is True
    assert _predicate('pf_forward_alive "4194304"', port) is False
    # Process alive, port refused: the forward died and left the process
    # behind, or never bound. Restartable.
    assert _predicate('pf_forward_alive "$$"', _free_port()) is False


def test_an_empty_pid_is_not_alive(status_server):
    """An unreadable pid file means the supervisor has lost track of what it
    supervises, so it may not claim the forward is healthy."""
    assert _predicate('pf_forward_alive ""', status_server(200)) is False


def test_both_probe_sites_use_the_shared_helper():
    """One predicate, not two that happen to agree.

    The startup gate and the supervisor both decide whether the forward is
    serving. When they were separate copies of the same curl invocation, #94's
    change had to edit both, and a later correction to either would silently
    leave the other on the old rule.
    """
    text = RUN_SHARD.read_text()
    code_lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]

    # The literal comparison this replaced must not come back at either site.
    assert '/healthz" 2>/dev/null)" = "200"' not in text
    assert '"$pf_code" != "200"' not in text

    # One definition plus the two call sites, and no third copy of the curl.
    uses = [line for line in code_lines if "mcp_probe_answered" in line]
    assert len(uses) == 3, (
        "expected the definition plus exactly two call sites, got:\n"
        + "\n".join(uses)
        + "\n\nA new caller is fine, but it has to be a deliberate choice: update this "
        "count and say in the commit why the new site wants the same predicate rather "
        "than its own. Two predicates that merely agree today are what this pins against."
    )
    healthz_lines = [line for line in code_lines if "/healthz" in line]
    assert len(healthz_lines) == 1, f"the probe URL should exist once, in mcp_probe_code: {healthz_lines}"

    # The deep check is deliberately still strict: /kubectl/sse does exist in
    # the running image, so a non-200 there is a real application fault.
    assert '"$deep_code" != "200"' in text
