#!/usr/bin/env python3
"""Register the native and edge arms' brain-go connections for one slot.

The MCP arm's seed_connections.py registers four CUSTOM_MCP rows pointing at
SREGym's MCP endpoints, so brain-go's own connectors never run. This registers
the same backends as native connections instead: brain-go dials Prometheus,
Loki, Jaeger and, for the native arm only, the Kubernetes API server itself,
through internal/clients/{prometheus,loki,jaeger,kubernetes}. The edge arm is
native minus that last one: the same three telemetry connections, no
kubeconfig, no Kubernetes connection row at all.

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

`allow_private_networks` is set on all four connections. brain-go's outbound
SSRF guard refuses a non-public address unless the connection opts in, and
every address here is the host's own RFC1918 address. That includes the
Kubernetes API server kind exposes, which the kubeconfig names. buildKubernetes
reads the flag and passes it to the client, whose guard sits on the REST config
the clientset is built from, so it also covers the dial brain-go makes when it
validates the connection. Without the flag that validation is refused.

The kubernetes connection also sets `configmap_values` (brain-go#4052),
default "screened". Screened is what lets k8s_describe show a ConfigMap's
actual data, secrets masked, instead of only key names and byte sizes;
measuring arm B's native kubernetes connector without it would never exercise
that code path. The three telemetry connections do not carry the key; it
means nothing outside internal/clients/kubernetes.

SREGYM_K8S_CONFIGMAP_VALUES overrides the default (resolve_configmap_values),
but only in the environment of the process that runs this script on the
instance: launch.sh and terraform/templates/bootstrap.sh.tftpl do not forward
it, the way neither forwards SREGYM_BASELINE_SYNC_SECONDS. There is no tfvars
field or launch.sh flag for it. Changing it means exporting it by hand on the
box before seed_slot.sh runs, for one slot of one run.
"""

from __future__ import annotations

import argparse
import os
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

from scripts.benchmark.arm import ARM_EDGE, current_arm  # noqa: E402
from scripts.benchmark.seed_connections import csrf_token  # noqa: E402

# The four in-cluster backends, in the order they are seeded. Each entry is
# (connection name, brain-go ConnectionKind, brain-go DataSourceService).
#
# The names match the MCP arm's, so a results tree names the same four things
# in either arm and a reader comparing them is not also comparing labels.
#
# This is the native arm's full set. The edge arm seeds all but the first row:
# see connections_for_arm.
NATIVE_CONNECTIONS: list[tuple[str, str, str]] = [
    ("sregym-kubernetes", "OBSERVABILITY", "kubernetes"),
    ("sregym-prometheus", "METRICS", "prometheus"),
    ("sregym-jaeger", "TRACES", "jaeger"),
    ("sregym-loki", "LOGS", "loki"),
]


def connections_for_arm(arm: str) -> list[tuple[str, str, str]]:
    """Which of NATIVE_CONNECTIONS this arm actually seeds.

    Edge drops the Kubernetes row: brain-go holds no Kubernetes API
    connection under that arm, by design, so seeding one unhealthy (no
    kubeconfig exists to seed it with) would be worse than not seeding it at
    all. Every other arm that reaches this function seeds the full set.
    """
    if arm == ARM_EDGE:
        return [c for c in NATIVE_CONNECTIONS if c[2] != "kubernetes"]

    return list(NATIVE_CONNECTIONS)


# Every native connection is registered under this one bucket. See the module
# docstring for why Kubernetes is here too rather than under state.
BUCKET = ["observation"]

REQUEST_TIMEOUT_S = 60

# The env var that overrides the kubernetes connection's "configmap_values"
# setting (brain-go#4052, internal/clients/kubernetes/configmap.go). Default
# is "screened": arm B measures brain-go's own native kubernetes connector,
# and the benchmark's cluster holds no customer data, so there is no reason
# to run it in the "sizes"-only mode a real customer connection would default
# to. "Screened" is what lets k8s_describe answer a question about a
# ConfigMap's actual content, with secrets masked, instead of only key names
# and byte sizes.
#
# Read only from this process's own environment. Nothing forwards it from a
# launch: launch.sh does not pass it as a terraform var and
# terraform/templates/bootstrap.sh.tftpl does not export it into the
# instance's bootstrap, so "overridable" means set by hand in the shell that
# runs seed_slot.sh on the box, not from a scenario's tfvars.
CONFIGMAP_VALUES_ENV = "SREGYM_K8S_CONFIGMAP_VALUES"
DEFAULT_CONFIGMAP_VALUES = "screened"


