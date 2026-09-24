#!/usr/bin/env python3
"""Mint the slot's edge API key and print it on stdout.

The edge agent cannot use the slot's CLI key. brain-go's edge ingest resolves
a Bearer token to an account and then refuses anything that is not an edge key
(cmd/brain/internal/edgesvc/auth.go and internal/api/edge/handler.go both
check APIKeyType == edge), which is the same narrowing the MCP surface
applies. `brainctl seed dev-stack` mints one CLI key and one session and no
edge key, so the native arm mints its own through the account's own route.

Not a second auth path: POST /account/api-keys is the product's own endpoint,
authenticated by the same owner session seed_slot.sh already holds and already
uses for connections, and the key it returns belongs to the same account. The
account the edge agent publishes under is therefore the account the driver
investigates under, with no extra bookkeeping to keep the two in step.

Only the key reaches stdout, so a caller can read it with a command
substitution. Everything else goes to stderr.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
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

REQUEST_TIMEOUT_S = 30


def mint(brain_url: str, session_cookie: str, name: str) -> str:
    """Create an edge-type API key and return its raw token."""
    csrf = csrf_token(brain_url, session_cookie)

    resp = requests.post(
        urljoin(brain_url, "/api/account/api-keys"),
        json={"name": name, "key_type": "edge"},
        headers={"X-CSRF-Token": csrf},
        # By hand rather than via `cookies=`, for the reason
        # seed_connections.csrf_token documents: CSRF-TOKEN is Secure and this
        # instance is reached over plain http.
        cookies={"session": session_cookie, "CSRF-TOKEN": csrf},
        timeout=REQUEST_TIMEOUT_S,
    )

    if resp.status_code not in (200, 201):
        raise SystemExit(f"POST /api/account/api-keys returned {resp.status_code}: {resp.text[:300]}")

    # The envelope is {"success": true, "data": {...,"key": "<raw token>"}}.
    # brain-go returns the raw token once, at create, and offers no read-back,
    # so a missing field here is fatal rather than something to retry.
    data = resp.json().get("data") or {}
    token = data.get("key") or ""
    if not token:
        raise SystemExit(f"POST /api/account/api-keys returned no key field: {sorted(data)}")

    return token


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--brain-url", required=True, help="brain-go api base, e.g. http://127.0.0.1:8000")
    ap.add_argument("--session", required=True, help="Seeded owner session cookie")
    ap.add_argument("--name", default="sregym-edge", help="Name recorded on the key")
    args = ap.parse_args()

    sys.stdout.write(mint(args.brain_url, args.session, args.name) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
