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
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from scripts.benchmark import brain_graph, native_gate
from scripts.benchmark.arm import (
    ARM_EDGE,
    ARM_MCP,
    ARM_NATIVE,
    ARMS,
    current_arm,
    is_edge,
    is_native,
    resolve_arm,
    runs_edge_agent,
)
from scripts.benchmark.seed_connections import build_config as mcp_build_config
from scripts.benchmark.seed_native_connections import (
    BUCKET,
    DEFAULT_CONFIGMAP_VALUES,
    NATIVE_CONNECTIONS,
    build_config,
    connections_for_arm,
    resolve_configmap_values,
)

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


def test_runs_edge_agent_for_the_native_and_edge_arms():
    """Both run brain-go's own connectors and the edge agent; only the
    Kubernetes connection tells them apart, which is_edge names."""
    assert runs_edge_agent({"SREGYM_ARM": "native"})
    assert runs_edge_agent({"SREGYM_ARM": "edge"})
    assert not runs_edge_agent({"SREGYM_ARM": "mcp"})
    assert not runs_edge_agent({})


def test_is_native_is_a_deprecated_alias_for_runs_edge_agent():
    """The old name stuck around only so an import written against it does
    not break; new code and this repo's own callers use runs_edge_agent."""
    assert is_native is runs_edge_agent


def test_is_edge_only_for_the_edge_arm():
    assert is_edge({"SREGYM_ARM": "edge"})
    assert not is_edge({"SREGYM_ARM": "native"})
    assert not is_edge({"SREGYM_ARM": "mcp"})
    assert not is_edge({})


def test_arms_lists_all_three():
    assert set(ARMS) == {ARM_MCP, ARM_NATIVE, ARM_EDGE}


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


def test_the_kubernetes_connection_carries_a_kubeconfig_and_the_private_network_opt_in():
    """kind's API server is on the host's RFC1918 address too, and brain-go's
    guard on the Kubernetes client refuses it unless the connection opts in."""
    config = build_config("kubernetes", "", "apiVersion: v1\nkind: Config\n", "screened")

    assert config == {
        "kubeconfig": "apiVersion: v1\nkind: Config\n",
        "allow_private_networks": True,
        "configmap_values": "screened",
    }


def test_every_native_connection_opts_into_private_addresses():
    """All four backends are on the host's own RFC1918 address, so a service
    added to NATIVE_CONNECTIONS without the opt-in fails here."""
    for _, _, service in NATIVE_CONNECTIONS:
        config = build_config(service, "http://10.0.1.5:21000", "apiVersion: v1\n")
        assert config.get("allow_private_networks") is True, service


# --- what each arm seeds (connections_for_arm) -------------------------------


def test_connections_for_arm_drops_kubernetes_under_edge():
    services = [service for _, _, service in connections_for_arm(ARM_EDGE)]

    assert sorted(services) == ["jaeger", "loki", "prometheus"]


def test_connections_for_arm_keeps_the_full_set_under_native():
    assert connections_for_arm(ARM_NATIVE) == NATIVE_CONNECTIONS


def test_connections_for_arm_does_not_mutate_the_shared_list():
    """A caller that filters in place would quietly change NATIVE_CONNECTIONS
    for every other reader, including the native arm's own seeding."""
    before = list(NATIVE_CONNECTIONS)
    connections_for_arm(ARM_EDGE)

    assert before == NATIVE_CONNECTIONS


# --- configmap_values (brain-go#4052) ----------------------------------------


def test_configmap_values_defaults_to_screened():
    """Arm B's cluster holds no customer data, so there is no reason to seed
    the "sizes"-only default a real customer connection would start from."""
    assert DEFAULT_CONFIGMAP_VALUES == "screened"
    assert resolve_configmap_values({}) == "screened"
    assert resolve_configmap_values({"SREGYM_K8S_CONFIGMAP_VALUES": "  "}) == "screened"


def test_configmap_values_override_is_read_from_its_own_env_var():
    assert resolve_configmap_values({"SREGYM_K8S_CONFIGMAP_VALUES": "sizes"}) == "sizes"


def test_build_config_defaults_configmap_values_when_the_caller_passes_none():
    """main() always passes a resolved value; the default argument only
    matters to a caller, such as a test, that does not."""
    config = build_config("kubernetes", "", "apiVersion: v1\n")

    assert config["configmap_values"] == DEFAULT_CONFIGMAP_VALUES


def test_only_the_kubernetes_connection_carries_configmap_values():
    """The key means nothing outside internal/clients/kubernetes; writing it
    on the three telemetry connections would be dead config."""
    for service in ("prometheus", "loki", "jaeger"):
        config = build_config(service, "http://10.0.1.5:21000", "apiVersion: v1\n", "screened")
        assert "configmap_values" not in config


def test_arm_a_never_gains_configmap_values():
    """Arm A seeds through seed_connections.py's own build_config, a
    different function this change does not touch, and a CUSTOM_MCP config
    has no kubernetes-service branch for the key to reach."""
    tools = [{"name": "exec_kubectl_cmd_safely", "inputSchema": {"type": "object"}}]
    config = mcp_build_config("http://10.0.1.5:9954", "kubectl", True, "sid", tools, None)

    assert "configmap_values" not in config


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
        env={**os.environ, "SREGYM_ARM": "native"},
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
    # Every backend, the Kubernetes API server included, is on the host's
    # RFC1918 address, which brain-go's outbound guard refuses without this.
    assert all(c["config"]["allow_private_networks"] is True for c in creates)
    # Only Kubernetes is probed at seed time: the three telemetry backends
    # belong to the application, which does not exist yet.
    assert [path for path, _ in posts if path.endswith("/validate")] == ["/api/connections/u-1/validate"]

    kubernetes_config = next(c["config"] for c in creates if c["service"] == "kubernetes")
    assert kubernetes_config["configmap_values"] == "screened"
    assert all("configmap_values" not in c["config"] for c in creates if c["service"] != "kubernetes")


def test_seeding_the_native_arm_honours_a_configmap_values_override(stub_brain, tmp_path):
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
        env={**os.environ, "SREGYM_ARM": "native", "SREGYM_K8S_CONFIGMAP_VALUES": "sizes"},
    )
    assert done.returncode == 0, done.stderr

    creates = [body for path, body in posts if path == "/api/connections"]
    kubernetes_config = next(c["config"] for c in creates if c["service"] == "kubernetes")
    assert kubernetes_config["configmap_values"] == "sizes"


