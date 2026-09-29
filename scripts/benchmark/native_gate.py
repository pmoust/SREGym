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

A third is checked just after the fault is injected: brain-go holds a
publish from after the fault, so the investigation does not start against the
pre-fault graph. See wait_for_fault_publish.

Everything reads the environment the slot already exports, so the conductor
passes nothing: BRAIN_ADMIN_API_URL and BRAIN_SESSION come from seed_slot.sh,
the three ports and the cluster name from run_slot_env.sh, and the private
address from the same place seed_slot.sh got it.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime

import requests

from scripts.benchmark.arm import is_native
from scripts.benchmark.brain_graph import (
    GraphEmpty,
    change_events,
    parse_structured_root_cause,
    require_populated_graph,
)

logger = logging.getLogger(__name__)

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


# --- after the fault ---------------------------------------------------------
#
# The brain_go driver starts diagnosis as soon as the conductor leaves
# inject_fault, and the edge agent's first publish after the fault lands 60s
# to 90s later (see native_arm.sh for the tick arithmetic). Without a wait the
# first minute of every investigation reads the pre-fault graph, and this arm
# is graded on whether the graph and its change events help. In production an
# investigation starts when an alert fires, minutes after the fault, so
# waiting for the graph is the closer match.
#
# The wait ends on evidence, not on time. Two things count, checked in order
# on every poll:
#
#   change_event  a change event on the fault's component with a timestamp at
#                 or after injection.
#   publish       a change event anywhere in the slot's cluster at or after
#                 injection. brain-go stamps every change event from one
#                 ingest with that ingest's receive time, so such an event
#                 exists only if a publish arrived after the fault. brain-go
#                 exposes no last-publish time, so this is the only way to see
#                 one; a publish that changed nothing is invisible here.
#
# The cap is 120s: one 60s tick plus the agent's 30s publish timeout, and 30s
# of margin for ingest.
#
# Reaching the cap does not fail the attempt. Some faults change no object the
# agent watches (chaos-mesh CPU or network stress touches no Kubernetes
# object, and chart 0.5.2's default resource list has no networkpolicies), and
# a publish that changed nothing is invisible, so an empty read at the cap
# does not show the agent is broken. install-edge already proved the agent
# publishes, and the pre-fault gate proved the graph is populated. Failing
# here would spend all three deploy attempts on such a problem and record it
# as deploy_failed. The attempt proceeds and arm-native.json says "cap", which
# counts how often the graph showed nothing after a fault.
POST_FAULT_WAIT_S = 120
POST_FAULT_POLL_S = 5


def _int_env(source, name: str, default: int) -> int:
    try:
        return int(source.get(name, "") or default)
    except ValueError as exc:
        raise NativeArmNotReady(f"{name} is not an integer") from exc


def wait_for_fault_publish(
    injected_at: float,
    root_cause: str | None,
    fallback_namespace: str | None = None,
    env: dict[str, str] | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object] | None:
    """Block until brain-go holds a publish from after the fault.

    Returns None outside the native arm without reading anything. Otherwise
    returns what ended the wait, for arm-native.json:

      {"ended_by": "change_event" | "publish" | "cap", "waited_seconds": ...,
       "lag_seconds": ..., "polls": ..., "cap_seconds": ..., ...}

    lag_seconds is from injection to the poll that saw the evidence, so it is
    an upper bound on the graph's lag, by at most one poll interval. It is
    None when the wait reached the cap, because the lag is then unknown.

    Reaching the cap returns rather than raises; see POST_FAULT_WAIT_S. Raises
    NativeArmNotReady only when BRAIN_SESSION or KIND_CLUSTER_NAME is unset,
    or a budget variable is not an integer, since then nothing can be read.
    """
    source = os.environ if env is None else env
    if not is_native(source):
        return None

    cap = _int_env(source, "SREGYM_POST_FAULT_WAIT_SECONDS", POST_FAULT_WAIT_S)
    poll = _int_env(source, "SREGYM_POST_FAULT_POLL_SECONDS", POST_FAULT_POLL_S)

    session = source.get("BRAIN_SESSION", "")
    cluster = source.get("KIND_CLUSTER_NAME", "")
    if not session:
        raise NativeArmNotReady("BRAIN_SESSION is unset, so the post-fault publish cannot be read")
    if not cluster:
        raise NativeArmNotReady("KIND_CLUSTER_NAME is unset, so the post-fault publish cannot be read")

    component, namespace, _ = parse_structured_root_cause(root_cause)
    namespace = namespace or (fallback_namespace or "")
    since = datetime.fromtimestamp(injected_at, UTC)
    base = _brain_url(source)

    evidence: dict[str, object] = {
        "cap_seconds": cap,
        "component": component,
        "namespace": namespace,
    }

    start = clock()
    polls = 0
    last_error = ""
    while True:
        polls += 1
        ended_by = ""
        try:
            # An empty component means the problem carried no parseable label;
            # the cluster-wide read below is then the only evidence.
            if component and change_events(base, session, cluster, component, namespace, since)["count"]:
                ended_by = "change_event"
            elif change_events(base, session, cluster, "", "", since)["count"]:
                ended_by = "publish"
        except requests.RequestException as exc:
            last_error = type(exc).__name__

        waited = clock() - start
        if ended_by:
            evidence.update(
                ended_by=ended_by,
                waited_seconds=round(waited, 1),
                lag_seconds=round(time.time() - injected_at, 1),
                polls=polls,
            )
            if last_error:
                evidence["last_error"] = last_error

            return evidence

        if waited >= cap:
            evidence.update(ended_by="cap", waited_seconds=round(waited, 1), lag_seconds=None, polls=polls)
            if last_error:
                evidence["last_error"] = last_error
            logger.warning("no post-fault publish seen within %ss; diagnosis proceeds: %s", cap, evidence)

            return evidence

        sleep(min(poll, max(cap - waited, 0)))
