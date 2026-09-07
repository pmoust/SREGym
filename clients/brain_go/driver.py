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

import collections
import datetime
import json
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


# Local tools the driver will execute on brain-go's behalf when it asks, per
# phase. Diagnosis runs with none by default and mitigation with kubectl and
# helm, since only mitigation is meant to change the cluster.
#
# This is NOT a switch that takes cluster access away from the agent, and an
# earlier version of this comment claimed it was. It gates only this driver's
# own execute_local_tool shell-out. brain-go reaches the cluster through the
# sregym-kubectl MCP connection the harness registers on the tenant
# (scripts/benchmark/seed_connections.py), which nothing here touches: the
# second full run made 573 exec_kubectl_cmd_safely calls during the diagnosis
# phase, about 27 per problem, with this set to empty.
#
# So diagnosis runs with full cluster access either way, which is what every
# agent in the SREGym paper gets and is the comparable configuration. A
# genuinely graph-only measurement would have to disable that connection for
# the tenant, and would only be meaningful once the entity graph is populated
# at all (NOFireAI/brain-go#2410). Nothing populates it here: brain-go derives
# the graph from Prometheus on its periodic entity-graph-reindex schedule,
# whose target lister takes Prometheus-compatible connections only (Prometheus
# itself, or Grafana carrying app metrics) and skips the rest, and the
# connect-time discovery path has providers for AWS, GCP, Azure, Cloudflare,
# GitHub and MongoDB Atlas but none for MCP. A CUSTOM_MCP-only tenant never
# gets one. See #26.
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


def run_turn(
    message: str,
    thread_id: str,
    allowed_tools: set[str],
    tool_calls: collections.Counter[str] | None = None,
    tool_calls_by_node: collections.Counter[str] | None = None,
) -> str:
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
                    tool = data.get("tool") or "unknown"
                    if tool_calls is not None:
                        tool_calls[tool] += 1
                    # brain-go's own extension (brain-go#2687): the RCI node
                    # or signal that made the call, not necessarily the same
                    # thing as which tool was called. Absent on an older
                    # brain-go, same fallback as an unnamed tool.
                    node = data.get("node") or "unknown"
                    if tool_calls_by_node is not None:
                        tool_calls_by_node[node] += 1
                    logger.info("tool_start: %s", tool)
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


