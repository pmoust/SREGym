"""brain-go agent driver for SREGym.

Drives the two-phase benchmark flow (diagnosis, then mitigation) by calling
brain-go's CLI chat endpoint over SSE and submitting results to the SREGym
conductor.

brain-go runs on this box: BRAIN_API_URL defaults to localhost:8001, and
scripts/benchmark/setup_brain_go.sh stands it up and seeds it. The agent
investigates through CUSTOM_MCP connections pointing at SREGym's MCP server
(kubectl, prometheus, jaeger, loki), which that script registers.

Written fresh rather than ported from clients/brain/driver.py. That driver
carries nine SREGYM_* interventions built to work around a 24 GB ARM laptop, and
they are why engineering#799's numbers cannot be read. None of them are here.

Two deliberate differences from the brain driver, both load-bearing:

  * No retry on a read timeout. See run_turn.
  * No pkill of other runs' port-forwards, and no testbed mutation of any kind.

Tracked by NOFireAI/brain-go#2219.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests

sregym_root = Path(__file__).resolve().parents[2]
if str(sregym_root) not in sys.path:
    sys.path.insert(0, str(sregym_root))

from clients.brain_go.sse import iter_sse_events  # noqa: E402
from clients.harness.problem_id import resolve_problem_id  # noqa: E402
from logger import init_logger  # noqa: E402

init_logger()
logger = logging.getLogger("all.brain_go.driver")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BRAIN_API_URL = os.environ.get("BRAIN_API_URL", "http://localhost:8001")
BRAIN_API_KEY = os.environ.get("BRAIN_API_KEY", "")

# The api service, and the owner session that authenticates against it. Both
# come from seed_slot.sh. They are separate from BRAIN_API_URL/BRAIN_API_KEY
# above, which point at the chat service: a CLI API key carries no permissions
# at all, so anything on the api service uses the session instead.
#
# Unset means "do not open an investigation", which keeps a local run against a
# hand-started stack working without them.
BRAIN_ADMIN_API_URL = os.environ.get("BRAIN_ADMIN_API_URL", "")
BRAIN_SESSION = os.environ.get("BRAIN_SESSION", "")

API_HOSTNAME = os.environ.get("API_HOSTNAME", "localhost")
API_PORT = os.environ.get("API_PORT", "8000")
CONDUCTOR_URL = f"http://{API_HOSTNAME}:{API_PORT}"

# brain-go sends a ": keepalive" comment every 15s on an idle stream
# (NOFireAI/brain-go#2250), so a gap this long means the connection is
# actually dead, not just a slow tool call or a long model turn. 6x the
# keepalive interval leaves margin for network jitter or a GC pause without
# giving up minutes late on a genuinely stuck run.
SSE_READ_TIMEOUT = int(os.environ.get("BRAIN_SSE_READ_TIMEOUT", "90"))
SSE_CONNECT_TIMEOUT = 30

MAX_TOOL_RESULT_CHARS = 100_000
LOCAL_CMD_TIMEOUT = int(os.environ.get("BRAIN_LOCAL_CMD_TIMEOUT", "120"))


def _parse_tools(env_value: str) -> set[str]:
    return {t.strip() for t in env_value.split(",") if t.strip()}


# The two phases have opposite tool policies, and the diagnosis one is the
# measurement. Giving brain-go kubectl during diagnosis would let it read the
# injected fault straight off the cluster, bypassing the observability context
# the benchmark exists to evaluate. Mitigation is the inverse: the task is to
# change the cluster.
#
# This is also the Track A / Track B seam. Track B (empty here) measures
# diagnosis from the entity graph alone, which is harder than any published
# SREGym result. Track A sets kubectl to match how every agent in the paper
# runs, and is the only comparable number.
DIAGNOSIS_LOCAL_TOOLS = _parse_tools(os.environ.get("BRAIN_DIAGNOSIS_TOOLS", ""))
MITIGATION_LOCAL_TOOLS = _parse_tools(os.environ.get("BRAIN_MITIGATION_TOOLS", "kubectl,helm"))


class TurnFailed(RuntimeError):
    """A chat turn did not complete. Never retried on the same thread."""


# ---------------------------------------------------------------------------
# brain-go chat
# ---------------------------------------------------------------------------


def brain_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {BRAIN_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }


def _timeout_message(where: str) -> str:
    """Explain why a timeout is fatal rather than retried. See run_turn."""
    return (
        f"no data for {SSE_READ_TIMEOUT}s ({where}), well past brain-go's 15s keepalive "
        f"cadence, so the connection is dead. Not retrying: the turn is already persisted "
        f"server-side, so a retry would resume partial state with a duplicated message "
        f"rather than redoing the turn."
    )


def run_turn(message: str, thread_id: str, allowed_tools: set[str]) -> str:
    """Run one brain-go turn to completion over /cli/chat SSE.

    Deliberately not retried. The brain driver retries on requests.Timeout with
    the comment "the failed attempt never appended to the conversation", which
    is true only for its stated case, an immediate 500 from a stale pooled
    connection. It is false for a read timeout, and retrying then corrupts the
    thread rather than redoing it:

      * graphexec.execute persists a checkpoint with source "input", step -1
        before the first node runs, and that state already carries the user
        message.
      * The message is built with no ID, and engine.AddMessages only indexes
        non-empty IDs, so it always appends rather than deduplicating.
      * The run context derives from the request context, so a disconnect
        cancels it, but every node that already finished has persisted a "loop"
        checkpoint.
      * rehydrateState loads the latest checkpoint, so a retry resumes on top of
        that partial state and asks the same question again.

    The result is a half-finished investigation plus a duplicated turn, which
    answers as if nothing happened. For a benchmark that is a contaminated run
    rather than a visible failure, so this fails loudly instead.
    """
    payload = {"message": message, "thread_id": thread_id, "cli_mode": True}
    accumulated = ""
    final = ""

    try:
        resp = requests.post(
            f"{BRAIN_API_URL}/cli/chat",
            json=payload,
            headers=brain_headers(),
            stream=True,
            timeout=(SSE_CONNECT_TIMEOUT, SSE_READ_TIMEOUT),
        )
    except requests.exceptions.Timeout as e:
        # The request reached brain-go even though no headers came back, so the
        # turn may already be persisted. Same reasoning as the streaming case.
        raise TurnFailed(_timeout_message("waiting for the response to start")) from e
    except requests.RequestException as e:
        # A connection-level failure happens before graphexec writes its input
        # checkpoint, so nothing was persisted. Safe to retry, but the harness
        # owns that decision, not this driver.
        raise TurnFailed(f"could not open the chat stream: {e}") from e

    if not resp.ok:
        raise TurnFailed(f"POST /cli/chat returned {resp.status_code}: {resp.text[:300]}")

    try:
        with resp:
            for event, data in iter_sse_events(resp.iter_lines(decode_unicode=True)):
                if event == "token":
                    accumulated = data.get("accumulated") or accumulated
                elif event == "tool_start":
                    logger.info("tool_start: %s", data.get("tool"))
                elif event == "tool_end":
                    logger.info("tool_end: %s", data.get("tool"))
                elif event == "execute_local_tool":
                    execute_local_tool(data, allowed_tools)
                elif event == "response":
                    final = data.get("content") or final
                elif event == "error":
                    raise TurnFailed(f"brain-go reported an error: {data.get('message')}")
                elif event == "done":
                    break
    except requests.exceptions.Timeout as e:
        raise TurnFailed(_timeout_message("mid-stream")) from e
    except requests.RequestException as e:
        raise TurnFailed(f"chat stream failed: {e}") from e

    return final or accumulated


def execute_local_tool(payload: dict[str, Any], allowed_tools: set[str]) -> None:
    """Run a command brain-go asked the client to execute, and post the result back."""
    tool_call_id = payload.get("tool_call_id", "")
    command = payload.get("command", "")
    tool_type = payload.get("tool_type", "bash")

    if tool_type not in allowed_tools:
        logger.warning("refusing local tool_type=%s: %s", tool_type, command)
        result = {
            "tool_call_id": tool_call_id,
            "result": "",
            "exit_code": 1,
            "error": (f"tool_type '{tool_type}' is not permitted in this phase. Use your connected MCP tools instead."),
        }
    else:
        logger.info("executing local %s: %s", tool_type, command)
        result = _run_local(tool_call_id, command)

    try:
        resp = requests.post(
            f"{BRAIN_API_URL}/cli/tool-result",
            json=result,
            headers={"Authorization": f"Bearer {BRAIN_API_KEY}", "Content-Type": "application/json"},
            timeout=30,
        )
        if not resp.ok:
            logger.error("tool-result POST failed: %s %s", resp.status_code, resp.text[:200])
    except requests.RequestException as e:
        # Do not raise: the turn is still running and the model can proceed
        # without this result. Failing here would abandon a live stream.
        logger.error("tool-result POST error: %s", e)


def _run_local(tool_call_id: str, command: str) -> dict[str, Any]:
    try:
        proc = subprocess.run(  # noqa: S602 - the command comes from brain-go, gated by allowed_tools
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=LOCAL_CMD_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "tool_call_id": tool_call_id,
            "result": "",
            "exit_code": 124,
            "error": f"command timed out after {LOCAL_CMD_TIMEOUT}s",
        }
    except Exception as e:  # noqa: BLE001 - report any launch failure back to the model
        return {"tool_call_id": tool_call_id, "result": "", "exit_code": 1, "error": str(e)}

    output = proc.stdout
    if proc.stderr:
        output = f"{output}\n{proc.stderr}" if output else proc.stderr

    return {
        "tool_call_id": tool_call_id,
        "result": output[:MAX_TOOL_RESULT_CHARS],
        "exit_code": proc.returncode,
        "error": None if proc.returncode == 0 else f"exit code {proc.returncode}",
    }


# ---------------------------------------------------------------------------
# Conductor
# ---------------------------------------------------------------------------


def get_app_info(max_retries: int = 6, backoff: int = 5) -> dict[str, Any]:
    """Fetch the problem's app metadata, which the prompts interpolate."""
    last: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(f"{CONDUCTOR_URL}/get_app", timeout=10)
            resp.raise_for_status()

            return resp.json()
        except requests.RequestException as e:
            last = e
            logger.warning("get_app attempt %d/%d failed: %s", attempt, max_retries, e)
            time.sleep(backoff)

    raise TurnFailed(f"could not reach the conductor at {CONDUCTOR_URL}: {last}")


