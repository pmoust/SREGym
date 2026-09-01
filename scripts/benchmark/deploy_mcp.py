#!/usr/bin/env python3
"""Deploy SREGym's MCP server into the current cluster, before any run starts.

The conductor deploys this itself, but only from `deploy_app()`, which runs
per problem inside `start_problem()`. That is too late for us: brain-go's
CUSTOM_MCP connections have to exist before the agent takes its first turn,
and registering them means discovering each endpoint's tool schemas from a
live MCP server (brain-go never populates `tools_cache` itself, see
seed_connections.py). So the server has to be up before seeding, which is
before any problem has been deployed.

Running it here is safe rather than duplicative: `MCPServer.deploy()` skips the
apply when the deployment is already ready.

This used to add "so the conductor's own call later in the run becomes a
no-op". That was wrong, and it is the claim NOFireAI/sregym#34 retracts. The
conductor's per-problem deploy() took the restart path 15 times in
run-20260831-1752, killed a forward on 8 of those (the port was already free on
the other 7), and the attempt after stalled in 12 of the 15. It is
a no-op now only because SREGYM_EXTERNAL_PORT_FORWARD is set, for this call and
for the conductor's.

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

    # Belt and braces. With SREGYM_EXTERNAL_PORT_FORWARD set, which the
    # bootstrap sets for this call, deploy() starts no forward and there is
    # nothing here to stop.
    #
    # Without the flag it would, and that forward would be worthless: it is a
    # subprocess.Popen child the interpreter reaps when this script exits
    # seconds later. The conductor never sees that because it keeps its
    # MCPServer alive for a whole run. The first run to get this far logged
    # "MCP server ready on port 9954" and then every slot timed out on that
    # same port with no kubectl port-forward alive.
    #
    # Either way the caller owns a forward that outlives us: run_slot in
    # terraform/templates/bootstrap.sh.tftpl starts one with nohup so it lives
    # as long as the slot does.
    server.stop_port_forward()

    log.info("MCP server deployed; the caller owns the port-forward on %s", server.port)

    return 0


if __name__ == "__main__":
    sys.exit(main())
