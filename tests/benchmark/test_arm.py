"""SREGYM_ARM: the flag, what each arm seeds, and the gate that scores neither wrongly.

The thing worth protecting here is the MCP arm. It is the anchor every other
arm is compared against, and a change that silently altered what it seeds
would not fail anything else: the run would finish, score, and be read as
paired with runs it is not paired with. So the tests below assert the native
arm does what it claims *and* that the MCP arm's seeding is untouched, down to
the manifest line that records it.

The two seeding scripts are exercised against a stub brain-go rather than
mocked at the requests layer. What they get wrong is the shape of a request
body, and a mock that accepts any body cannot see that.
"""

import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from scripts.benchmark import brain_graph, native_gate
from scripts.benchmark.arm import ARM_MCP, ARM_NATIVE, current_arm, is_native, resolve_arm
from scripts.benchmark.seed_native_connections import BUCKET, NATIVE_CONNECTIONS, build_config

ROOT = Path(__file__).resolve().parents[2]
SEED_NATIVE = ROOT / "scripts" / "benchmark" / "seed_native_connections.py"
WRITE_MANIFEST = ROOT / "scripts" / "benchmark" / "write_run_manifest.sh"


# --- the flag ---------------------------------------------------------------


def test_an_absent_arm_is_the_mcp_arm():
    """The default has to be mcp, or an existing launch changes what it runs."""
    assert resolve_arm(None) == ARM_MCP
    assert resolve_arm("") == ARM_MCP
    assert current_arm({}) == ARM_MCP


def test_whitespace_around_a_value_is_not_a_different_arm():
    """The value travels through terraform and a shell variable to get here."""
    assert resolve_arm(" native\n") == ARM_NATIVE


def test_an_unrecognised_arm_is_a_hard_stop_not_a_fallback():
    """Falling back to mcp would run arm A under arm B's label, which is the
    one outcome an arm comparison cannot survive."""
    with pytest.raises(ValueError, match="mcp"):
        resolve_arm("mpc")

    with pytest.raises(ValueError):
        current_arm({"SREGYM_ARM": "MCP"})


def test_is_native_only_for_the_native_arm():
    assert is_native({"SREGYM_ARM": "native"})
    assert not is_native({"SREGYM_ARM": "mcp"})
    assert not is_native({})


def test_arm_py_prints_the_arm_or_exits_two():
    """run_shard.sh and seed_slot.sh both read this as a command substitution
    and terminate the run on a non-zero exit, so both halves are the contract."""
    script = ROOT / "scripts" / "benchmark" / "arm.py"

    env = {k: v for k, v in os.environ.items() if k != "SREGYM_ARM"}

    ok = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, cwd=ROOT, env=env)
    assert ok.returncode == 0
    assert ok.stdout.strip() == "mcp"

    bad = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**env, "SREGYM_ARM": "both"},
    )
    assert bad.returncode == 2
    assert "both" in bad.stderr


# --- what the native arm seeds ----------------------------------------------


def test_the_native_arm_seeds_brain_gos_own_services_and_no_custom_mcp():
    services = [service for _, _, service in NATIVE_CONNECTIONS]
    kinds = {kind for _, kind, _ in NATIVE_CONNECTIONS}

    assert sorted(services) == ["jaeger", "kubernetes", "loki", "prometheus"]
    assert "CUSTOM_MCP" not in kinds
    assert "mcp" not in services


def test_every_native_connection_is_in_the_observation_bucket():
    """brain-go binds the Kubernetes tool family to the metrics node, which
    draws from observation; the state node binds database tools alone. A
    state-bucket Kubernetes row would reach no node at all."""
    assert BUCKET == ["observation"]


def test_the_telemetry_connections_opt_into_private_addresses():
    """Every URL is the host's own RFC1918 address, and brain-go's outbound
    guard refuses a non-public address unless the connection says so."""
    for service in ("prometheus", "loki", "jaeger"):
        config = build_config(service, "http://10.0.1.5:21000", "kubeconfig-body")
        assert config == {"url": "http://10.0.1.5:21000", "allow_private_networks": True}