def wait_for_stage(targets: set[str], timeout: int = 300) -> str:
    """Block until the conductor reports one of the target stages."""
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            resp = requests.get(f"{CONDUCTOR_URL}/status", timeout=10)
            resp.raise_for_status()
            stage = resp.json().get("stage", "")
            if stage in targets:
                return stage
        except requests.RequestException as e:
            logger.warning("status poll failed: %s", e)

        time.sleep(2)

    raise TimeoutError(f"conductor did not reach any of {sorted(targets)} within {timeout}s")


def submit_to_conductor(solution: str) -> None:
    logger.info("submitting to conductor (%d chars)", len(solution))
    resp = requests.post(f"{CONDUCTOR_URL}/submit", json={"solution": solution}, timeout=30)

    if not resp.ok:
        logger.error("submit failed: %s %s", resp.status_code, resp.text[:200])

    resp.raise_for_status()


def open_investigation(problem_id: str) -> str | None:
    """Open an investigation and return the thread id to run this problem on.

    brain-go only persists an RCI report when the turn carries an investigation
    id, and it derives that id from the thread id: `completeInvestigation`
    requires a non-empty `InvestigationID`, which `InvestigationUUIDFromThread`
    produces only for a thread of the form `cli:inv-<uuid>`. A thread we invent
    ourselves parses to nothing, the write is skipped, and the run's
    hypotheses, their verdicts and their probabilities are discarded when the
    workflow ends.

    The id and the row are created together by the api service, so inventing an
    `inv-` prefixed id here would not work either: it would parse to a
    valid-looking uuid that updates no row. We ask for a real one.

    Returns None when the api service or session was not supplied, which leaves
    the caller on a self-made thread and the old, non-persisting behaviour.
    """
    if not BRAIN_ADMIN_API_URL or not BRAIN_SESSION:
        logger.warning(
            "no BRAIN_ADMIN_API_URL/BRAIN_SESSION; this run will not persist its "
            "hypotheses (see NOFireAI/sregym#28)"
        )

        return None

    base = BRAIN_ADMIN_API_URL.rstrip("/")
    cookies = {"session": BRAIN_SESSION}

    # Prime the double-submit CSRF token. The cookie is Secure, so requests'
    # jar will not attach it to a plain http:// request and it has to be passed
    # by hand on the POST -- the same dance seed_connections.py does, for the
    # same reason.
    primed = requests.get(f"{base}/health", cookies=cookies, timeout=30)
    csrf = primed.cookies.get("CSRF-TOKEN")

    if not csrf:
        raise RuntimeError(
            f"no CSRF-TOKEN cookie in {base}/health ({primed.status_code}); "
            "is BRAIN_ADMIN_API_URL pointing at the api service rather than chat?"
        )

    resp = requests.post(
        f"{base}/api/v2/investigations",
        json={"title": f"SREGym {problem_id}", "description": f"SREGym benchmark problem {problem_id}"},
        headers={"X-CSRF-Token": csrf},
        cookies={"session": BRAIN_SESSION, "CSRF-TOKEN": csrf},
        timeout=30,
    )

    if not resp.ok:
        raise RuntimeError(
            f"POST /api/v2/investigations returned {resp.status_code}: {resp.text[:300]}"
        )

    thread_id = (resp.json().get("data") or {}).get("chat_thread_id")

    if not thread_id:
        raise RuntimeError(f"investigation response carried no chat_thread_id: {resp.text[:300]}")

    return thread_id


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