def test_seeding_the_edge_arm_posts_three_connections_and_skips_kubernetes(stub_brain):
    """Edge is native minus the Kubernetes connection: no kubeconfig given, no
    sregym-kubernetes row, and the script must not demand one."""
    url, posts = stub_brain

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
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "SREGYM_ARM": "edge"},
    )
    assert done.returncode == 0, done.stderr

    creates = [body for path, body in posts if path == "/api/connections"]
    assert len(creates) == 3
    assert {c["service"] for c in creates} == {"prometheus", "loki", "jaeger"}
    assert all(c["config"]["allow_private_networks"] is True for c in creates)
    assert all("configmap_values" not in c["config"] for c in creates)
    # Only Kubernetes is probed at seed time, and there is none under edge.
    assert [path for path, _ in posts if path.endswith("/validate")] == []


def test_seeding_the_native_arm_without_a_kubeconfig_file_is_a_hard_stop(stub_brain):
    """Native wants the Kubernetes connection, so a caller that forgets
    --kubeconfig-file must not seed a connection with no credential."""
    url, posts = stub_brain

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
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "SREGYM_ARM": "native"},
    )

    assert done.returncode != 0
    assert "--kubeconfig-file" in done.stderr
    assert not [body for path, body in posts if path == "/api/connections"]


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
            env={**os.environ, "SREGYM_ARM": "native"},
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
        env={**os.environ, "SREGYM_ARM": "native"},
    )

    assert done.returncode != 0
    assert "--prometheus-url" in done.stderr


# --- the cluster-side grant --------------------------------------------------

NATIVE_ARM = ROOT / "scripts" / "benchmark" / "native_arm.sh"

# Every resource brain-go's Kubernetes client reaches, by API group. Derived
# from `git grep 'clientset\.\|dynamicClient\.' -- internal/clients/kubernetes`
# on brain-go main 428144dde (#4784 merged), plus networkpolicies and
# resourcequotas from brain-go#4834 (fix/k8s-tool-diagnosis-fields), which is
# the client's whole surface: the tool adapter calls only that package's
# methods and the connection probe calls its IsConnectionValid. native_arm.sh's
# header is the authority; re-pin both copies once #4834 merges.
#
# Copied here rather than imported because the ClusterRole is YAML inside a
# shell heredoc. That makes this a second copy, which is the point: the two
# have to be edited together, and the test fails when only one is.
EXPECTED_GRANT = {
    "": [
        "configmaps",
        "events",
        "namespaces",
        "nodes",
        "persistentvolumeclaims",
        "pods",
        "resourcequotas",
        "serviceaccounts",
        "services",
    ],
    "apps": ["controllerrevisions", "daemonsets", "deployments", "replicasets", "statefulsets"],
    "batch": ["cronjobs", "jobs"],
    "autoscaling": ["horizontalpodautoscalers"],
    "scheduling.k8s.io": ["priorityclasses"],
    "rbac.authorization.k8s.io": ["clusterrolebindings", "clusterroles", "rolebindings", "roles"],
    "crd.projectcalico.org": ["bgppeers", "ipamconfigs", "ippools"],
    "networking.k8s.io": ["networkpolicies"],
}

# The kinds k8s_describe added in describe-more-kinds, and NetworkPolicy and
# ResourceQuota added in fix/k8s-tool-diagnosis-fields, are reached only by a
# Get, so their rules grant get alone.
GET_ONLY = {
    ("batch", "cronjobs"),
    ("", "serviceaccounts"),
    ("autoscaling", "horizontalpodautoscalers"),
    ("scheduling.k8s.io", "priorityclasses"),
    ("rbac.authorization.k8s.io", "roles"),
    ("rbac.authorization.k8s.io", "clusterroles"),
    ("rbac.authorization.k8s.io", "rolebindings"),
    ("rbac.authorization.k8s.io", "clusterrolebindings"),
    ("crd.projectcalico.org", "ippools"),
    ("crd.projectcalico.org", "bgppeers"),
    ("crd.projectcalico.org", "ipamconfigs"),
    ("networking.k8s.io", "networkpolicies"),
    ("", "resourcequotas"),
}


def _cluster_role_doc() -> dict:
    """The ClusterRole document from native_arm.sh's ServiceAccount heredoc,
    parsed as YAML so a rule's layout cannot hide it from the checks below."""
    src = NATIVE_ARM.read_text()
    body = src.split("kubectl apply -f - >/dev/null <<YAML || die \"could not apply the read-only ServiceAccount\"", 1)[1]
    body = body.split("\nYAML\n", 1)[0]
    roles = [d for d in yaml.safe_load_all(body) if d and d.get("kind") == "ClusterRole"]
    assert len(roles) == 1, f"expected one ClusterRole in the heredoc, found {len(roles)}"
    return roles[0]


def _cluster_role_verbs() -> dict[tuple[str, str], list[str]]:
    """The ClusterRole native_arm.sh applies, as {(apiGroup, resource): verbs}.
    A group can appear in more than one rule; each resource keeps its own
    rule's verbs."""
    rules = _cluster_role_doc()["rules"]
    assert rules, "no ClusterRole rules found in native_arm.sh"

    out: dict[tuple[str, str], list[str]] = {}
    for rule in rules:
        assert set(rule) == {"apiGroups", "resources", "verbs"}, f"unexpected rule fields: {sorted(rule)}"
        for g in rule["apiGroups"]:
            for r in rule["resources"]:
                key = (g, r)
                assert key not in out, f"{key} granted twice"
                out[key] = sorted(rule["verbs"])
    return out


def _cluster_role_rules() -> dict[str, list[str]]:
    """The ClusterRole native_arm.sh applies, as {apiGroup: [resources]}."""
    grouped: dict[str, list[str]] = {}
    for group, resource in _cluster_role_verbs():
        grouped.setdefault(group, []).append(resource)
    return {g: sorted(rs) for g, rs in grouped.items()}


def test_the_read_only_grant_is_exactly_what_brain_go_reaches():
    """Over-granting weakens the least-privilege claim; under-granting breaks a
    tool. Both are silent on this machine and expensive on a cloud run."""
    assert _cluster_role_rules() == EXPECTED_GRANT


def test_every_rule_is_read_only_and_names_no_secret():
    """No write verb, no wildcard, no Secret and no subresource anywhere in
    the role, whatever the rule's layout."""
    for (group, resource), verbs in _cluster_role_verbs().items():
        assert set(verbs) <= {"get", "list"}, (group, resource, verbs)
        assert "*" not in (group, resource)
        assert resource != "secrets"
        assert "/" not in resource, (group, resource)


