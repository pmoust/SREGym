#!/usr/bin/env python3
"""Register the native arm's four brain-go connections for one slot.

The MCP arm's seed_connections.py registers four CUSTOM_MCP rows pointing at
SREGym's MCP endpoints, so brain-go's own connectors never run. This registers
the same four backends as native connections instead: brain-go dials
Prometheus, Loki, Jaeger and the Kubernetes API server itself, through
internal/clients/{prometheus,loki,jaeger,kubernetes}.

Authentication is the owner session cookie and the double-submit CSRF token,
for the same reason seed_connections.py uses them: a CLI API key carries no
permissions at all, so POST /api/connections has to run as the seeded owner.
The CSRF helper is imported from that module rather than copied.

What each row is, and why:

  prometheus  kind METRICS, service prometheus, bucket observation.
  loki        kind LOGS, service loki, bucket observation.
  jaeger      kind TRACES, service jaeger, bucket observation.
  kubernetes  kind OBSERVABILITY, service kubernetes, bucket observation.

The bucket is written explicitly rather than left to brain-go's
DefaultBucketForKind, so the row a run seeds does not change when that switch
does. Observation is the right bucket for all four including Kubernetes:
brain-go's registry binds the Kubernetes tool family to the metrics node
(signalSelectors' NodeMetrics unions ts.Kubernetes), and the state node binds
ts.Database alone, so a state-bucket Kubernetes row would reach no node at
all.

No `signals` key. That key scopes CUSTOM_MCP and CUSTOM_TOOL tools inside the
observation bucket, which is what the MCP arm needs because bucket alone maps
its three telemetry endpoints onto one bucket. Native families are already
partitioned by service, so the key would be read by nothing.

`allow_private_networks` is set on the three HTTP connections. brain-go's
outbound SSRF guard refuses a non-public address unless the connection opts
in, and every URL here is the host's own RFC1918 address. The Kubernetes
connection has no such key: buildKubernetes passes no options to the client,
so its guard cannot be lifted from config today. See --kubeconfig-file below.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

# Run as a file, not as a module: seed_slot.sh cd's into the brain-go checkout
# before calling this, so sys.path[0] is this directory and the sregym root is
# absent. `scripts` then resolves to an unrelated site-packages package of the
# same name that ships with gguf, and the import below dies on one of its
# optional dependencies. Put the checkout first. Same shape as
# clients/*/driver.py, which carries the same exception for the same reason.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.benchmark.seed_connections import csrf_token  # noqa: E402

# The four in-cluster backends, in the order they are seeded. Each entry is
# (connection name, brain-go ConnectionKind, brain-go DataSourceService).
#
# The names match the MCP arm's, so a results tree names the same four things
# in either arm and a reader comparing them is not also comparing labels.
NATIVE_CONNECTIONS: list[tuple[str, str, str]] = [
    ("sregym-kubernetes", "OBSERVABILITY", "kubernetes"),
    ("sregym-prometheus", "METRICS", "prometheus"),
    ("sregym-jaeger", "TRACES", "jaeger"),
    ("sregym-loki", "LOGS", "loki"),
]

# Every native connection is registered under this one bucket. See the module
# docstring for why Kubernetes is here too rather than under state.
BUCKET = ["observation"]

REQUEST_TIMEOUT_S = 60


def build_config(service: str, url: str, kubeconfig: str) -> dict[str, Any]:
    """Build one native connection's config for the given service."""
    if service == "kubernetes":
        # A kubeconfig, not api_server plus bearer_token. Three reasons, in
        # order of weight. kind already writes one per slot, so there is no
        # second credential to mint and keep in step. The kubeconfig carries
        # the cluster's CA, so TLS stays verified; the discrete shape offers
        # only ca_cert or insecure_skip_tls_verify, and the API server's
        # certificate is issued for the addresses kind chose, not for
        # whatever address this connection dials. And brain-go prefers a
        # kubeconfig when both shapes are present, so the discrete fields
        # would be dead config sitting beside it.
        #
        # The credential inside it is a read-only ServiceAccount token, not
        # kind's cluster-admin client certificate. native_arm.sh builds it.
        return {"kubeconfig": kubeconfig}

    return {"url": url, "allow_private_networks": True}


