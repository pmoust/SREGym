import logging
import os
import socket
import subprocess
import time

import requests

from sregym.paths import MCP_SERVER_K8S
from sregym.service.kubectl import KubeCtl

logger = logging.getLogger("all.sregym.mcp_server")

# How long a freshly started port-forward may take to serve traffic before it is
# treated as failed. It is not instant: the process binds immediately but cannot
# answer until the pod behind the service does, and a pod that satisfied
# wait_for_ready can still need a few seconds more.
PORT_FORWARD_READY_TIMEOUT = 20


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
                # Still probe. Not acting on the result is the point; not
                # looking would delete the only signal there is. The 15
                # "absent or stale" lines that diagnosed run-20260831-1752 came
                # from exactly this check, and a regression investigated after
                # this flag ships would otherwise have strictly less to go on.
                if self._is_port_forward_healthy():
                    logger.info("Port-forward is owned externally and serving.")
                else:
                    logger.warning(
                        "Port-forward is owned externally and NOT serving. "
                        "Leaving it to its owner; expect tool calls to time out until it recovers."
                    )
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
        """Check if the port-forward is actually serving traffic, not just bound.

        Any HTTP response counts, not specifically a 200. A reply of any status
        proves the request crossed the forward and reached the app, and every
        caller acts on a False by tearing down and restarting `kubectl
        port-forward`, which cannot change a status the app chose to send.

        It has to be any status rather than 200 to work at all. The pod does
        not run the image deployment.yaml names: deploy() below runs `kubectl
        apply -k`, and that kustomization's `images:` transform rewrites
        `sregym` to ghcr.io/sregym/sregym-mcp, which imagePullPolicy: Always
        pulls fresh from GHCR on every pod start. The published image was built
        2026-09-06 and has no /healthz route, so Starlette answers 404 there.
        Requiring 200 made this return False forever.

        Which path that actually saves: not the benchmark's fresh-cluster
        deploy, where SREGYM_EXTERNAL_PORT_FORWARD makes deploy() return before
        reaching here, nor the already-running branch, where the result is only
        logged. It is start_port_forward()'s early return. Under the strict-200
        form that return could never fire, so a healthy forward was killed and
        rebuilt three times and the method ended with none at all -- the exact
        regression its own docstring warns about.

        Probes /healthz rather than one of the /kubectl, /jaeger, /loki,
        /prometheus, /submit SSE mounts. Those hold a probing GET open rather
        than answering right away; see the healthz() docstring in
        mcp_server/sregym_mcp_server.py for what a bare GET against one of
        those mounts actually costs, and why that is not a session leak.
        /healthz answers immediately and never streams, whether it exists (200)
        or not (404), so this can be polled as often as a caller needs.

        That last point is load-bearing for this timeout in a way run_shard.sh's
        is not. `--max-time` there caps total wall clock; `timeout` here is
        per-read, so a response that trickles forever never trips it. Against
        /healthz the two agree. Retarget either at an SSE mount and the shell
        probe still ends while this one hangs for good.
        """
        try:
            requests.get(f"http://127.0.0.1:{self.port}/healthz", timeout=5)
        except Exception:
            # No HTTP response at all: connection refused, nothing listening,
            # or a socket that accepts and never answers within the timeout.
            # That last one is the failure #94 exists to catch.
            return False
        return True

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
            # Serving traffic, not merely alive. `kubectl port-forward` stays
            # running while forwarding to a pod that never becomes ready, and a
            # process check cannot tell the difference. Everything downstream
            # then pays a full call timeout per request against a socket that
            # accepts and never answers.
            #
            # Polled to a deadline rather than checked once. A pod that passed
            # wait_for_ready can still need a few seconds before its SSE
            # endpoint answers, and a single check three seconds in would tear
            # down a forward that was about to work -- three times, ending with
            # no forward at all, where the old poll() check would have kept one
            # that then served. That is the one way this change could be worse
            # than what it replaced.
            healthy = False
            deadline = time.time() + PORT_FORWARD_READY_TIMEOUT
            while time.time() < deadline:
                if self.port_forward_process.poll() is not None:
                    break  # the process itself exited; no point waiting out the deadline
                if self._is_port_forward_healthy():
                    healthy = True
                    break
                time.sleep(2)

            if healthy:
                os.environ["MCP_SERVER_PORT"] = str(self.port)
                logger.info(f"Port forwarding established at {self.port}. MCP_SERVER_PORT set.")
                break

            logger.warning(
                "Port forwarding did not become healthy within "
                f"{PORT_FORWARD_READY_TIMEOUT}s "
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