def test_the_describe_only_kinds_grant_get_alone():
    """Nothing lists or watches these kinds; a broader verb is a grant nothing
    exercises."""
    verbs = _cluster_role_verbs()
    for key in GET_ONLY:
        assert verbs.get(key) == ["get"], key


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


def _go_duration_seconds(value: str) -> float:
    """Seconds in a Go duration string of h/m/s parts, e.g. "60s" or "1m30s"."""
    parts = re.findall(r"(\d+(?:\.\d+)?)(h|m|s)", value)
    assert parts and "".join(n + u for n, u in parts) == value, f"not a duration this test reads: {value!r}"

    return sum(float(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in parts)


def test_the_graph_publish_interval_is_not_below_the_agents_floor():
    """The edge agent exits at startup when its publish interval is under one
    minute. At 30s it crash-looped for a whole run, Helm's wait passed anyway,
    and every attempt failed the empty-graph gate."""
    src = NATIVE_ARM.read_text()
    default = re.search(r'^EDGE_GRAPH_INTERVAL="\$\{SREGYM_EDGE_GRAPH_INTERVAL:-([^}]*)\}"$', src, re.M)
    assert default, "EDGE_GRAPH_INTERVAL default not found in native_arm.sh"
    assert _go_duration_seconds(default.group(1)) >= 60

    # The value checked above is the one handed to the chart.
    assert '--set "config.publisher.graph.interval=$EDGE_GRAPH_INTERVAL"' in src


# --- install-edge's own check that the agent runs and publishes -------------
#
# kubectl, helm and uv are fakes on PATH; brain-go is a stub HTTP server, so
# the real brain_graph.py reads a real response. The fake uv runs this test's
# interpreter on whatever follows "python", which is how native_arm.sh calls it.

FAKE_KUBECTL = """#!/bin/sh
echo "$*" >>"$FAKE_KUBECTL_LOG"
case "$*" in
  *"create namespace"*|*"create secret"*) echo "apiVersion: v1" ;;
  *"apply -f -"*) cat >/dev/null ;;
  *"get deploy nofire-edge"*) printf nofire-edge ;;
  *"auth can-i"*) echo no ;;
  *"get pods -l"*) printf '%s\\n' "$FAKE_PODS" ;;
  *"get pods -o wide"*) echo FAKE-PODS-WIDE ;;
  *"get events"*) echo FAKE-EVENTS ;;
  *"logs deploy/nofire-edge"*) echo FAKE-AGENT-LOG ;;
esac
exit 0
"""

FAKE_UV = """#!/bin/sh
if [ -n "${FAKE_UV_FAIL:-}" ]; then echo "$FAKE_UV_FAIL" >&2; exit 2; fi
while [ "$#" -gt 0 ] && [ "$1" != python ]; do shift; done
shift
exec "$FAKE_PYTHON" "$@"
"""


# Records its arguments one per line and keeps a copy of the values file it
# was handed, which install-edge deletes when it exits.
FAKE_HELM = """#!/bin/sh
for arg in "$@"; do echo "$arg"; done >"$FAKE_HELM_ARGS"
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-f" ]; then cp "$2" "$FAKE_HELM_VALUES"; fi
  shift
done
exit 0
"""


class _Graph(BaseHTTPRequestHandler):
    """brain-go's entity search. Answers with no entities for the first
    `empty_replies` requests and one entity after that."""

    empty_replies = 0
    seen: list = []

    def log_message(self, *args):  # noqa: A002
        pass

    def do_GET(self):  # noqa: N802
        type(self).seen.append((self.path, self.headers.get("Cookie", "")))
        populated = len(type(self).seen) > type(self).empty_replies
        body = {"entities": [{"entity_type": "Pod"}] if populated else []}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


@pytest.fixture
def install_edge(tmp_path):
    """Run install-edge against fakes. Returns a runner taking the pod line
    kubectl reports and how many empty graph reads brain-go serves first."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("kubectl", FAKE_KUBECTL), ("uv", FAKE_UV), ("helm", FAKE_HELM)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)

    chart = tmp_path / "nofire-edge-0.7.0.tgz"
    chart.write_bytes(b"chart")
    kubectl_log = tmp_path / "kubectl.log"

    server = HTTPServer(("127.0.0.1", 0), _Graph)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def run(pods: str, empty_replies: int, extra_env: dict | None = None):
        _Graph.empty_replies = empty_replies
        _Graph.seen = []
        env = {
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "FAKE_PYTHON": sys.executable,
            "FAKE_KUBECTL_LOG": str(kubectl_log),
            "FAKE_PODS": pods,
            "FAKE_HELM_ARGS": str(tmp_path / "helm.args"),
            "FAKE_HELM_VALUES": str(tmp_path / "helm-values.yaml"),
            "SREGYM_EDGE_CHART": str(chart),
            "BRAIN_ADMIN_API_URL": f"http://127.0.0.1:{server.server_port}",
            "BRAIN_SESSION": "session_x",
            "SREGYM_EDGE_READY_SECONDS": "3",
            "SREGYM_EDGE_PUBLISH_SECONDS": "3",
            "SREGYM_EDGE_POLL_SECONDS": "1",
            **(extra_env or {}),
        }
        done = subprocess.run(
            [
                "bash",
                str(NATIVE_ARM),
                "install-edge",
                "sregym-0",
                "http://10.0.1.5:8000/api/edge",
                "key",
                "nofire-edge",
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=env,
            timeout=60,
        )
        return done, list(_Graph.seen)

    yield run
    server.shutdown()


def _assert_diagnostics_dumped(stderr: str) -> None:
    for marker in ("FAKE-PODS-WIDE", "FAKE-EVENTS", "FAKE-AGENT-LOG"):
        assert marker in stderr, f"{marker} missing from the slot log dump"


def test_install_edge_succeeds_once_the_agent_has_published(install_edge):
    # A generous budget, since this test returns as soon as the graph fills:
    # under a loaded machine one Python start can outlast a 3s budget.
    done, seen = install_edge(
        "nofire-edge-abc true 0", empty_replies=1, extra_env={"SREGYM_EDGE_PUBLISH_SECONDS": "30"}
    )

    assert done.returncode == 0, done.stderr
    assert "the edge agent has published" in done.stderr
    assert "FAKE-AGENT-LOG" not in done.stderr
    # Polled past the first empty read, under the slot's session.
    assert len(seen) == 2
    assert all(path.startswith("/api/entities/search") for path, _ in seen)
    assert all("session=session_x" in cookie for _, cookie in seen)


def test_install_edge_aborts_the_slot_when_nothing_is_published(install_edge):
    """The smoke-run failure: a pod Helm accepted and a graph that never
    filled. The slot stops here with the agent's logs, not three attempts
    later with none."""
    done, seen = install_edge(
        "nofire-edge-abc true 0", empty_replies=10_000, extra_env={"SREGYM_EDGE_PUBLISH_SECONDS": "8"}
    )

    assert done.returncode != 0
    assert "held no entities" in done.stderr
    assert len(seen) >= 2
    _assert_diagnostics_dumped(done.stderr)


def test_install_edge_prints_the_entity_counts_stderr_on_failure(install_edge):
    """A broken uv or Python must not read as an agent that never published."""
    done, seen = install_edge(
        "nofire-edge-abc true 0", empty_replies=0, extra_env={"FAKE_UV_FAIL": "uv: interpreter not found"}
    )

    assert done.returncode != 0
    assert "uv: interpreter not found" in done.stderr
    assert "session_x" not in done.stderr
    assert seen == []
    _assert_diagnostics_dumped(done.stderr)


def test_install_edge_aborts_at_once_when_the_agent_restarts(install_edge):
    """A startup fault exits on every start, so a restart is not waited out."""
    done, seen = install_edge("nofire-edge-abc false 2", empty_replies=0)

    assert done.returncode != 0
    assert "restarted" in done.stderr
    assert seen == []
    _assert_diagnostics_dumped(done.stderr)


def test_install_edge_aborts_when_the_pod_never_becomes_ready(install_edge):
    done, seen = install_edge("nofire-edge-abc false 0", empty_replies=0)

    assert done.returncode != 0
    assert "was not Ready within" in done.stderr
    assert seen == []
    _assert_diagnostics_dumped(done.stderr)


# --- what install-edge hands the chart ---------------------------------------

# Chart 0.7.0's config.kube.resources, in its order (values.yaml at tag
# nofire-edge-0.7.0). A values override replaces the chart's list, so
# native_arm.sh has to repeat every one of these.
CHART_070_RESOURCES = [
    "pods",
    "nodes",
    "k8services",
    "endpoints",
    "configmaps",
    "secrets",
    "persistentvolumeclaims",
    "persistentvolumes",
    "deployments",
    "replicasets",
    "statefulsets",
    "daemonsets",
    "jobs",
    "ingresses",
    "namespaces",
    "services",
]


def _installed(install_edge, tmp_path, extra_env=None):
    """Run install-edge to success and return Helm's arguments and values."""
    done, _ = install_edge(
        "nofire-edge-abc true 0",
        empty_replies=0,
        extra_env={"SREGYM_EDGE_PUBLISH_SECONDS": "30", **(extra_env or {})},
    )
    assert done.returncode == 0, done.stderr

    args = (tmp_path / "helm.args").read_text().splitlines()
    values = yaml.safe_load((tmp_path / "helm-values.yaml").read_text())
    return args, values


def test_the_edge_image_is_pinned_to_a_release_tag(install_edge, tmp_path):
    """:latest moves with every edge release, so two runs of one measurement
    could grade two different agents."""
    args, _ = _installed(install_edge, tmp_path)

    assert "image.repository=nofireai/edge" in args
    assert "image.tag=v3.7.0" in args

    versions = subprocess.run(
        ["bash", str(NATIVE_ARM), "edge-versions"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={k: v for k, v in os.environ.items() if k != "SREGYM_EDGE_IMAGE"},
    )
    assert versions.stdout.split("\t")[0] == "nofireai/edge:v3.7.0"


def test_the_edge_image_is_still_overridable(install_edge, tmp_path):
    args, _ = _installed(install_edge, tmp_path, {"SREGYM_EDGE_IMAGE": "example/edge:dev"})

    assert "image.repository=example/edge" in args
    assert "image.tag=dev" in args


# Informers edge v3.7.0 has that chart 0.7.0's list leaves out.
EXTRA_AGENT_RESOURCES = [
    "networkpolicies",
    "poddisruptionbudgets",
    "horizontalpodautoscalers",
    "resourcequotas",
    "storageclasses",
]

# The chart's own opt-in kinds, commented out of its list. Its ClusterRole
# grants all of them.
CHART_OPT_IN_RESOURCES = [
    "cronjobs",
    "priorityclasses",
    "roles",
    "clusterroles",
    "rolebindings",
    "clusterrolebindings",
    "calicoippools",
    "calicobgppeers",
    "calicoipamconfigs",
]


def test_the_informer_list_keeps_the_charts_and_adds_fourteen(install_edge, tmp_path):
    """The override replaces the chart's list, so a missing entry is an
    informer that silently stops running. Five added are agent informers the
    chart's list leaves out; nine are the chart's opt-in kinds."""
    args, values = _installed(install_edge, tmp_path)

    assert values["config"]["kube"]["resources"] == (
        CHART_070_RESOURCES + EXTRA_AGENT_RESOURCES + CHART_OPT_IN_RESOURCES
    )
    assert len(values["config"]["kube"]["resources"]) == 30
    assert not [a for a in args if a.startswith("config.kube.resources")], "--set would override the file"


def test_the_edge_service_account_is_granted_what_the_chart_lacks(install_edge, tmp_path):
    """The agent lists a resource before starting its informer and skips it
    when the list is refused, so without these two rules the HPA and
    storageclass informers never start. Chart 0.7.0 grants endpoints, the
    other agent informers and all nine opt-in kinds itself."""
    _, values = _installed(install_edge, tmp_path)

    assert values["rbac"]["extraRules"] == [
        {"apiGroups": ["autoscaling"], "resources": ["horizontalpodautoscalers"], "verbs": ["get", "list", "watch"]},
        {"apiGroups": ["storage.k8s.io"], "resources": ["storageclasses"], "verbs": ["get", "list", "watch"]},
    ]


def test_configmap_and_env_capture_stay_hashed(install_edge, tmp_path):
    """The agent under test reads this graph through brain-go, so captured
    ConfigMap and env values must not be stored in cleartext."""
    args, _ = _installed(install_edge, tmp_path)

    assert "config.configMapCapture.clearText=false" in args
    assert "config.envCapture.clearText=false" in args


def test_install_edge_removes_its_values_file(install_edge, tmp_path):
    args, _ = _installed(install_edge, tmp_path)

    assert not Path(args[args.index("-f") + 1]).exists()


def test_the_private_address_helper_refuses_a_public_one():
    """Every address this arm builds comes from here, and the arm expects the
    host's RFC1918 address. brain-go's allow_private_networks is not what
    refuses a public one: it lifts the whole non-public address check."""
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
    the native one reachable under the native and edge arms."""
    src = (ROOT / "scripts" / "benchmark" / "seed_slot.sh").read_text()

    assert src.count("seed_mcp() {") == 1
    assert src.count("seed_native() {") == 1
    assert re.search(
        r'if \[ "\$ARM" = "native" \] \|\| \[ "\$ARM" = "edge" \]; then\n  seed_native\nelse\n  seed_mcp\nfi', src
    )
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
        env={
            **{k: v for k, v in os.environ.items() if k not in ("SREGYM_ARM", "SREGYM_K8S_CONFIGMAP_VALUES")},
            **env,
        },
    )
    assert done.returncode == 0, done.stderr

    return dict(line.split("=", 1) for line in out.read_text().strip().split("\n") if "=" in line)


def test_the_mcp_arm_records_the_same_connection_lines_it_always_did(tmp_path):
    """The line an arm comparison is paired on. It has to be byte-identical to
    what every archived MCP run wrote, or the two are not the same arm."""
    fields = _manifest(tmp_path, {})

    assert fields["arm"] == "mcp"
    assert fields["kubernetes_connection"] == "mcp"
    assert fields["seeded_connection_signals"] == "kubectl:-,prometheus:metrics,jaeger:traces,loki:logs"
    assert fields["seeded_connection_buckets"] == (
        "kubectl:state,prometheus:observation,jaeger:observation,loki:observation"
    )
    # No edge agent runs under this arm, and the keys are still present so a
    # reader parses one shape.
    assert fields["edge_image"] == ""
    assert fields["edge_chart_version"] == ""
    # The MCP arm seeds no native kubernetes connection, so the key is
    # recorded empty rather than guessed at.
    assert fields["seeded_configmap_values"] == ""


def test_the_native_arm_records_its_own_connections_and_its_edge_agent(tmp_path):
    fields = _manifest(tmp_path, {"SREGYM_ARM": "native"})

    assert fields["arm"] == "native"
    assert fields["kubernetes_connection"] == "native"
    assert fields["seeded_connection_buckets"] == (
        "kubernetes:observation,prometheus:observation,jaeger:observation,loki:observation"
    )
    assert fields["seeded_connection_signals"] == "kubernetes:-,prometheus:-,jaeger:-,loki:-"
    assert fields["edge_image"].startswith("nofireai/edge:")
    assert re.match(r"^\d+\.\d+\.\d+$", fields["edge_chart_version"])
    assert fields["seeded_configmap_values"] == "kubernetes:screened"


def test_the_native_arm_records_a_configmap_values_override(tmp_path):
    """The manifest reads resolve_configmap_values() itself rather than a
    fixed "screened", so a box that overrides SREGYM_K8S_CONFIGMAP_VALUES by
    hand still gets an archived manifest that says what it actually seeded."""
    fields = _manifest(tmp_path, {"SREGYM_ARM": "native", "SREGYM_K8S_CONFIGMAP_VALUES": "sizes"})

    assert fields["seeded_configmap_values"] == "kubernetes:sizes"


def test_the_edge_arm_records_no_kubernetes_connection_but_its_own_edge_agent(tmp_path):
    """Edge is native minus the Kubernetes connection: the same edge agent and
    telemetry connections, none of the kubernetes-specific fields."""
    fields = _manifest(tmp_path, {"SREGYM_ARM": "edge"})

    assert fields["arm"] == "edge"
    assert fields["kubernetes_connection"] == "none"
    assert fields["seeded_connection_buckets"] == "prometheus:observation,jaeger:observation,loki:observation"
    assert fields["seeded_connection_signals"] == "prometheus:-,jaeger:-,loki:-"
    # The same edge agent the native arm installs, recorded the same way.
    assert fields["edge_image"].startswith("nofireai/edge:")
    assert re.match(r"^\d+\.\d+\.\d+$", fields["edge_chart_version"])
    # No kubernetes connection means no configmap_values to record, the same
    # as the MCP arm.
    assert fields["seeded_configmap_values"] == ""


def test_an_edge_box_side_configmap_override_is_not_recorded(tmp_path):
    """SREGYM_K8S_CONFIGMAP_VALUES means nothing without a kubernetes
    connection to apply it to; the edge arm's manifest must not report it."""
    fields = _manifest(tmp_path, {"SREGYM_ARM": "edge", "SREGYM_K8S_CONFIGMAP_VALUES": "sizes"})

    assert fields["seeded_configmap_values"] == ""


# --- kubernetes_tools_enabled -------------------------------------------------


def test_manifest_reads_kubernetes_tools_enabled_from_deploy_env(tmp_path):
    """Read back from the box's own deploy/.env rather than derived from the
    arm, so an override through BRAIN_ENV_EXTRA is reflected rather than
    guessed at."""
    brain_go_dir = tmp_path / "brain-go"
    (brain_go_dir / "deploy").mkdir(parents=True)
    (brain_go_dir / "deploy" / ".env").write_text("FEATURE_KUBERNETES_TOOLS_ENABLED=true\n")

    fields = _manifest(tmp_path, {"SREGYM_ARM": "native", "BRAIN_GO_DIR": str(brain_go_dir)})

    assert fields["kubernetes_tools_enabled"] == "true"


def test_manifest_defaults_kubernetes_tools_enabled_to_false_when_the_key_is_absent(tmp_path):
    """brain-go's own default when the key is absent from deploy/.env, so a
    reader always gets true or false rather than sometimes an empty field."""
    brain_go_dir = tmp_path / "brain-go"
    (brain_go_dir / "deploy").mkdir(parents=True)
    (brain_go_dir / "deploy" / ".env").write_text("LOG_LEVEL=debug\n")

    fields = _manifest(tmp_path, {"SREGYM_ARM": "mcp", "BRAIN_GO_DIR": str(brain_go_dir)})

    assert fields["kubernetes_tools_enabled"] == "false"


def test_manifest_defaults_kubernetes_tools_enabled_to_false_with_no_brain_go_checkout(tmp_path):
    """A missing BRAIN_GO_DIR or deploy/.env must not crash the manifest
    write; it reads the same as the key being absent."""
    fields = _manifest(tmp_path, {"SREGYM_ARM": "mcp", "BRAIN_GO_DIR": str(tmp_path / "does-not-exist")})

    assert fields["kubernetes_tools_enabled"] == "false"


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


def test_the_gate_runs_for_the_edge_arm_too(monkeypatch):
    """Edge shares the gate with native: neither telemetry reachability nor
    the entity graph depends on the Kubernetes connection edge lacks."""
    monkeypatch.setattr(
        native_gate, "telemetry_reachable", lambda env: {"prometheus": "ok", "loki": "ok", "jaeger": "ok"}
    )
    monkeypatch.setattr(native_gate, "require_populated_graph", lambda *a, **k: {"total": 2, "by_type": {"POD": 2}})

    evidence = native_gate.check({"SREGYM_ARM": "edge", "BRAIN_SESSION": "session_x", "KIND_CLUSTER_NAME": "sregym-0"})

    assert evidence["entities"]["total"] == 2


def test_the_gate_pins_the_sentinel_and_records_the_real_arm_under_edge(monkeypatch):
    """ "arm" is the gate-family sentinel both arms share, always "native", so
    conductor.py and main.py keep branching on one value under either arm.
    "run_arm" is the resolved SREGYM_ARM, so arm-native.json itself can tell
    an edge attempt from a native one without the run manifest."""
    monkeypatch.setattr(
        native_gate, "telemetry_reachable", lambda env: {"prometheus": "ok", "loki": "ok", "jaeger": "ok"}
    )
    monkeypatch.setattr(native_gate, "require_populated_graph", lambda *a, **k: {"total": 1, "by_type": {"POD": 1}})

    evidence = native_gate.check({"SREGYM_ARM": "edge", "BRAIN_SESSION": "session_x", "KIND_CLUSTER_NAME": "sregym-0"})

    assert evidence["arm"] == "native"
    assert evidence["run_arm"] == "edge"


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
    assert evidence["run_arm"] == "native"
    assert evidence["cluster"] == "sregym-2"
    assert evidence["entities"]["total"] == 7


# --- the post-fault wait -----------------------------------------------------

NATIVE_ENV = {"SREGYM_ARM": "native", "BRAIN_SESSION": "session_x", "KIND_CLUSTER_NAME": "sregym-0"}
ROOT_CAUSE = "[fault_spec] component=frontend; namespace=astronomy-shop || bad tag"
INJECTED_AT = 1_790_000_000.0


class _Clock:
    """A monotonic clock that only moves when the wait sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _change_events_reads(monkeypatch, answer):
    """Replace brain-go's change-event read. answer(poll, entity_name) is the
    count; every call is recorded as (entity_name, namespace, since)."""
    calls = []

    def fake(brain_url, session, cluster, entity_name, namespace="", since=None, timeout=None):
        assert session == "session_x"
        assert cluster == "sregym-0"
        assert timeout is not None, "every post-fault read carries a timeout"
        calls.append((entity_name, namespace, since))
        polls = sum(1 for c in calls if c[0] == entity_name)

        return {"count": answer(polls, entity_name)}

    monkeypatch.setattr(native_gate, "change_events", fake)

    return calls


def test_the_post_fault_wait_ends_on_a_change_event_on_the_component(monkeypatch):
    calls = _change_events_reads(monkeypatch, lambda poll, name: 1 if name == "frontend" and poll >= 3 else 0)
    _no_subprocess(monkeypatch)
    clock = _Clock()

    evidence = native_gate.wait_for_fault_publish(
        INJECTED_AT, ROOT_CAUSE, "fallback-ns", NATIVE_ENV, clock=clock, sleep=clock.sleep
    )

    assert evidence["ended_by"] == "change_event"
    assert evidence["polls"] == 3
    assert evidence["waited_seconds"] == 10.0
    assert evidence["cap_seconds"] == 120
    assert evidence["component"] == "frontend"
    # Events are read from injection on, on the component the fault names, in
    # the namespace the root cause names rather than the fallback.
    # From a few seconds after injection, so an ingest that started before the
    # fault and finished just after it does not count.
    since = native_gate.datetime.fromtimestamp(INJECTED_AT + native_gate.SINCE_MARGIN_S, native_gate.UTC)
    assert native_gate.SINCE_MARGIN_S > 0
    assert ("frontend", "astronomy-shop", since) in calls
    assert all(c[2] == since for c in calls)


def test_the_post_fault_wait_ends_on_any_publish_after_the_fault(monkeypatch):
    """brain-go stamps a change event with its ingest's receive time, so one
    anywhere in the cluster after injection means a publish landed."""
    _change_events_reads(monkeypatch, lambda poll, name: 1 if name == "" else 0)
    clock = _Clock()

    evidence = native_gate.wait_for_fault_publish(
        INJECTED_AT, ROOT_CAUSE, None, NATIVE_ENV, clock=clock, sleep=clock.sleep
    )

    assert evidence["ended_by"] == "publish"
    assert evidence["polls"] == 1
    assert clock.sleeps == []


def _no_subprocess(monkeypatch):
    """Fail if anything spawns a process, which is how diagnostics would run."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail(f"unexpected subprocess: {a}"))


def test_the_post_fault_wait_proceeds_at_the_cap(monkeypatch):
    """Some faults change no object the agent watches, so a graph that shows
    nothing after the fault is a result to count, not a broken attempt."""
    _change_events_reads(monkeypatch, lambda poll, name: 0)
    _no_subprocess(monkeypatch)
    clock = _Clock()

    evidence = native_gate.wait_for_fault_publish(
        INJECTED_AT, ROOT_CAUSE, None, NATIVE_ENV, clock=clock, sleep=clock.sleep
    )

    assert evidence["ended_by"] == "cap"
    assert evidence["waited_seconds"] == 120
    assert evidence["lag_seconds"] is None
    assert evidence["polls"] == 25
    assert "last_error" not in evidence
    assert sum(clock.sleeps) == 120


def test_the_post_fault_cap_is_overridable_and_read_errors_do_not_end_it(monkeypatch):
    def failing(*a, **k):
        raise brain_graph.requests.ConnectionError("refused")

    monkeypatch.setattr(native_gate, "change_events", failing)
    _no_subprocess(monkeypatch)
    clock = _Clock()
    env = {**NATIVE_ENV, "SREGYM_POST_FAULT_WAIT_SECONDS": "30", "SREGYM_POST_FAULT_POLL_SECONDS": "10"}

    evidence = native_gate.wait_for_fault_publish(INJECTED_AT, ROOT_CAUSE, None, env, clock=clock, sleep=clock.sleep)

    assert evidence["ended_by"] == "cap"
    assert evidence["waited_seconds"] == 30
    assert evidence["last_error"] == "ConnectionError"
    assert clock.sleeps == [10, 10, 10]


@pytest.mark.parametrize("unset", ["BRAIN_SESSION", "KIND_CLUSTER_NAME"])
def test_the_post_fault_wait_still_raises_when_it_cannot_read(monkeypatch, unset):
    monkeypatch.setattr(native_gate, "change_events", lambda *a, **k: pytest.fail("nothing to read with"))
    env = {k: v for k, v in NATIVE_ENV.items() if k != unset}

    with pytest.raises(native_gate.NativeArmNotReady, match=unset):
        native_gate.wait_for_fault_publish(INJECTED_AT, ROOT_CAUSE, None, env)


def test_the_post_fault_wait_records_the_http_status_of_a_failed_read(monkeypatch):
    """A cap reached on a 401 must read differently from a quiet graph."""

    def unauthorized(*a, **k):
        response = brain_graph.requests.Response()
        response.status_code = 401
        raise brain_graph.requests.HTTPError("401 Client Error", response=response)

    monkeypatch.setattr(native_gate, "change_events", unauthorized)
    clock = _Clock()
    env = {**NATIVE_ENV, "SREGYM_POST_FAULT_WAIT_SECONDS": "10"}

    evidence = native_gate.wait_for_fault_publish(INJECTED_AT, ROOT_CAUSE, None, env, clock=clock, sleep=clock.sleep)

    assert evidence["ended_by"] == "cap"
    assert evidence["last_error"] == "HTTPError 401"


@pytest.mark.parametrize("name", ["SREGYM_POST_FAULT_WAIT_SECONDS", "SREGYM_POST_FAULT_POLL_SECONDS"])
def test_a_negative_post_fault_budget_raises_before_the_wait(monkeypatch, name):
    monkeypatch.setattr(native_gate, "change_events", lambda *a, **k: pytest.fail("read before validating"))
    clock = _Clock()

    with pytest.raises(native_gate.NativeArmNotReady, match=f"{name} is negative"):
        native_gate.wait_for_fault_publish(
            INJECTED_AT, ROOT_CAUSE, None, {**NATIVE_ENV, name: "-1"}, clock=clock, sleep=clock.sleep
        )

    assert clock.sleeps == []


def test_a_zero_post_fault_poll_interval_is_floored_at_one_second(monkeypatch):
    _change_events_reads(monkeypatch, lambda poll, name: 0)
    clock = _Clock()
    env = {**NATIVE_ENV, "SREGYM_POST_FAULT_WAIT_SECONDS": "3", "SREGYM_POST_FAULT_POLL_SECONDS": "0"}

    native_gate.wait_for_fault_publish(INJECTED_AT, ROOT_CAUSE, None, env, clock=clock, sleep=clock.sleep)

    assert clock.sleeps == [1, 1, 1]


def test_the_post_fault_request_timeout_shrinks_with_the_budget(monkeypatch):
    """A hung brain-go spends each request's whole timeout. Capping it at the
    budget left keeps the wait near the cap instead of 60s per poll past it."""
    clock = _Clock()
    timeouts = []

    def slow(*a, timeout=None, **k):
        timeouts.append(timeout)
        clock.now += 25

        return {"count": 0}

    monkeypatch.setattr(native_gate, "change_events", slow)

    evidence = native_gate.wait_for_fault_publish(
        INJECTED_AT, ROOT_CAUSE, None, NATIVE_ENV, clock=clock, sleep=clock.sleep
    )

    # Remaining budget at each read: 120, 95, 65, 40, 10, then spent.
    assert timeouts == [30, 30, 30, 30, 10, native_gate.MIN_REQUEST_TIMEOUT_S]
    assert evidence["ended_by"] == "cap"


def test_change_events_passes_its_timeout_to_the_request(monkeypatch):
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [], "pagination": {}}

    def fake_get(url, params=None, cookies=None, timeout=None):
        seen["timeout"] = timeout

        return _Resp()

    monkeypatch.setattr(brain_graph.requests, "get", fake_get)

    brain_graph.change_events("http://brain", "session_x", "sregym-0", "frontend", timeout=7.5)
    assert seen["timeout"] == 7.5

    brain_graph.change_events("http://brain", "session_x", "sregym-0", "frontend")
    assert seen["timeout"] == brain_graph.REQUEST_TIMEOUT_S


def test_the_post_fault_wait_is_a_no_op_outside_the_native_arm(monkeypatch):
    monkeypatch.setattr(native_gate, "change_events", lambda *a, **k: pytest.fail("the MCP arm must not read"))
    clock = _Clock()

    for env in ({"SREGYM_ARM": "mcp"}, {}):
        assert (
            native_gate.wait_for_fault_publish(INJECTED_AT, ROOT_CAUSE, None, env, clock=clock, sleep=clock.sleep)
            is None
        )

    assert clock.sleeps == []


def test_the_post_fault_wait_runs_for_the_edge_arm_too(monkeypatch):
    """The wait exists to keep the pre-fault graph out of the investigation;
    that applies to edge's graph exactly as it does to native's."""
    calls = _change_events_reads(monkeypatch, lambda poll, name: 1 if name == "" else 0)
    clock = _Clock()
    env = {**NATIVE_ENV, "SREGYM_ARM": "edge"}

    evidence = native_gate.wait_for_fault_publish(INJECTED_AT, ROOT_CAUSE, None, env, clock=clock, sleep=clock.sleep)

    assert evidence["ended_by"] == "publish"
    assert calls


class _InjectedProblem:
    namespace = "astronomy-shop"
    root_cause = ROOT_CAUSE

    def inject_fault(self):
        self.injected = True


def _conductor_for_injection(monkeypatch, gate_evidence):
    from sregym.conductor.conductor import Conductor

    monkeypatch.setenv("SREGYM_BASELINE_SYNC_SECONDS", "0")
    monkeypatch.setattr(native_gate, "check", lambda: dict(gate_evidence))
    stub = Conductor.__new__(Conductor)
    stub.problem = _InjectedProblem()
    stub.problem_id = "incorrect_image"
    stub.logger = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    stub.fault_injected = False
    stub.fault_injected_at = None
    stub.fault_spec = None

    return Conductor, stub


def test_the_conductor_skips_the_post_fault_wait_under_the_mcp_arm(monkeypatch):
    conductor, stub = _conductor_for_injection(monkeypatch, {"arm": "mcp"})
    monkeypatch.setattr(native_gate, "wait_for_fault_publish", lambda *a, **k: pytest.fail("the MCP arm must not wait"))

    conductor._inject_fault(stub)

    assert stub.fault_injected
    assert stub.native_arm_evidence == {"arm": "mcp"}


def test_the_conductor_records_the_post_fault_wait_under_the_native_arm(monkeypatch):
    conductor, stub = _conductor_for_injection(monkeypatch, {"arm": "native", "cluster": "sregym-0"})
    seen = []

    def fake_wait(injected_at, root_cause, namespace):
        seen.append((injected_at, root_cause, namespace))
        return {"ended_by": "change_event", "waited_seconds": 42.0}

    monkeypatch.setattr(native_gate, "wait_for_fault_publish", fake_wait)

    conductor._inject_fault(stub)

    assert seen == [(stub.fault_injected_at, ROOT_CAUSE, "astronomy-shop")]
    assert stub.native_arm_evidence["post_fault_wait"] == {"ended_by": "change_event", "waited_seconds": 42.0}


def test_the_conductor_log_line_names_the_real_arm(monkeypatch):
    """The "arm" key conductor.py branches on is always "native", for native
    and edge alike, so the log line has to read run_arm instead or it would
    always say "Native arm ready" on an edge attempt."""
    conductor, stub = _conductor_for_injection(monkeypatch, {"arm": "native", "run_arm": "edge", "cluster": "sregym-0"})
    monkeypatch.setattr(native_gate, "wait_for_fault_publish", lambda *a, **k: None)
    logged = []
    stub.logger = types.SimpleNamespace(info=lambda msg: logged.append(msg), warning=lambda *a, **k: None)

    conductor._inject_fault(stub)

    assert any(msg.startswith("[ENV] Edge arm ready:") for msg in logged), logged


def test_the_conductor_records_the_post_fault_wait_under_the_edge_arm(monkeypatch):
    """Edge gets the same post-fault wait as native: the gate-family sentinel
    ("arm") is what the conductor branches on, and it is "native" for edge
    too. run_arm is what says this attempt is actually edge."""
    conductor, stub = _conductor_for_injection(monkeypatch, {"arm": "native", "run_arm": "edge", "cluster": "sregym-0"})
    seen = []

    def fake_wait(injected_at, root_cause, namespace):
        seen.append((injected_at, root_cause, namespace))
        return {"ended_by": "change_event", "waited_seconds": 17.0}

    monkeypatch.setattr(native_gate, "wait_for_fault_publish", fake_wait)

    conductor._inject_fault(stub)

    assert seen == [(stub.fault_injected_at, ROOT_CAUSE, "astronomy-shop")]
    assert stub.native_arm_evidence["post_fault_wait"] == {"ended_by": "change_event", "waited_seconds": 17.0}


def test_the_conductor_proceeds_when_the_post_fault_wait_reaches_its_cap(monkeypatch):
    """The real wait, with a zero cap and a graph that never changes."""
    conductor, stub = _conductor_for_injection(monkeypatch, {"arm": "native"})
    for key, value in {**NATIVE_ENV, "SREGYM_POST_FAULT_WAIT_SECONDS": "0"}.items():
        monkeypatch.setenv(key, value)
    _change_events_reads(monkeypatch, lambda poll, name: 0)
    _no_subprocess(monkeypatch)

    conductor._inject_fault(stub)

    assert stub.fault_injected
    assert stub.native_arm_evidence["post_fault_wait"]["ended_by"] == "cap"


def test_the_conductor_fails_the_attempt_when_the_post_fault_wait_does(monkeypatch):
    """A precondition failure is raised out of start_problem, which main.py's
    deploy loop retries, the same path the empty-graph gate takes."""
    conductor, stub = _conductor_for_injection(monkeypatch, {"arm": "native"})

    def fake_wait(*a, **k):
        raise native_gate.NativeArmNotReady("no publish")

    monkeypatch.setattr(native_gate, "wait_for_fault_publish", fake_wait)

    with pytest.raises(native_gate.NativeArmNotReady, match="no publish"):
        conductor._inject_fault(stub)


def test_arm_native_json_carries_the_post_fault_wait(tmp_path, monkeypatch):
    import main

    monkeypatch.delenv("BRAIN_SESSION", raising=False)
    wait = {"ended_by": "publish", "waited_seconds": 61.0, "lag_seconds": 63.2, "polls": 13, "cap_seconds": 120}
    conductor = types.SimpleNamespace(native_arm_evidence={"arm": "native", "post_fault_wait": wait})

    main._write_native_arm_evidence(tmp_path, conductor)

    assert json.loads((tmp_path / "arm-native.json").read_text())["post_fault_wait"] == wait


def test_arm_native_json_carries_run_arm_under_edge(tmp_path, monkeypatch):
    """main.py copies whatever native_gate.check() returned verbatim, so
    run_arm reaches the file with no code change here: this pins that it
    does, and that the file still says edge rather than native."""
    import main

    monkeypatch.delenv("BRAIN_SESSION", raising=False)
    conductor = types.SimpleNamespace(native_arm_evidence={"arm": "native", "run_arm": "edge"})

    main._write_native_arm_evidence(tmp_path, conductor)

    written = json.loads((tmp_path / "arm-native.json").read_text())
    assert written["arm"] == "native"
    assert written["run_arm"] == "edge"


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


def test_the_edge_image_is_not_preloaded_into_the_node():
    """kind runs `ctr images import --all-platforms` for both of its load
    paths, and the edge image is a manifest list. docker fetches one
    platform's blobs, `docker save` keeps the whole list, and the import then
    asks the node for a digest nobody downloaded. Two runs died that way,
    run-20260925-0933 on the docker store and run-20260928-0703 on the
    archive.

    Each node pulls the image itself now, so nothing here loads it and the
    chart's own imagePullPolicy: Always is left in place. The MCP image keeps
    its load: it is built on the box, so every blob it references is present.
    """
    shard = (ROOT / "scripts" / "benchmark" / "run_shard.sh").read_text()
    native = (ROOT / "scripts" / "benchmark" / "native_arm.sh").read_text()

    # Comment lines dropped before matching: the comment explaining this
    # names both commands it forbids, and matching the whole file would read
    # the explanation as the thing it warns about.
    code = [line for line in shard.splitlines() if not line.strip().startswith("#")]
    loads = [line.strip() for line in code if "kind load" in line and "if !" in line]

    assert len(loads) == 1, f"only the MCP image is loaded: {loads}"
    assert "SREGYM_MCP_IMAGE" in loads[0], f"the one load must be the MCP image: {loads[0]}"
    assert not [line for line in code if "docker save" in line], "saving it was the second failed attempt"
    assert not [line for line in native.splitlines() if "pullPolicy" in line and not line.strip().startswith("#")], (
        "the chart's own Always policy is what pulls it"
    )
