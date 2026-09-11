import logging
import os

import uvicorn
from fastmcp.server.http import create_sse_app
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

from mcp_server.configs.load_all_cfg import mcp_server_cfg
from mcp_server.jaeger_server import mcp as observability_mcp
from mcp_server.kubectl_mcp_tools import kubectl_mcp
from mcp_server.loki_server import mcp as loki_mcp
from mcp_server.prometheus_server import mcp as prometheus_mcp
from mcp_server.submit_server import mcp as submit_mcp
from sregym.service.k8s_proxy import KubernetesAPIProxy

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def healthz(_request: Request) -> PlainTextResponse:
    """Liveness probe for whatever is port-forwarding to this pod.

    Deliberately not a Mount under any of the MCP apps below. The expensive
    part of probing one of those with a bare GET is not tool-session
    allocation: KubectlToolSet is only built by get_tools() in
    kubectl_mcp_tools.py, which only runs inside an @kubectl_mcp.tool()
    function, i.e. only on an actual JSON-RPC call POSTed to /messages, never
    on a plain GET. A bare GET against one of the SSE mounts reaches only
    SseServerTransport.connect_sse (the vendored mcp/server/sse.py), which
    does leak: it adds an entry to _read_stream_writers that is never
    removed on disconnect. Real, but small next to what actually costs time.

    The real cost is on the probing side. `curl -w '%{http_code}' --max-time
    N` against a healthy, immediately-responding SSE endpoint still takes the
    full N seconds to return: the endpoint answers with 200 right away, but
    the body is an endless stream, so curl keeps reading until --max-time
    kills it, not the server. Verified against a minimal local SSE endpoint:
    a 200 that still took 5.13s wall clock at --max-time 5. A prober on a 15s
    cycle was paying that on every healthy check. /healthz answers
    immediately and never streams, so a
    prober gets an instant reply and can poll as often as it likes.
    """
    return PlainTextResponse("ok")


routes = [
    Route("/healthz", healthz),
    Mount("/kubectl", app=create_sse_app(kubectl_mcp, "/messages/", "/sse")),
    Mount("/jaeger", app=create_sse_app(observability_mcp, "/messages/", "/sse")),
    Mount("/loki", app=create_sse_app(loki_mcp, "/messages/", "/sse")),
    Mount("/prometheus", app=create_sse_app(prometheus_mcp, "/messages/", "/sse")),
    Mount("/submit", app=create_sse_app(submit_mcp, "/messages/", "/sse")),
]

app = Starlette(
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        ),
    ],
    routes=routes,
)

if __name__ == "__main__":
    # Start the Kubernetes API filtering proxy so kubectl commands executed by
    # MCP tools go through the proxy and have load-generator / chaos resources
    # filtered out.  The proxy picks up in-cluster ServiceAccount credentials
    # automatically and listens on a local port.
    proxy_port = 16443
    proxy = KubernetesAPIProxy(listen_port=proxy_port)
    proxy.start()
    kubeconfig_path = proxy.generate_agent_kubeconfig()
    os.environ["KUBECONFIG"] = kubeconfig_path
    logger.info(f"Kubernetes API proxy started on port {proxy_port}, KUBECONFIG={kubeconfig_path}")

    port = mcp_server_cfg.mcp_server_port
    host = "0.0.0.0" if mcp_server_cfg.expose_server else "127.0.0.1"
    logger.info("Starting SREGym MCP Server")
    uvicorn.run(app, host=host, port=port)
