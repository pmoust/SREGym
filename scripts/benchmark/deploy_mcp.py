#!/usr/bin/env python3
"""Deploy SREGym's MCP server into the current cluster, before any run starts.

The conductor deploys this itself, but only from `deploy_app()`, which runs
per problem inside `start_problem()`. That is too late for us: brain-go's
CUSTOM_MCP connections have to exist before the agent takes its first turn,
and registering them means discovering each endpoint's tool schemas from a
live MCP server (brain-go never populates `tools_cache` itself, see
seed_connections.py). So the server has to be up before seeding, which is
before any problem has been deployed.

Running it here is safe rather than duplicative: `MCPServer.deploy()` skips
the apply when the deployment is already ready and only re-establishes the
port-forward if it went stale, so the conductor's own call later in the run
becomes a no-op.

Reads KUBECONFIG and MCP_SERVER_PORT from the environment, both of which
run_slot_env.sh sets per slot, so each slot deploys into its own cluster and
forwards to its own host port.
"""

import logging
import os
import sys

from sregym.service.mcp_server import MCPServer


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[deploy-mcp] %(message)s")
    log = logging.getLogger("deploy-mcp")

    kubeconfig = os.environ.get("KUBECONFIG")
    if not kubeconfig:
        log.error("KUBECONFIG is not set; refusing to deploy into whatever the default context is")
        return 1
    if not os.path.exists(kubeconfig):
        log.error("KUBECONFIG points at %s, which does not exist; create the cluster first", kubeconfig)
        return 1

    server = MCPServer()
    log.info("deploying the MCP server (kubeconfig=%s, host port=%s)", kubeconfig, server.port)
    server.deploy()

    # deploy() returns without raising even when the port-forward never came
    # up, so confirm the endpoint actually answers rather than trusting it.
    if not server._is_port_forward_healthy():
        log.error("MCP server deployed but the port-forward on %s is not serving", server.port)
        return 1

    log.info("MCP server ready on port %s", server.port)

    return 0


if __name__ == "__main__":
    sys.exit(main())
