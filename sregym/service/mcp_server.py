import logging
import os
import socket
import subprocess
import time

import requests

from sregym.paths import MCP_SERVER_K8S
from sregym.service.kubectl import KubeCtl

logger = logging.getLogger("all.sregym.mcp_server")


class MCPServer:
    def __init__(self):
        self.namespace = "sregym"
        self.service_name = "mcp-server"
        # Host side of the port-forward below, so it has to differ between runs
        # sharing a host. The in-cluster service port stays 9954 either way.
        self.port = int(os.getenv("MCP_SERVER_PORT", "9954"))
        self.port_forward_process = None
        self.kubectl = KubeCtl()

    def _is_running(self) -> bool:
        """Check if the MCP server deployment already exists and is ready."""
        result = self.kubectl.exec_command(
            f"kubectl get deployment {self.service_name} -n {self.namespace} -o jsonpath='{{.status.readyReplicas}}'"
        )
        value = result.strip().strip("'")
        # exec_command returns stderr on failure (e.g. "Error from server (NotFound)"),
        # so only treat a purely numeric positive value as "running".
        return value.isdigit() and int(value) > 0

    def _ensure_rbac(self):
        """Ensure RBAC resources exist even if the MCP server pod is already running."""
        rbac_dir = MCP_SERVER_K8S
        for resource in ["clusterrole.yaml", "clusterrolebinding.yaml"]:
            self.kubectl.exec_command(f"kubectl apply -f {rbac_dir / resource}")
        logger.info("MCP server RBAC resources ensured.")

    def _forward_is_external(self) -> bool:
        """Whether something outside this process owns the port-forward.

        Set SREGYM_EXTERNAL_PORT_FORWARD=1 when a caller establishes and
        supervises the forward itself and this class must not touch it.

        Without it, deploy() runs per problem and replaces a forward it does not
        own. On a long benchmark run that forward is not a deploy-time
        convenience: the agent's MCP connection is registered against the
        forwarded address, so every tool call for the whole run goes through it.
        Killing it costs one full call timeout per tool call until it recovers.

        Off by default, so the standalone behaviour is unchanged.
        """
        return os.environ.get("SREGYM_EXTERNAL_PORT_FORWARD", "").strip().lower() in ("1", "true", "yes")

    def deploy(self):
        """Deploy the MCP server into the cluster via kustomize.

        Skips redeployment if the MCP server is already running to avoid disrupting existing connections.
        Always ensures RBAC resources exist regardless of pod state.
        """
        self._ensure_rbac()

        if self._is_running():
            logger.info("MCP server already running, skipping redeploy.")
            if self._forward_is_external():
                logger.info("Port-forward is owned externally, leaving it alone.")
            elif not self._is_port_forward_healthy():
                logger.info("Port-forward is absent or stale, restarting.")
                self.start_port_forward()
            return

        self.kubectl.exec_command(f"kubectl apply -k {MCP_SERVER_K8S}")
        self.kubectl.wait_for_ready(self.namespace)

        if self._forward_is_external():
            logger.info("MCP server deployed; port-forward is owned externally.")
            return

        self.start_port_forward()
        logger.info("MCP server deployed successfully.")

    def is_port_in_use(self, port: int) -> bool:
        """Check if a local TCP port is already bound."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    def _is_port_forward_healthy(self) -> bool:
        """Check if the port-forward is actually serving traffic, not just bound."""
        try:
            resp = requests.get(f"http://127.0.0.1:{self.port}/kubectl/sse", stream=True, timeout=5)
            resp.close()
            # SSE endpoint returns 200 with text/event-stream
            return resp.status_code == 200
        except Exception:
            return False

    def _kill_stale_port_forward(self):
        """Kill any existing kubectl port-forward process on our port."""
        if self.port_forward_process and self.port_forward_process.poll() is None:
            logger.info("Killing existing port-forward process to re-establish fresh connection.")
            self.stop_port_forward()
            self.port_forward_process = None

        # Also kill orphaned port-forward processes from previous runs that we
        # don't hold a handle to (e.g. the process survived a previous crash).
        if self.is_port_in_use(self.port):
            try:
                result = subprocess.run(f"lsof -ti tcp:{self.port}", shell=True, capture_output=True, text=True)
                for pid in result.stdout.strip().split():
                    if pid.isdigit():
                        logger.info(f"Killing orphaned process {pid} on port {self.port}")
                        subprocess.run(f"kill {pid}", shell=True)
                time.sleep(1)
            except Exception as e:
                logger.warning(f"Failed to kill stale port-forward: {e}")

    def start_port_forward(self):
        """Starts port-forwarding to access the MCP server.

        Returns early when the port already serves traffic. The kill below is
        destructive and unconditional, so doing it before checking meant a
        healthy forward was torn down and, if the three attempts below then
        failed, replaced by nothing at all.
        """
        if self._is_port_forward_healthy():
            logger.info(f"Port-forward on {self.port} is already serving; leaving it in place.")
            os.environ["MCP_SERVER_PORT"] = str(self.port)
            return

        self._kill_stale_port_forward()

        for attempt in range(3):
            if self.is_port_in_use(self.port):
                logger.debug(
                    f"Port {self.port} is already in use. Attempt {attempt + 1} of 3. Retrying in 3 seconds..."
                )
                time.sleep(3)
                continue

            command = (
                f"kubectl port-forward svc/{self.service_name} {self.port}:9954 -n {self.namespace} --address 0.0.0.0"
            )
            self.port_forward_process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(3)

            # Serving traffic, not merely alive. `kubectl port-forward` stays
            # running while forwarding to a pod that never becomes ready, and a
            # process check cannot tell the difference. Everything downstream
            # then pays a full call timeout per request against a socket that
            # accepts and never answers.
            if self._is_port_forward_healthy():
                os.environ["MCP_SERVER_PORT"] = str(self.port)
                logger.info(f"Port forwarding established at {self.port}. MCP_SERVER_PORT set.")
                break

            logger.warning(
                "Port forwarding did not become healthy "
                f"(process {'alive' if self.port_forward_process.poll() is None else 'exited'}). Retrying..."
            )
            self.stop_port_forward()
            self.port_forward_process = None
        else:
            logger.warning("Failed to establish port forwarding after multiple attempts.")

    def stop_port_forward(self):
        """Stops the kubectl port-forward command and cleans up resources."""
        if self.port_forward_process:
            self.port_forward_process.terminate()
            try:
                self.port_forward_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("Port-forward process did not terminate in time, killing...")
                self.port_forward_process.kill()

            if self.port_forward_process.stdout:
                self.port_forward_process.stdout.close()
            if self.port_forward_process.stderr:
                self.port_forward_process.stderr.close()

            logger.info("Port forwarding for MCP server stopped.")