def open_investigation(problem_id: str) -> tuple[str, str] | None:
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

    Returns (thread_id, investigation_uuid), or None when the api service or
    session was not supplied, which leaves the caller on a self-made thread and
    the old, non-persisting behaviour.
    """
    if not BRAIN_ADMIN_API_URL or not BRAIN_SESSION:
        logger.warning(
            "no BRAIN_ADMIN_API_URL/BRAIN_SESSION; this run will not persist its hypotheses (see NOFireAI/sregym#28)"
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

    # The title is how a row is attributed afterwards. n_attempts>1 opens one
    # investigation per attempt, and --resume re-runs a partially finished
    # problem for all of its attempts again, so "SREGym <problem>" alone leaves
    # several identical rows to be told apart by creation time.
    label = f"SREGym {problem_id} [{_run_label()}]"

    resp = requests.post(
        f"{base}/api/v2/investigations",
        json={"title": label, "description": f"SREGym benchmark problem {problem_id}"},
        headers={"X-CSRF-Token": csrf},
        cookies={"session": BRAIN_SESSION, "CSRF-TOKEN": csrf},
        timeout=30,
    )

    if not resp.ok:
        raise RuntimeError(f"POST /api/v2/investigations returned {resp.status_code}: {resp.text[:300]}")

    data = resp.json().get("data") or {}
    thread_id, uuid_str = data.get("chat_thread_id"), data.get("uuid")

    if not thread_id or not uuid_str:
        raise RuntimeError(f"investigation response was incomplete: {resp.text[:300]}")

    return thread_id, uuid_str


def _run_label() -> str:
    """Identify which run, shard and slot a row belongs to.

    The fleet bootstrap exports all three per slot before launching a run. A
    local run supplies none of them and gets a random suffix instead, which
    keeps rows distinguishable without pretending to be attributable.

    The first version of this read three variables nothing in the repo wrote,
    so it always took the fallback and the identity it advertised was never
    present. If that regresses, TestRunLabel below fails.
    """
    parts = [os.environ.get(k, "") for k in ("SREGYM_RUN_ID", "SREGYM_SHARD_INDEX", "SREGYM_SLOT")]
    label = " ".join(p for p in parts if p)

    return label or uuid.uuid4().hex[:8]


def confirm_persisted(investigation_uuid: str) -> None:
    """Check that the diagnosis turn actually recorded something.

    Without this a misdirected session is invisible: brain-go's CompleteRCI
    swallows both a missing investigation and a state conflict as benign
    no-ops, so a session resolving to a different tenant than the API key
    would let every turn complete, the run exit 0, and nothing be written.

    Raises only when the investigation cannot be read at all, which is
    unambiguous. An empty report is logged rather than raised; see below.
    """
    if not BRAIN_ADMIN_API_URL or not BRAIN_SESSION:
        return

    base = BRAIN_ADMIN_API_URL.rstrip("/")
    resp = requests.get(
        f"{base}/api/v2/investigations/{investigation_uuid}",
        cookies={"session": BRAIN_SESSION},
        timeout=30,
    )

    if not resp.ok:
        raise RuntimeError(
            f"investigation {investigation_uuid} could not be read back: {resp.status_code} {resp.text[:200]}"
        )

    data = resp.json().get("data") or {}

    # rca_response, not analysis. The stored column is `analysis` on the triage
    # task row, but this route surfaces it as data.rca_response
    # (internal/api/investigations/v2_detail.go). Checking `analysis` here would
    # have failed on every run regardless of whether the write happened.
    #
    # Empty is ambiguous, so it does not raise. It means either the session and
    # the API key resolve to different tenants, so the report was written to an
    # account this investigation does not belong to, or the turn legitimately
    # produced no structured report, which completeInvestigation treats as a
    # no-op. Killing the problem would punish the second case for the first.
    # A misconfigured run prints this on every problem, which is what makes it
    # visible; a one-off does not cost the problem.
    if not data.get("rca_response"):
        logger.error(
            "NOT PERSISTED: investigation %s has no rca_response after the diagnosis turn "
            "(state=%s). Either BRAIN_SESSION and BRAIN_API_KEY resolve to different tenants, "
            "or the turn produced no structured report. If every problem logs this, it is the "
            "former and the run is recording nothing (NOFireAI/sregym#28).",
            investigation_uuid,
            data.get("state"),
        )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

# How the incident window has to be phrased, and why it is the last thing in
# every prompt.
#
# Given no bounds, brain-go's init_state stamps a flat one-hour lookback ending
# now. On this harness almost all of that hour is cluster creation, app deploy
# and observability startup, and the injected fault is a sliver at the end, so
# the agent diagnoses the loudest thing it can see and the judge scores that as
# a failure (sregym#52).
#
# brain-go will take a window off the message text instead. Four constraints,
# each read off its own regexes rather than assumed:
#
#   - "between <t> and <t>", because that is what its reFromTo matches.
#   - Last in the message, no trailing newline. reFromTo ends in `(.+)$` and is
#     not multiline, so a window with anything after it does not match. It then
#     falls through to a single-anchor parse that yields start=injection and
#     end=now, which at turn time is a window seconds wide. Worse than the
#     default hour, and silent.
#   - Minute precision and no trailing "Z". Its token pattern is
#     `\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2})?`, so seconds do not parse and a "Z"
#     stops both the gate and the anchor from matching at all.
#   - UTC, because brain-go builds the timestamp in the account's timezone and
#     the seeded benchmark account sets none, which resolves to UTC.
INCIDENT_WINDOW_FMT = "%Y-%m-%dT%H:%M"


def incident_window(incident_start: float | None) -> str:
    """The window sentence to append to a prompt, or "" when unknown.

    Empty rather than a guess: with no sentence brain-go keeps its own
    one-hour default, which is the behaviour before this existed. A wrong
    window would be worse than no window, because it would look deliberate.

    The window ends one agent budget after the fault, not "now". brain-go
    freezes a thread's window on the first turn, so an end at turn time would
    be seconds wide and everything the agent queried afterwards would fall
    outside it. The budget is what bounds the investigation, so it is the
    honest end.

    One consequence worth knowing: brain-go derives baseline_start as
    start - (end - start) * 3, so a 40 minute window puts the baseline two
    hours before the fault, which is before the cluster existed. The baseline
    is then empty rather than wrong. Tightening the window would buy a
    populated baseline at the cost of the incident window not covering the
    investigation, which is the worse trade.
    """
    if not incident_start:
        return ""

    budget = int(os.environ.get("AGENT_TIMEOUT_SECONDS", "1800") or 1800)
    injected = datetime.datetime.fromtimestamp(incident_start, datetime.UTC)

    # Round the window start UP, never down.
    #
    # Truncating to the minute is what run-20260902-2008 got wrong. The fault
    # landed at 20:35:38, the window was sent as 20:35, and all seven findings
    # the agent cited fell between 20:35:13 and 20:35:28: inside the window it
    # was given, and before the fault. It diagnosed otel-collector shutdown
    # churn and scored 0.0 on a problem that had scored 100 the run before.
    #
    # Those 38 seconds are the worst in the run to hand over. The app deploy
    # and the observability stack finish immediately before injection, so the
    # noisiest moment available is exactly the gap that rounding down opens.
    #
    # Rounding up costs up to 59 seconds of real post-fault data at the front
    # of a 30 to 40 minute window. That is the cheaper error by a wide margin:
    # these faults are configuration changes that persist and keep producing
    # symptoms, rather than one-off events whose only trace is at onset.
    start = injected.replace(second=0, microsecond=0)
    if start < injected:
        start += datetime.timedelta(minutes=1)

    end = start + datetime.timedelta(seconds=budget)

    # The exact instant goes in the prose and the rounded window goes last.
    # brain-go parses the window and needs minute precision; the model reading
    # the prose does not, and the true instant is strictly better for the tool
    # calls it makes itself. An ISO stamp before the word "between" cannot
    # disturb the parse, because reFromTo matches from "between" onward.
    return (
        f"The fault was introduced at {injected.strftime('%Y-%m-%dT%H:%M:%S')} UTC. "
        f"Everything before it is normal operation, including the cluster build and "
        f"the application deploy, so treat pre-fault logs and restarts as setup rather "
        f"than as the incident. Investigate between "
        f"{start.strftime(INCIDENT_WINDOW_FMT)} and {end.strftime(INCIDENT_WINDOW_FMT)}"
    )


def write_tool_calls(
    per_phase: dict[str, collections.Counter[str]],
    per_phase_by_node: dict[str, collections.Counter[str]] | None = None,
) -> None:
    """Record what the agent called, where the harness can read it back.

    The tool_start events already arrive on the SSE stream and were only
    logged. Recovering them meant parsing a column-formatted console log in
    which two thirds of tool names wrap onto a continuation line, so
    `grep -c "tool_start: get_logs"` undercounted by however many of that
    tool's calls happened to wrap. On run-20260903-0558, 193 of 286 wrapped
    (sregym#67).

    per_phase_by_node is the same events grouped by brain-go's own node
    attribution (brain-go#2687) instead of by tool name -- which underlying
    node actually made the call, for the RCI callers where several nodes are
    unioned behind one set of tools. Optional and defaulted for callers that
    predate this field.

    AGENT_LOGS_DIR is the run's artifact directory, exported by main.py's
    _artifact_environment around the agent launch, so this lands beside the
    attempt it belongs to and main.py reads it back from the same path.

    Best effort: a run's numbers are the product, and a bookkeeping failure
    must not cost one. Absent output reads as unmeasured rather than zero.
    """
    target = os.environ.get("AGENT_LOGS_DIR")
    if not target:
        logger.info("tool calls not recorded: AGENT_LOGS_DIR is unset")

        return

    per_phase_by_node = per_phase_by_node or {}
    payload = {
        "total": sum(sum(c.values()) for c in per_phase.values()),
        "by_phase": {phase: sum(counter.values()) for phase, counter in per_phase.items()},
        "by_tool": dict(sum(per_phase.values(), collections.Counter())),
        "by_phase_and_tool": {phase: dict(counter) for phase, counter in per_phase.items()},
        "by_node": dict(sum(per_phase_by_node.values(), collections.Counter())),
        "by_phase_and_node": {phase: dict(counter) for phase, counter in per_phase_by_node.items()},
    }
    try:
        path = Path(target) / "tool-calls.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        logger.info("tool calls: %s", payload["by_phase"])
    except OSError as exc:
        # OSError covers what this can actually hit: no such directory, a file
        # already at that path, no permission. The environment cannot deliver a
        # path the os layer rejects outright, since os.environ refuses to store
        # one.
        logger.warning("could not write tool calls (non-fatal): %s", exc)


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

Be specific and technical. State the root cause clearly in your final answer.

{window}"""

# The window is repeated here rather than relied on from the diagnosis turn.
# brain-go would preserve it (its hasWindow case keeps a window a later turn
# does not override), but that case sits below the explicit-time check, so any
# incidental time phrase in a later message resets it. Restating it costs a
# line and removes the dependency.
MITIGATION_PROMPT = """Your task now is to FIX the failure in this application by
applying changes to the cluster.

Application: {app_name}
Namespace: {namespace}

Apply the fix, then confirm the application has recovered.

{window}"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    if not BRAIN_API_KEY:
        logger.error("BRAIN_API_KEY is unset. scripts/benchmark/setup_brain_go.sh writes it.")

        return 1

    problem_id = resolve_problem_id()
    logger.info("brain-go driver starting: problem=%s brain=%s", problem_id, BRAIN_API_URL)
    # "none" means no driver-executed local tools. It does not mean no cluster
    # access: brain-go still has kubectl over MCP either way (see
    # DIAGNOSIS_LOCAL_TOOLS).
    logger.info("diagnosis local tools: %s", sorted(DIAGNOSIS_LOCAL_TOOLS) or "none")
    logger.info("mitigation local tools: %s", sorted(MITIGATION_LOCAL_TOOLS))

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
        "window": incident_window(app_info.get("incident_start")),
    }
    logger.info("incident window: %s", params["window"] or "not supplied by the conductor")

    # One thread for both phases, so the mitigation turn sees the diagnosis it
    # is acting on.
    #
    # An investigation-backed thread when we can open one, so brain-go persists
    # the report and the run's hypotheses survive it (#28). The self-made thread
    # is the fallback: it still runs, it just records nothing.
    # Opened before any turn, so a rejected credential costs the conductor wait
    # and nothing else: no LLM call, no tokens. It does not stop the sweep,
    # because main.py records a non-zero driver exit as an ordinary snapshot and
    # moves to the next problem. With a bad session that yields a full run of
    # zeros that read like scores. The log line below is what tells them apart;
    # making the harness abort needs a change in main.py, which is upstream code.
    opened = open_investigation(problem_id)
    thread_id = opened[0] if opened else f"cli:sregym-{problem_id}-{uuid.uuid4().hex[:8]}"
    logger.info("thread: %s", thread_id)

    # Per phase, because the mitigation turn's tool use is a different question
    # from the diagnosis turn's, and in run-20260903-0558 the balance inverted
    # between them: kubectl-heavy while fixing, log-heavy while diagnosing.
    tool_calls: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    # Same events, grouped by which RCI node actually made the call rather
    # than which tool (brain-go#2687) -- the two differ for a caller like the
    # hypothesis planner, which unions several nodes' tools behind one label.
    tool_calls_by_node: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)

    # try/finally so a run always records what it spent, including the two
    # early returns below: a failed diagnosis turn and a conductor that goes
    # straight to done. Those are exactly the attempts whose tool use is
    # worth seeing, so recording only the happy path would miss them.
    try:
        logger.info("=" * 30 + " DIAGNOSIS " + "=" * 30)
        try:
            diagnosis = run_turn(
                DIAGNOSIS_PROMPT.format(**params).rstrip(),
                thread_id,
                DIAGNOSIS_LOCAL_TOOLS,
                tool_calls["diagnosis"],
                tool_calls_by_node["diagnosis"],
            )
        except TurnFailed:
            logger.exception("diagnosis turn failed")
            submit_to_conductor("")

            return 1

        logger.info("diagnosis (%d chars): %s", len(diagnosis), diagnosis[:300])

        # Submit first. The check below is about whether the run is recording, not
        # about whether the answer is good, and an unsubmitted diagnosis scores zero
        # on both dimensions. An earlier version ran it first and would have thrown
        # away a perfectly good diagnosis whenever the turn produced no structured
        # report, which completeInvestigation treats as a no-op and which is
        # reachable through a recursion-limit exit or a decode failure.
        submit_to_conductor(diagnosis)

        if opened:
            confirm_persisted(opened[1])

        stage = wait_for_stage({"mitigation", "done"})
        if stage == "done":
            logger.info("conductor went straight to done after diagnosis")

            return 0

        logger.info("=" * 30 + " MITIGATION " + "=" * 29)
        try:
            mitigation = run_turn(
                MITIGATION_PROMPT.format(**params).rstrip(),
                thread_id,
                MITIGATION_LOCAL_TOOLS,
                tool_calls["mitigation"],
                tool_calls_by_node["mitigation"],
            )
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

    finally:
        write_tool_calls(tool_calls, tool_calls_by_node)


if __name__ == "__main__":
    sys.exit(main())