def resolve_configmap_values(env: dict[str, str] | None = None) -> str:
    """The "configmap_values" value to seed, read from CONFIGMAP_VALUES_ENV.

    Defaults to DEFAULT_CONFIGMAP_VALUES when the variable is unset or empty.
    Not validated here. A brain-go carrying #4052 rejects a value that is
    neither "screened" nor "sizes" (ErrInvalidConfigMapValues), but only when
    it actually builds the client: POST /api/connections stores the config
    as given, so a typo here passes seeding and surfaces later, at
    POST /api/connections/{uuid}/validate, as an unhealthy kubernetes
    connection. A brain-go predating #4052 does not check the value at all,
    at either call, and accepts a typo silently.
    """
    source = os.environ if env is None else env

    return source.get(CONFIGMAP_VALUES_ENV, "").strip() or DEFAULT_CONFIGMAP_VALUES


def build_config(
    service: str, url: str, kubeconfig: str, configmap_values: str = DEFAULT_CONFIGMAP_VALUES
) -> dict[str, Any]:
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
        #
        # configmap_values is read by brain-go's internal/clients/kubernetes
        # New() only, not by buildKubernetes (internal/clients/factory/
        # builders.go), which reads allow_private_networks and stops there,
        # translating it into the WithAllowPrivateDial() option before
        # calling New(). An older brain-go without that key support ignores
        # it: New() just forwards cfg to its own buildRESTConfig, which reads
        # a fixed set of named keys off the config map (kubeconfig,
        # api_server/host/url, bearer_token/token, ca_cert,
        # insecure_skip_tls_verify) and never rejects an unrecognised one, so
        # this key sits unread beside kubeconfig on a brain-go predating
        # #4052.
        return {
            "kubeconfig": kubeconfig,
            "allow_private_networks": True,
            "configmap_values": configmap_values,
        }

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
    ap.add_argument(
        "--kubeconfig-file",
        default=None,
        help="Path to the slot's read-only kubeconfig. Required for the native arm's Kubernetes "
        "connection; the edge arm seeds no such connection and takes none.",
    )
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

    # Read from this process's own environment rather than a CLI flag, the
    # same source seed_slot.sh and run_shard.sh already resolved SREGYM_ARM
    # from, so the two cannot disagree about which connections this slot
    # seeds.
    arm = current_arm()
    connections = connections_for_arm(arm)
    wants_kubernetes = any(service == "kubernetes" for _, _, service in connections)

    kubeconfig = ""
    if wants_kubernetes:
        if not args.kubeconfig_file:
            raise SystemExit(f"arm {arm!r} needs --kubeconfig-file for its Kubernetes connection")

        kubeconfig = Path(args.kubeconfig_file).read_text()
        if not kubeconfig.strip():
            raise SystemExit(
                f"--kubeconfig-file {args.kubeconfig_file} is empty; the Kubernetes connection has no credential"
            )

    csrf = csrf_token(args.brain_url, args.session)

    configmap_values = resolve_configmap_values() if wants_kubernetes else ""

    urls = {"prometheus": args.prometheus_url, "loki": args.loki_url, "jaeger": args.jaeger_url}
    unhealthy: list[str] = []

    for name, kind, service in connections:
        config = build_config(service, urls.get(service, ""), kubeconfig, configmap_values)
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
            # create_connection already logged the 409 and returned no uuid:
            # the pre-existing row keeps whatever configmap_values it was
            # created with, which this resolved value was not applied to.
            print(
                f"  {name}: pre-existing, skipping the health probe; configmap_values={configmap_values} not applied to it"
            )

            continue

        print(f"  {name}: configmap_values={configmap_values}")

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
