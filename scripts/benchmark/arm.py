#!/usr/bin/env python3
"""Which benchmark arm a run is: SREGYM_ARM.

Three values, and the default reproduces every run archived so far.

    mcp     brain-go reaches the cluster through SREGym's four CUSTOM_MCP
            endpoints and nothing else. The default, and the anchor every
            other arm is compared against, so it must stay byte-identical.

    native  brain-go reaches the same four backends through its own
            connectors (kubernetes, prometheus, loki, jaeger) and the edge
            agent runs in the slot's cluster, so the entity graph and change
            events exist during the attempt.

    edge    native minus the Kubernetes API connection: the edge agent plus
            the prometheus, loki and jaeger connectors, and nothing that
            reaches the API server. No kubeconfig is built, no kind config
            binds the API server to a reachable address, and brain-go's
            k8s_* tool family is never enabled. This is NOFire's production
            posture, where brain-go holds no Kubernetes API token at all.

An unrecognised value is a hard stop, not a fallback to mcp. A typo that
silently selected the default would produce a run that reads as arm B in the
launch command and is arm A in every artifact, which is the one outcome an
arm comparison cannot survive. Same reasoning as SREGYM_STAGES in
write_tasklist.py.

Run as a script it prints the resolved arm and exits 0, or prints the reason
and exits 2, so a shell can both validate and read the value in one call:

    ARM=$(uv run python scripts/benchmark/arm.py) || terminate ...
"""

from __future__ import annotations

import os
import sys

# The MCP arm. Also the value an unset SREGYM_ARM resolves to.
ARM_MCP = "mcp"
# The native-connector and edge-agent arm.
ARM_NATIVE = "native"
# Native minus the Kubernetes API connection.
ARM_EDGE = "edge"

ARMS = (ARM_MCP, ARM_NATIVE, ARM_EDGE)


def resolve_arm(value: str | None) -> str:
    """Return the arm named by value, defaulting to mcp on an absent one.

    Raises ValueError on anything else. Whitespace is stripped because the
    value travels through terraform and a shell variable before it gets here,
    and a trailing newline is not a different arm.
    """
    spec = (value or "").strip()
    if not spec:
        return ARM_MCP

    if spec not in ARMS:
        raise ValueError(f"SREGYM_ARM must be one of {sorted(ARMS)}, got: {spec!r}")

    return spec


def current_arm(env: dict[str, str] | None = None) -> str:
    """The arm this process runs under, read from the environment."""
    source = os.environ if env is None else env

    return resolve_arm(source.get("SREGYM_ARM"))


def is_native(env: dict[str, str] | None = None) -> bool:
    """Whether this process runs its own connectors and an edge agent.

    True for both native and edge: they share every mechanism except the
    Kubernetes API connection (native_gate's telemetry probe and empty-graph
    gate, the pre-fault window, the post-fault wait, the edge agent install),
    so the gating logic for all of that reads this one flag rather than
    listing both arms at every call site. Code that cares about the one
    difference, the Kubernetes connection, reads is_edge instead.
    """
    return current_arm(env) in (ARM_NATIVE, ARM_EDGE)


def is_edge(env: dict[str, str] | None = None) -> bool:
    """Whether this process runs under the edge arm specifically.

    Unlike is_native, this is false for the native arm: it names the one
    difference between the two (no Kubernetes API connection), not the
    mechanism they share.
    """
    return current_arm(env) == ARM_EDGE


def main() -> int:
    try:
        print(current_arm())
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")

        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
