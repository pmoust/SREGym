"""The native arm's pre-fault gate, and the per-attempt evidence beside it.

Two things have to be true when the fault is injected, and neither is true by
construction:

  The three telemetry backends are reachable at the addresses brain-go's
  native connections name. The application, and therefore Prometheus, Loki and
  Jaeger, is deployed per problem, so the host port-forwards the connections
  point at come and go with it. A connection registered at slot start proves
  nothing about the problem now running.

  The entity graph holds something. The graph is what separates this arm from
  the MCP arm. An attempt whose graph is empty ran the MCP arm's information
  set through the native arm's connections, and scoring it as arm B would put
  a number on an experiment that did not happen.

Both are checked once per attempt, in the window the conductor already opens
before injecting the fault (SREGYM_BASELINE_SYNC_SECONDS). Failing here costs
one attempt. Not failing here costs the comparison.

Everything reads the environment the slot already exports, so the conductor
passes nothing: BRAIN_ADMIN_API_URL and BRAIN_SESSION come from seed_slot.sh,
the three ports and the cluster name from run_slot_env.sh, and the private
address from the same place seed_slot.sh got it.
"""

from __future__ import annotations

import os

import requests

from scripts.benchmark.arm import is_native
from scripts.benchmark.brain_graph import GraphEmpty, require_populated_graph

# How long one telemetry reachability probe may take. These are host-local
# port-forwards into the slot's own cluster, so a healthy one answers in
# milliseconds and anything near this bound means the forward is gone.
PROBE_TIMEOUT_S = 10

# The readiness path each backend answers on, and what a healthy answer is.
# Prometheus and Loki both serve /ready; Jaeger's query UI has no such route,
# so its services listing stands in. Any HTTP status counts as reachable
# except for Prometheus and Loki, which say "ready" explicitly: what is being
# proved here is that the forward carries traffic to the right process, not
# that the backend has finished warming up.
PROBES: tuple[tuple[str, str, str], ...] = (
    ("prometheus", "SREGYM_PROMETHEUS_PORT", "/-/ready"),
    ("loki", "SREGYM_LOKI_PORT", "/ready"),
    ("jaeger", "SREGYM_JAEGER_PORT", "/api/services"),
)


class NativeArmNotReady(RuntimeError):
    """The attempt cannot be the native arm, so it must not be scored as one."""


def _brain_url(env: dict[str, str]) -> str:
    return env.get("BRAIN_ADMIN_API_URL", "http://127.0.0.1:8000").rstrip("/")


def telemetry_reachable(env: dict[str, str] | None = None) -> dict[str, str]:
    """Probe the three forwarded telemetry backends.

    Returns {service: "ok"} or {service: reason}. The probe runs against
    loopback rather than the private address the connections name: the
    forwards bind 0.0.0.0, so both reach the same listener, and loopback keeps
    this check working on a box whose private address is not what the harness
    guessed.
    """
    source = os.environ if env is None else env
    out: dict[str, str] = {}

    for service, port_var, path in PROBES:
        port = source.get(port_var, "")
        if not port:
            out[service] = f"{port_var} is unset"

            continue

        try:
            resp = requests.get(f"http://127.0.0.1:{port}{path}", timeout=PROBE_TIMEOUT_S)
        except requests.RequestException as exc:
            out[service] = type(exc).__name__

            continue

        out[service] = "ok" if resp.status_code < 500 else f"HTTP {resp.status_code}"

    return out


def check(env: dict[str, str] | None = None) -> dict[str, object]:
    """Run the gate and return the evidence it gathered.

    A no-op returning {"arm": "mcp"} outside the native arm, so the conductor
    calls it unconditionally and the MCP arm's behaviour does not depend on a
    branch at the call site.

    Raises NativeArmNotReady when a telemetry backend is unreachable or the
    entity graph is empty.
    """
    source = os.environ if env is None else env
    if not is_native(source):
        return {"arm": "mcp"}

    evidence: dict[str, object] = {"arm": "native", "cluster": source.get("KIND_CLUSTER_NAME", "")}

    telemetry = telemetry_reachable(source)
    evidence["telemetry"] = telemetry

    broken = sorted(name for name, status in telemetry.items() if status != "ok")
    if broken:
        raise NativeArmNotReady(
            "native connections cannot reach "
            + ", ".join(f"{name} ({telemetry[name]})" for name in broken)
            + ". brain-go would investigate this attempt with no telemetry at all."
        )

    session = source.get("BRAIN_SESSION", "")
    if not session:
        raise NativeArmNotReady("BRAIN_SESSION is unset, so the entity graph cannot be read")

    try:
        evidence["entities"] = require_populated_graph(_brain_url(source), session)
    except GraphEmpty as exc:
        raise NativeArmNotReady(str(exc)) from exc
    except requests.RequestException as exc:
        raise NativeArmNotReady(f"could not read the entity graph: {type(exc).__name__}") from exc

    return evidence