DIAGNOSIS_PROMPT = """You are investigating a Kubernetes application failure.

Application: {app_name}
Namespace: {namespace}
Description: {descriptions}

Diagnose the root cause of the failure.

Work autonomously and do not ask clarifying questions. Investigate until you can
state the root cause with confidence.

Provide a clear, concise diagnosis identifying:
- What component is failing and how
- The root cause of the failure
- Supporting evidence from your investigation

Be specific and technical. State the root cause clearly in your final answer."""

MITIGATION_PROMPT = """Your task now is to FIX the failure in this application by
applying changes to the cluster.

Application: {app_name}
Namespace: {namespace}

Apply the fix, then confirm the application has recovered."""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    if not BRAIN_API_KEY:
        logger.error("BRAIN_API_KEY is unset. scripts/benchmark/setup_brain_go.sh writes it.")

        return 1

    problem_id = resolve_problem_id()
    logger.info("brain-go driver starting: problem=%s brain=%s", problem_id, BRAIN_API_URL)
    logger.info("diagnosis tools: %s", sorted(DIAGNOSIS_LOCAL_TOOLS) or "none (graph only)")
    logger.info("mitigation tools: %s", sorted(MITIGATION_LOCAL_TOOLS))

    try:
        wait_for_stage({"diagnosis"})
    except TimeoutError:
        logger.exception("conductor never reached the diagnosis stage")

        return 1

    app_info = get_app_info()
    params = {
        "app_name": app_info.get("name", "unknown"),
        "namespace": app_info.get("namespace", "unknown"),
        "descriptions": app_info.get("descriptions", ""),
    }

    # One thread for both phases, so the mitigation turn sees the diagnosis it
    # is acting on.
    #
    # An investigation-backed thread when we can open one, so brain-go persists
    # the report and the run's hypotheses survive it (#28). The self-made thread
    # is the fallback: it still runs, it just records nothing.
    thread_id = open_investigation(problem_id) or f"cli:sregym-{problem_id}-{uuid.uuid4().hex[:8]}"
    logger.info("thread: %s", thread_id)

    logger.info("=" * 30 + " DIAGNOSIS " + "=" * 30)
    try:
        diagnosis = run_turn(DIAGNOSIS_PROMPT.format(**params), thread_id, DIAGNOSIS_LOCAL_TOOLS)
    except TurnFailed:
        logger.exception("diagnosis turn failed")
        submit_to_conductor("")

        return 1

    logger.info("diagnosis (%d chars): %s", len(diagnosis), diagnosis[:300])
    submit_to_conductor(diagnosis)

    stage = wait_for_stage({"mitigation", "done"})
    if stage == "done":
        logger.info("conductor went straight to done after diagnosis")

        return 0

    logger.info("=" * 30 + " MITIGATION " + "=" * 29)
    try:
        mitigation = run_turn(MITIGATION_PROMPT.format(**params), thread_id, MITIGATION_LOCAL_TOOLS)
        logger.info("mitigation (%d chars): %s", len(mitigation), mitigation[:300])
    except TurnFailed:
        logger.exception("mitigation turn failed")

    # Mitigation is graded from live cluster state, not from this text, so the
    # submission body is deliberately empty.
    submit_to_conductor("")

    try:
        wait_for_stage({"done", "tearing_down"})
    except TimeoutError:
        logger.warning("conductor did not reach done; exiting anyway")

    return 0


if __name__ == "__main__":
    sys.exit(main())
