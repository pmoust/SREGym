#!/usr/bin/env python3
"""Register SREGym's four MCP endpoints as CUSTOM_MCP connections on brain-go.

Ports brain's scripts/sregym/seed_connections.py to brain-go's API. Three
differences that matter, all verified against brain-go main:

1. brain-go takes `headers` as a plain map. Python stored `auth_header` and
   `auth_value` and folded them into a dict at dial time. Copying the Python
   config verbatim silently drops the kubectl session header.

2. Nothing in brain-go writes `tools_cache`. There is no refresh endpoint; the
   builder's "run connection refresh to populate" warning describes a Python-era
   path that was not ported. An empty cache exposes zero tools, so this script
   discovers them itself.

3. `POST /connections` needs `connections:create`, and a CLI API key carries no
   permissions at all (authz.go's APIServicePermissions returns nil for
   APIKeyTypeCLI). So this authenticates with the seeded owner session cookie,
   not the API key. The API key is for /cli/chat only.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any
from urllib.parse import urljoin

import requests
from mcp import ClientSession
from mcp.client.sse import sse_client

# SREGym mounts one MCP SSE app per tool family. kubectl is tagged `state` so it
# reaches the state agent (cluster inspection and mitigation); the telemetry
# endpoints are `observation` so they reach the metrics/logs/traces agents. The
# kubectl endpoint tracks per-session rollback history via the `sregym_ssid`
# header, so a stable session id is pinned on that connection.
ENDPOINTS: list[tuple[str, str, list[str], bool]] = [
    ("sregym-kubectl", "kubectl", ["state"], True),
    ("sregym-prometheus", "prometheus", ["observation"], False),
    ("sregym-jaeger", "jaeger", ["observation"], False),
    ("sregym-loki", "loki", ["observation"], False),
]

DISCOVERY_TIMEOUT_S = 30.0


async def discover_tools(url: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    """Open a one-shot MCP session and return JSON-ready tool schemas.

    `by_alias=True` is what produces `inputSchema` in camelCase. brain-go's
    customMCPToolSchema tags that field `json:"inputSchema"` while every sibling
    key in the config is snake_case, because this half mirrors mcp.types.Tool's
    wire form rather than the config's own convention. Writing `input_schema`
    leaves the field null, brain-go substitutes an empty object schema, and the
    tool is then advertised as taking no arguments -- which surfaces as the model
    calling tools wrong rather than as a config error.
    """

    async def _list() -> list[dict[str, Any]]:
        async with (
            sse_client(url, headers=headers or None) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.list_tools()

            return [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in result.tools]

    return await asyncio.wait_for(_list(), timeout=DISCOVERY_TIMEOUT_S)


def build_config(
    mcp_base: str, path: str, needs_session: bool, session_id: str, tools: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build one CUSTOM_MCP connection config.

    `tool_allowlist` is deliberately absent. It is tri-state in brain-go: absent
    exposes every cached tool, present-but-empty exposes none. Writing `[]` here
    would silently expose nothing.

    `transport` is omitted too: empty defaults to SSE, which is what these
    endpoints speak.

    `allow_private_endpoint` lifts brain-go's outbound SSRF guard for RFC1918 and
    ULA only. Loopback stays blocked with no flag that lifts it, so mcp_base must
    be the instance's private address rather than localhost. Note the allowance
    is honoured only on the in-process CUSTOM_MCP path; the Temporal MCP activity
    path ignores it.
    """
    config: dict[str, Any] = {
        "mcp_server_url": f"{mcp_base.rstrip('/')}/{path}/sse",
        "allow_private_endpoint": True,
        "tools_cache": tools,
    }

    if needs_session:
        config["headers"] = {"sregym_ssid": session_id}

    return config


def create_connection(
    brain_url: str, session_cookie: str, name: str, config: dict[str, Any], buckets: list[str]
) -> None:
    """POST one connection, treating an existing one as success."""
    resp = requests.post(
        urljoin(brain_url, "/connections"),
        json={
            "name": name,
            "kind": "CUSTOM_MCP",
            "service": "mcp",
            "config": config,
            "bucket": buckets,
            "enabled": True,
        },
        cookies={"session": session_cookie},
        timeout=30,
    )

    if resp.status_code in (200, 201):
        print(f"  {name}: created ({len(config['tools_cache'])} tools)")

        return

    # A re-run against an already-seeded stack is normal and not a failure.
    if resp.status_code == 409:
        print(f"  {name}: already exists, leaving it alone")

        return

    raise SystemExit(f"  {name}: POST /connections returned {resp.status_code}: {resp.text[:300]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--brain-url", required=True, help="brain-go API base, e.g. http://127.0.0.1:8000")
    ap.add_argument(
        "--mcp-base", required=True, help="SREGym MCP base on an RFC1918 address, e.g. http://10.0.1.5:9954"
    )
    ap.add_argument("--session", required=True, help="Seeded owner session cookie (session_{32hex}_{30hex})")
    ap.add_argument("--session-id", default="sregym-bench", help="Value pinned as the kubectl sregym_ssid header")
    args = ap.parse_args()

    if "localhost" in args.mcp_base or "127.0.0.1" in args.mcp_base:
        raise SystemExit(
            "--mcp-base points at loopback. brain-go's SSRF guard blocks loopback with no flag "
            "that lifts it; allow_private_endpoint covers RFC1918 and ULA only. Use the "
            "instance's private address."
        )

    failures = 0

    for name, path, buckets, needs_session in ENDPOINTS:
        url = f"{args.mcp_base.rstrip('/')}/{path}/sse"
        headers = {"sregym_ssid": args.session_id} if needs_session else {}

        try:
            tools = asyncio.run(discover_tools(url, headers))
        except Exception as e:  # noqa: BLE001 - report and continue, one bad endpoint should not hide the rest
            print(f"  {name}: tool discovery failed against {url}: {e}")
            failures += 1

            continue

        if not tools:
            print(f"  {name}: discovery returned no tools; brain-go would expose none")
            failures += 1

            continue

        create_connection(
            args.brain_url,
            args.session,
            name,
            build_config(args.mcp_base, path, needs_session, args.session_id, tools),
            buckets,
        )

    if failures:
        raise SystemExit(
            f"{failures} endpoint(s) failed. Is the SREGym MCP server up and reachable at {args.mcp_base}?"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
