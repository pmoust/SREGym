import anyio.to_thread
import requests
from fastmcp import FastMCP

from clients.stratus.configs.langgraph_tool_configs import LanggraphToolConfig
from clients.stratus.stratus_utils.get_logger import get_logger

logger = get_logger()
logger.info("Starting Submission MCP Server")

langgraph_tool_config = LanggraphToolConfig()

mcp = FastMCP("Submission MCP Server")


def _submit_sync(ans: str) -> dict[str, str]:
    logger.info("[submit_mcp] submit mcp called")
    # FIXME: reference url from config file, remove hard coding
    url = langgraph_tool_config.benchmark_submit_url
    headers = {"Content-Type": "application/json"}
    # Match curl behavior: send "\"yes\"" when ans is "yes"
    payload = {"solution": f"{ans}"}

    try:
        response = requests.post(url, json=payload, headers=headers)
        logger.info(f"[submit_mcp] Response status: {response.status_code}, text: {response.text}")
        return {"status": str(response.status_code), "text": str(response.text)}

    except Exception as e:
        logger.error(f"[submit_mcp] HTTP submission failed: {e}")
        return {"status": "N/A", "text": f"[submit_mcp] HTTP submission failed: {e}"}


@mcp.tool(name="submit")
async def submit(ans: str) -> dict[str, str]:
    """Submit task result to benchmark

    Args:
        ans (str): task result that the agent submits

    Returns:
        dict[str]: http response code and response text of benchmark submission server
    """
    # requests.post is a blocking network call with no explicit timeout, so it
    # can run indefinitely. FastMCP calls a sync tool function inline on the
    # single uvicorn worker, so running it directly here would block every
    # other request on this process, including the /healthz liveness probe
    # the port-forward supervisor polls. See the matching comment on
    # exec_kubectl_cmd_safely in mcp_server/kubectl_mcp_tools.py.
    return await anyio.to_thread.run_sync(_submit_sync, ans)