def test_the_kubernetes_connection_carries_a_kubeconfig_and_nothing_else():
    config = build_config("kubernetes", "", "apiVersion: v1\nkind: Config\n")

    assert set(config) == {"kubeconfig"}
    assert config["kubeconfig"].startswith("apiVersion: v1")


# --- the seeding request bodies, against a stub brain-go --------------------


class _Stub(BaseHTTPRequestHandler):
    """A brain-go shaped enough for the seeding scripts: a CSRF cookie on
    /health, a create that returns a uuid, and a validate that says ok."""

    posts: list = []

    def log_message(self, *args):  # noqa: A002 - silence the default stderr spam
        pass

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's own spelling
        self.send_response(200)
        self.send_header("Set-Cookie", "CSRF-TOKEN=tok; Path=/; Secure")
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).posts.append((self.path, body))

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

        if self.path.endswith("/validate"):
            self.wfile.write(b'{"success": true}')
        else:
            self.wfile.write(b'{"success": true, "id": 1, "uuid": "u-1"}')


@pytest.fixture
def stub_brain():
    _Stub.posts = []
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", _Stub.posts
    server.shutdown()


def test_seeding_the_native_arm_posts_four_native_connections(stub_brain, tmp_path):
    url, posts = stub_brain
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n")

    done = subprocess.run(
        [
            sys.executable,
            str(SEED_NATIVE),
            "--brain-url",
            url,
            "--session",
            "session_x",
            "--prometheus-url",
            "http://10.0.1.5:21000",
            "--loki-url",
            "http://10.0.1.5:22000",
            "--jaeger-url",
            "http://10.0.1.5:23000",
            "--kubeconfig-file",
            str(kubeconfig),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert done.returncode == 0, done.stderr

    creates = [body for path, body in posts if path == "/api/connections"]
    assert len(creates) == 4
    assert {c["service"] for c in creates} == {"kubernetes", "prometheus", "loki", "jaeger"}
    assert {c["kind"] for c in creates} == {"OBSERVABILITY", "METRICS", "LOGS", "TRACES"}
    assert all(c["bucket"] == ["observation"] for c in creates)
    # The key that scopes a CUSTOM_MCP tool inside the observation bucket. A
    # native family is partitioned by service, so writing it would be writing
    # a key nothing reads.
    assert not any("signals" in c["config"] for c in creates)
    # Only Kubernetes is probed at seed time: the three telemetry backends
    # belong to the application, which does not exist yet.
    assert [path for path, _ in posts if path.endswith("/validate")] == ["/api/connections/u-1/validate"]


def test_an_unhealthy_kubernetes_connection_stops_the_slot(tmp_path):
    """The one connection whose backend exists at seed time. A slot that
    carried on would investigate with no cluster access and score as arm B."""

    class _Unhealthy(_Stub):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self.send_response(200 if not self.path.endswith("/validate") else 422)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if self.path.endswith("/validate"):
                self.wfile.write(b'{"success": false, "errors": "connection test failed"}')
            else:
                self.wfile.write(b'{"success": true, "id": 1, "uuid": "u-1"}')

    server = HTTPServer(("127.0.0.1", 0), _Unhealthy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")

    try:
        done = subprocess.run(
            [
                sys.executable,
                str(SEED_NATIVE),
                "--brain-url",
                f"http://127.0.0.1:{server.server_port}",
                "--session",
                "session_x",
                "--prometheus-url",
                "http://10.0.1.5:21000",
                "--loki-url",
                "http://10.0.1.5:22000",
                "--jaeger-url",
                "http://10.0.1.5:23000",
                "--kubeconfig-file",
                str(kubeconfig),
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
    finally:
        server.shutdown()

    assert done.returncode != 0
    assert "sregym-kubernetes" in done.stderr + done.stdout


def test_a_loopback_telemetry_url_is_refused(tmp_path):
    """brain-go dials from inside a container, where loopback is its own."""
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")

    done = subprocess.run(
        [
            sys.executable,
            str(SEED_NATIVE),
            "--brain-url",
            "http://127.0.0.1:8000",
            "--session",
            "session_x",
            "--prometheus-url",
            "http://127.0.0.1:21000",
            "--loki-url",
            "http://10.0.1.5:22000",
            "--jaeger-url",
            "http://10.0.1.5:23000",
            "--kubeconfig-file",
            str(kubeconfig),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )

    assert done.returncode != 0
    assert "--prometheus-url" in done.stderr


# --- the cluster-side grant --------------------------------------------------

NATIVE_ARM = ROOT / "scripts" / "benchmark" / "native_arm.sh"

# Every resource brain-go's Kubernetes client reaches, by API group. Derived
# from `git grep 'clientset\.' -- internal/clients/kubernetes` on brain-go
# e49e7711, which is the client's whole surface: the tool adapter calls only
# that package's seven methods and the connection probe calls its
# IsConnectionValid.
#
# Copied here rather than imported because the ClusterRole is YAML inside a
# shell heredoc. That makes this a second copy, which is the point: the two
# have to be edited together, and the test fails when only one is.
EXPECTED_GRANT = {
    "": ["configmaps", "events", "namespaces", "nodes", "persistentvolumeclaims", "pods", "services"],
    "apps": ["controllerrevisions", "daemonsets", "deployments", "replicasets", "statefulsets"],
    "batch": ["jobs"],
}


def _cluster_role_rules() -> dict[str, list[str]]:
    """The ClusterRole native_arm.sh applies, as {apiGroup: [resources]}."""
    src = NATIVE_ARM.read_text()
    rules = re.findall(r"- apiGroups: \[([^\]]*)\]\n  resources: \[([^\]]*)\]", src)
    assert rules, "no ClusterRole rules found in native_arm.sh"

    return {
        group.strip().strip('"'): sorted(r.strip().strip('"') for r in resources.split(","))
        for group, resources in rules
    }


def test_the_read_only_grant_is_exactly_what_brain_go_reaches():
    """Over-granting weakens the least-privilege claim; under-granting breaks a
    tool. Both are silent on this machine and expensive on a cloud run."""
    assert _cluster_role_rules() == EXPECTED_GRANT


def test_the_grant_covers_batch_jobs():
    """Called out on its own because its absence is not a partial outage.

    ListWorkloads lists jobs last and returns (nil, err) on failure, discarding
    the deployments, statefulsets and daemonsets it already collected, so a
    missing batch grant makes k8s_list_workloads fail on every call in every
    namespace whether or not a Job exists.
    """
    assert "jobs" in _cluster_role_rules().get("batch", [])


def test_the_grant_reaches_nothing_chaos_mesh_owns():
    """The correctness rule rather than the least-privilege one: an agent that
    can read a PodChaos reads the injected fault verbatim."""
    src = NATIVE_ARM.read_text()
    rules_block = src.split("kind: ClusterRole", 1)[1].split("---", 1)[0]

    assert "chaos-mesh" not in rules_block
    for group in _cluster_role_rules():
        assert not group.endswith("chaos-mesh.org")


def test_installing_the_edge_agent_refuses_a_chart_it_was_not_given():
    """The chart is fetched once per box and passed down by path. A slot that
    fetched its own would race its siblings on one download path, so the
    install refuses rather than falling back to fetching."""
    done = subprocess.run(
        ["bash", str(NATIVE_ARM), "install-edge", "sregym-0", "http://10.0.1.5:8000/api/edge", "key", "nofire-edge"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={k: v for k, v in os.environ.items() if k != "SREGYM_EDGE_CHART"},
    )

    assert done.returncode != 0
    assert "SREGYM_EDGE_CHART" in done.stderr


def test_the_private_address_helper_refuses_a_public_one():
    """Every address this arm builds comes from here, and brain-go's
    allow_private_networks covers RFC1918 and ULA only."""
    ok = subprocess.run(
        ["bash", str(NATIVE_ARM), "private-ip"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "SREGYM_PRIVATE_IP": "10.0.1.5"},
    )
    assert ok.returncode == 0
    assert ok.stdout.strip() == "10.0.1.5"

    bad = subprocess.run(
        ["bash", str(NATIVE_ARM), "private-ip"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "SREGYM_PRIVATE_IP": "8.8.8.8"},
    )
    assert bad.returncode != 0
    assert "RFC1918" in bad.stderr


# --- the MCP arm is untouched ------------------------------------------------


def test_seed_slot_dispatches_on_the_arm_and_defaults_to_mcp():
    """seed_slot.sh is not runnable here (it drives docker compose), so what
    is checked is the branch itself: two functions, one call site each, and
    the native one reachable only under the native arm."""
    src = (ROOT / "scripts" / "benchmark" / "seed_slot.sh").read_text()

    assert src.count("seed_mcp() {") == 1
    assert src.count("seed_native() {") == 1
    assert re.search(r'if \[ "\$ARM" = "native" \]; then\n  seed_native\nelse\n  seed_mcp\nfi', src)
    # The MCP arm's own registration call, unchanged.
    assert "seed_connections.py" in src.split("seed_mcp() {", 1)[1].split("seed_native", 1)[0]


def _manifest(tmp_path: Path, env: dict[str, str]) -> dict[str, str]:
    """Run write_run_manifest.sh and parse what it wrote."""
    out = tmp_path / "manifest.txt"
    done = subprocess.run(
        ["bash", str(WRITE_MANIFEST), str(out)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**{k: v for k, v in os.environ.items() if k != "SREGYM_ARM"}, **env},
    )
    assert done.returncode == 0, done.stderr

    return dict(line.split("=", 1) for line in out.read_text().strip().split("\n") if "=" in line)


def test_the_mcp_arm_records_the_same_connection_lines_it_always_did(tmp_path):
    """The line an arm comparison is paired on. It has to be byte-identical to
    what every archived MCP run wrote, or the two are not the same arm."""
    fields = _manifest(tmp_path, {})

    assert fields["arm"] == "mcp"
    assert fields["seeded_connection_signals"] == "kubectl:-,prometheus:metrics,jaeger:traces,loki:logs"
    assert fields["seeded_connection_buckets"] == (
        "kubectl:state,prometheus:observation,jaeger:observation,loki:observation"
    )
    # No edge agent runs under this arm, and the keys are still present so a
    # reader parses one shape.
    assert fields["edge_image"] == ""
    assert fields["edge_chart_version"] == ""


def test_the_native_arm_records_its_own_connections_and_its_edge_agent(tmp_path):
    fields = _manifest(tmp_path, {"SREGYM_ARM": "native"})

    assert fields["arm"] == "native"
    assert fields["seeded_connection_buckets"] == (
        "kubernetes:observation,prometheus:observation,jaeger:observation,loki:observation"
    )
    assert fields["seeded_connection_signals"] == "kubernetes:-,prometheus:-,jaeger:-,loki:-"
    assert fields["edge_image"].startswith("nofireai/edge:")
    assert re.match(r"^\d+\.\d+\.\d+$", fields["edge_chart_version"])


# --- the gate ----------------------------------------------------------------


def test_an_empty_graph_raises_rather_than_returning_a_zero(monkeypatch):
    """An attempt whose graph is empty is the MCP arm's information set under
    this arm's label. Returning a count would let it be scored."""
    monkeypatch.setattr(brain_graph, "_get", lambda *a, **k: {"entities": [], "total_count": 0})

    with pytest.raises(brain_graph.GraphEmpty):
        brain_graph.require_populated_graph("http://brain", "session_x")


def test_a_populated_graph_is_counted_by_type(monkeypatch):
    monkeypatch.setattr(
        brain_graph,
        "_get",
        lambda *a, **k: {
            "entities": [
                {"entity_type": "SERVICE", "name": "cart"},
                {"entity_type": "SERVICE", "name": "checkout"},
                {"entity_type": "POD", "name": "cart-abc"},
            ]
        },
    )

    counts = brain_graph.require_populated_graph("http://brain", "session_x")

    assert counts == {"total": 3, "by_type": {"POD": 1, "SERVICE": 2}, "truncated": False}


def test_the_gate_is_a_no_op_outside_the_native_arm(monkeypatch):
    """The conductor calls it unconditionally, so the MCP arm's behaviour must
    not depend on a branch at the call site."""

    def _boom(*a, **k):
        raise AssertionError("the MCP arm must not probe anything")

    monkeypatch.setattr(native_gate, "telemetry_reachable", _boom)

    assert native_gate.check({"SREGYM_ARM": "mcp"}) == {"arm": "mcp"}
    assert native_gate.check({}) == {"arm": "mcp"}


def test_the_gate_fails_the_attempt_when_the_graph_is_empty(monkeypatch):
    monkeypatch.setattr(
        native_gate, "telemetry_reachable", lambda env: {"prometheus": "ok", "loki": "ok", "jaeger": "ok"}
    )
    monkeypatch.setattr(
        native_gate,
        "require_populated_graph",
        lambda *a, **k: (_ for _ in ()).throw(brain_graph.GraphEmpty("no entities")),
    )

    with pytest.raises(native_gate.NativeArmNotReady, match="no entities"):
        native_gate.check({"SREGYM_ARM": "native", "BRAIN_SESSION": "session_x", "KIND_CLUSTER_NAME": "sregym-0"})


def test_the_gate_fails_the_attempt_when_a_telemetry_backend_is_unreachable(monkeypatch):
    monkeypatch.setattr(
        native_gate,
        "telemetry_reachable",
        lambda env: {"prometheus": "ok", "loki": "ConnectionError", "jaeger": "ok"},
    )

    with pytest.raises(native_gate.NativeArmNotReady, match="loki"):
        native_gate.check({"SREGYM_ARM": "native", "BRAIN_SESSION": "session_x"})


def test_the_gate_returns_the_evidence_it_gathered(monkeypatch):
    monkeypatch.setattr(
        native_gate, "telemetry_reachable", lambda env: {"prometheus": "ok", "loki": "ok", "jaeger": "ok"}
    )
    monkeypatch.setattr(native_gate, "require_populated_graph", lambda *a, **k: {"total": 7, "by_type": {"POD": 7}})

    evidence = native_gate.check(
        {"SREGYM_ARM": "native", "BRAIN_SESSION": "session_x", "KIND_CLUSTER_NAME": "sregym-2"}
    )

    assert evidence["arm"] == "native"
    assert evidence["cluster"] == "sregym-2"
    assert evidence["entities"]["total"] == 7


def _terminate_body() -> str:
    """The bootstrap's terminate function, read from the template itself."""
    text = (ROOT / "terraform" / "templates" / "bootstrap.sh.tftpl").read_text()
    start = text.index("terminate() {")

    return text[start : text.index("\n}\n", start)]


def test_terminate_survives_a_call_with_only_a_reason():
    """Every failure path calls terminate with one argument.

    Under `set -u` a bare `$2` aborts the shell, so the bootstrap died at the
    top of its own cleanup: no logs synced, no shutdown, the box left running
    until the runtime cap. A default-value expansion cannot be used here
    because terraform's templatefile reads dollar-brace as interpolation, so
    the arity is read from `$#` instead.
    """
    body = _terminate_body()

    assert '"$#" -ge 2' in body, "terminate must read its arity, not expand $2 bare"
    assert 'local outcome="$2"' not in body, "a bare $2 aborts under set -u"


def test_the_native_arm_writes_no_compose_override():
    """brain-go's own dev-env.sh writes BIND_ADDR=0.0.0.0 whenever it creates
    deploy/.env, so every service is already published on every interface and
    the in-cluster edge agent reaches the api with no help from here.

    An override publishing the api a second time binds the same port twice,
    because compose merges a service's ports with the base file's rather than
    replacing them, and the api container then fails to start with "address
    already in use". That stopped the first two native smokes.
    """
    setup = (ROOT / "scripts" / "benchmark" / "setup_brain_go.sh").read_text()

    assert "cat >docker-compose.override.yml" not in setup


def test_the_chart_fetch_is_handed_the_clone_token():
    """The bootstrap sets GH_TOKEN as a plain assignment, so a script it runs
    as a child does not inherit it. native_arm.sh fetch-chart needs it because
    the chart repo is private, and without it the shard terminates before any
    slot starts. Passed on the command, the way the bootstrap already hands the
    same token to setup_brain_go.sh, rather than exported to every child.
    """
    shard = (ROOT / "scripts" / "benchmark" / "run_shard.sh").read_text()
    call = next(line for line in shard.splitlines() if "fetch-chart" in line)

    assert "GH_TOKEN=" in call, f"fetch-chart must be handed the token: {call}"