def create_connection(
    brain_url: str, session_cookie: str, csrf: str, name: str, kind: str, service: str, config: dict[str, Any]
) -> str:
    """POST one connection and return its uuid, treating an existing one as success.

    The cookies are attached by hand for the reason seed_connections.py's
    csrf_token docstring gives: CSRF-TOKEN is a Secure cookie and requests'
    jar correctly refuses to echo it back over the plain http:// URL this
    instance serves.
    """
    resp = requests.post(
        urljoin(brain_url, "/api/connections"),
        json={
            "name": name,
            "kind": kind,
            "service": service,
            "config": config,
            "bucket": list(BUCKET),
            "enabled": True,
        },
        headers={"X-CSRF-Token": csrf},
        cookies={"session": session_cookie, "CSRF-TOKEN": csrf},
        timeout=REQUEST_TIMEOUT_S,
    )

    if resp.status_code in (200, 201):
        uuid = resp.json().get("uuid", "")
        print(f"  {name}: created ({service})")

        return uuid

    # A re-run against an already-seeded stack is normal and not a failure.
    # The uuid is unknown in that case, which only costs the validate below.
    if resp.status_code == 409:
        print(f"  {name}: already exists, leaving it alone")

        return ""

    raise SystemExit(f"  {name}: POST /api/connections returned {resp.status_code}: {resp.text[:300]}")


def validate_connection(brain_url: str, session_cookie: str, csrf: str, uuid: str) -> tuple[bool, str]:
    """Run brain-go's own probe against a saved connection.

    Returns (healthy, detail). What this does and does not prove differs by
    service, because the probe is whatever health check the vendor client
    implements (internal/services/connections/probe.go):

      kubernetes  lists one namespace, so it proves the API server is
                  reachable, the TLS chain verifies and the token authorises.
      prometheus  queries the instance, so it proves the same three things.
      loki        implements no health check, so brain-go reports a successful
                  client build. It proves the config parses and nothing more.
      jaeger      the same.

    That asymmetry means an ok for Loki or Jaeger is not reachability, so the
    harness probes those two over HTTP itself.
    """
    resp = requests.post(
        urljoin(brain_url, f"/api/connections/{uuid}/validate"),
        headers={"X-CSRF-Token": csrf},
        cookies={"session": session_cookie, "CSRF-TOKEN": csrf},
        timeout=REQUEST_TIMEOUT_S,
    )

    # brain-go answers 200 {"success": true} for a healthy probe and 422
    # {"success": false, "errors": <fixed reason>} for an unhealthy one. The
    # reason is a fixed classification string carrying no host or credential
    # detail, so it is safe to print.
    if resp.status_code == 200 and resp.json().get("success") is True:
        return True, ""

    return False, f"{resp.status_code} {resp.text[:300]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--brain-url", required=True, help="brain-go api base, e.g. http://127.0.0.1:8000")
    ap.add_argument("--session", required=True, help="Seeded owner session cookie (session_{32hex}_{30hex})")
    ap.add_argument("--prometheus-url", required=True, help="Prometheus base on the host's RFC1918 address")
    ap.add_argument("--loki-url", required=True, help="Loki base on the host's RFC1918 address")
    ap.add_argument("--jaeger-url", required=True, help="Jaeger query base on the host's RFC1918 address")
    ap.add_argument("--kubeconfig-file", required=True, help="Path to the slot's read-only kubeconfig")
    args = ap.parse_args()

    for flag, url in (
        ("--prometheus-url", args.prometheus_url),
        ("--loki-url", args.loki_url),
        ("--jaeger-url", args.jaeger_url),
    ):
        if "localhost" in url or "127.0.0.1" in url:
            raise SystemExit(
                f"{flag} points at loopback. brain-go dials from inside a container, where loopback is the "
                "container's own. Use the instance's private address, the same one the MCP arm uses."
            )

    kubeconfig = Path(args.kubeconfig_file).read_text()
    if not kubeconfig.strip():
        raise SystemExit(
            f"--kubeconfig-file {args.kubeconfig_file} is empty; the Kubernetes connection has no credential"
        )

    csrf = csrf_token(args.brain_url, args.session)

    urls = {"prometheus": args.prometheus_url, "loki": args.loki_url, "jaeger": args.jaeger_url}
    unhealthy: list[str] = []

    for name, kind, service in NATIVE_CONNECTIONS:
        config = build_config(service, urls.get(service, ""), kubeconfig)
        uuid = create_connection(args.brain_url, args.session, csrf, name, kind, service, config)

        # Only Kubernetes is gated on here, and only because it is the one
        # backend that already exists at slot-seed time. The three telemetry
        # services belong to the application, which the conductor deploys per
        # problem, so probing them now would fail every slot before it ran
        # anything. Their reachability is checked in the pre-fault window
        # instead, which is the only point at which the answer matters.
        if service != "kubernetes":
            continue

        if not uuid:
            print(f"  {name}: pre-existing, skipping the health probe")

            continue

        healthy, detail = validate_connection(args.brain_url, args.session, csrf, uuid)
        if healthy:
            print(f"  {name}: healthy")
        else:
            print(f"  {name}: UNHEALTHY: {detail}")
            unhealthy.append(name)

    if unhealthy:
        raise SystemExit(
            f"{len(unhealthy)} native connection(s) failed brain-go's own probe: {', '.join(unhealthy)}. "
            "An arm B attempt whose connectors cannot reach the cluster is not arm B, so the slot stops here."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
