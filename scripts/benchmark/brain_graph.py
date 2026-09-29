#!/usr/bin/env python3
"""Read the native arm's evidence out of brain-go: entity counts and change events.

Two questions this answers, both about an attempt that has already run rather
than about the harness's intentions:

  Did the entity graph populate?  Counted by entity type, before the fault is
  injected. "The graph populated" is the whole premise of the native arm, so
  an attempt that ran against an empty graph is not an arm B attempt and must
  not be scored as one. GraphEmpty is raised for the caller to fail on.

  Was a change event produced on the object the fault mutated?  Read after the
  attempt, from the component the fault spec names. This is evidence, not a
  gate: whether a Kubernetes mutation surfaces as a change event at all is one
  of the two open questions the native arm exists to answer
  (NOFireAI/brain-go#2410), so a zero here is a result, not a failure.

Both read the api service under the slot's owner session cookie, the same
credential seed_slot.sh already mints and seed_connections.py already uses.
Neither needs the CSRF token: both are GETs, and brain-go's double-submit
check applies to state-changing methods.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

REQUEST_TIMEOUT_S = 30

# How many entities one count reads. The search route caps nothing itself, so
# this is the harness's own bound on a response it has to hold in memory. A
# healthy astronomy-shop cluster is a few hundred entities, so 5000 leaves
# room for the counts to be exact rather than a floor; `truncated` below says
# when they are not.
COUNT_LIMIT = 5000


class GraphEmpty(RuntimeError):
    """The entity graph held nothing, so this attempt is not the native arm."""


def _get(
    brain_url: str,
    session_cookie: str,
    path: str,
    params: dict[str, Any],
    timeout: float = REQUEST_TIMEOUT_S,
) -> dict[str, Any]:
    resp = requests.get(
        urljoin(brain_url, path),
        params=params,
        cookies={"session": session_cookie},
        timeout=timeout,
    )
    resp.raise_for_status()

    return resp.json()


def entity_counts(brain_url: str, session_cookie: str, limit: int = COUNT_LIMIT) -> dict[str, Any]:
    """Count the account's entities by type.

    Returns {"total": n, "by_type": {...}, "truncated": bool}. truncated is
    true when the read hit the limit, in which case every count is a floor
    rather than a total and a reader must not subtract two of them.
    """
    body = _get(brain_url, session_cookie, "/api/entities/search", {"limit": limit})
    entities = body.get("entities") or []
    by_type = Counter(str(e.get("entity_type") or "") for e in entities)

    return {
        "total": len(entities),
        "by_type": dict(sorted(by_type.items())),
        "truncated": len(entities) >= limit,
    }


def require_populated_graph(brain_url: str, session_cookie: str, limit: int = COUNT_LIMIT) -> dict[str, Any]:
    """Count entities and raise GraphEmpty when there are none.

    Called in the pre-fault window, after the baseline-sync wait. Raising here
    costs one attempt; not raising costs a run whose numbers look like arm B
    and were produced with no graph at all, which is indistinguishable from a
    real result afterwards.
    """
    counts = entity_counts(brain_url, session_cookie, limit)
    if counts["total"] == 0:
        raise GraphEmpty(
            "brain-go holds no entities for this account after the baseline-sync wait. "
            "The edge agent has not published, so this attempt would be scored as the native "
            "arm while running against an empty graph."
        )

    return counts


def change_events(
    brain_url: str,
    session_cookie: str,
    cluster: str,
    entity_name: str,
    namespace: str = "",
    since: datetime | None = None,
    timeout: float = REQUEST_TIMEOUT_S,
) -> dict[str, Any]:
    """Read the change events recorded against one entity.

    cluster is required by the route. entity_name is the component the fault
    spec names; an empty one means the problem carried no parseable label, in
    which case this returns the account's events for the cluster and leaves
    the matching to the reader.
    """
    params: dict[str, Any] = {"cluster": cluster}
    if entity_name:
        params["entity_name"] = entity_name
    if namespace:
        params["namespace"] = namespace
    if since is not None:
        params["from"] = since.astimezone(UTC).isoformat().replace("+00:00", "Z")

    body = _get(brain_url, session_cookie, "/api/change-events", params, timeout)
    events = body.get("data") or []

    return {
        "cluster": cluster,
        "entity_name": entity_name,
        "namespace": namespace,
        "count": len(events),
        "change_types": sorted({str(e.get("change_type") or "") for e in events}),
        "has_more": bool((body.get("pagination") or {}).get("has_more")),
    }


def parse_structured_root_cause(root_cause: str | None) -> tuple[str, str, str]:
    """Split Problem.build_structured_root_cause output into its parts.

    The format is ``[fault_spec] component=<c>; namespace=<n> || <description>``.
    Returns empty strings for anything the text does not carry, so a problem
    that wrote a free-form root cause still yields a usable description.
    """
    if not root_cause:
        return "", "", ""

    text = root_cause.strip()
    description = text
    meta = ""
    if text.startswith("[fault_spec]"):
        body = text[len("[fault_spec]") :].strip()
        meta, _, description = body.partition("||")
        meta, description = meta.strip(), description.strip()

    fields = {}
    for part in meta.split(";"):
        key, _, value = part.partition("=")
        if value:
            fields[key.strip()] = value.strip()

    return fields.get("component", ""), fields.get("namespace", ""), description


def parse_injected_at(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--brain-url", required=True, help="brain-go api base, e.g. http://127.0.0.1:8000")
    ap.add_argument("--session", required=True, help="Seeded owner session cookie")
    ap.add_argument("--cluster", default="", help="Cluster name the edge agent publishes under")
    ap.add_argument("--fault-spec", default="", help="Path to the attempt's fault-spec.json")
    ap.add_argument("--out", default="", help="Where to write the evidence JSON; stdout when absent")
    ap.add_argument("--require-entities", action="store_true", help="Exit 3 when the graph is empty")
    args = ap.parse_args()

    evidence: dict[str, Any] = {"cluster": args.cluster}

    try:
        evidence["entities"] = entity_counts(args.brain_url, args.session)
    except requests.RequestException as exc:
        evidence["entities"] = {"error": type(exc).__name__}

    if args.fault_spec:
        try:
            spec = json.loads(Path(args.fault_spec).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            spec = {}
            evidence["fault_spec_error"] = f"{type(exc).__name__}: {exc}"

        if spec and args.cluster:
            try:
                evidence["change_events"] = change_events(
                    args.brain_url,
                    args.session,
                    args.cluster,
                    spec.get("component", ""),
                    spec.get("namespace", ""),
                    parse_injected_at(spec.get("injected_at")),
                )
            except requests.RequestException as exc:
                evidence["change_events"] = {"error": type(exc).__name__}

    text = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text)
    else:
        sys.stdout.write(text)

    if args.require_entities and not (evidence.get("entities") or {}).get("total"):
        sys.stderr.write("brain-go holds no entities for this account; this is not the native arm\n")

        return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
