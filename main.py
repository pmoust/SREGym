import argparse
import asyncio
import contextlib
import csv
import functools
import importlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import requests
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from clients.harness.problem_id import HARNESS_ARTIFACT_ID_ENV, HARNESS_PROBLEM_ID_ENV
from logger import console, init_logger
from scripts.benchmark import brain_graph
from sregym.agent_launcher import AgentLauncher
from sregym.agent_registry import get_agent, list_agents
from sregym.conductor.conductor import Conductor, ConductorConfig
from sregym.conductor.conductor_api import request_shutdown, run_api
from sregym.conductor.constants import StartProblemResult
from sregym.conductor.problem_sets import PROBLEM_SETS
from sregym.run_artifacts import ArtifactFinalizationError, RunArtifacts, stabilize_run_csv_headers
from sregym.service.container_runner import ContainerRunner, ExecInput
from sregym.traces import postprocess as trace_postprocess
from sregym.traces import store as trace_store

LAUNCHER = AgentLauncher()
logger = logging.getLogger(__name__)
_driver_results: list[dict] = []
_driver_base_dir: Path | None = None


def run_preflight_check(
    agent_name: str,
    container_runner: ContainerRunner | None = None,
    install_script: str | None = None,
) -> None:
    """Run the agent's pre-flight check inside the container."""

    # Agents that need pre-flight check
    agent_driver_modules: dict[str, str] = {
        "stratus": "clients.stratus.stratus_agent.driver.driver",
        "claudecode": "clients.claudecode.driver",
        "codex": "clients.codex.driver",
        "copilot": "clients.copilot.driver",
        "opencode": "clients.opencode.driver",
        "gemini": "clients.geminicli.driver",
    }

    module_path = agent_driver_modules.get(agent_name)
    if not module_path:
        return

    driver_mod = importlib.import_module(module_path)
    if not hasattr(driver_mod, "run_preflight"):
        return

    if container_runner is None:
        logger.warning(f"⚠️  No container runner — skipping pre-flight check for '{agent_name}'")
        return

    check_cmd = f"python3 -c 'from {module_path} import run_preflight; run_preflight()'"
    if install_script:
        check_cmd = f"/opt/sregym/install-scripts/{install_script} > /dev/null 2>&1 && {check_cmd}"

    logger.info(f"🔍 Running pre-flight check for '{agent_name}'...")
    result = container_runner.run_sync(ExecInput(command=check_cmd, label="preflight", timeout=180))
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip())
        logger.error(f"❌ Pre-flight check failed for '{agent_name}'")
        sys.exit(1)

    logger.info(f"✅ Pre-flight check passed for '{agent_name}'")


def run_judge_preflight_check() -> None:
    """Validate the judge model and credentials through its LiteLLM backend."""
    from llm_backend.init_backend import get_llm_backend_for_judge

    logger.info("🔍 Running pre-flight check for judge model...")
    try:
        get_llm_backend_for_judge().inference("Say ok.", system_prompt="Reply with exactly 'ok'.")
    except Exception as e:
        logger.error(f"❌ Judge pre-flight check failed: {e}")
        logger.error(
            "The judge uses LiteLLM. Ensure the model is LiteLLM-compatible or pass a compatible --judge-model."
        )
        sys.exit(1)

    logger.info("✅ Judge pre-flight check passed")


def get_current_datetime_formatted():
    now = datetime.now()
    formatted_datetime = now.strftime("%m%d_%H%M")
    return formatted_datetime


def _restore_env_var(name: str, previous_value: str | None) -> None:
    if previous_value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous_value


def _env_status(name: str) -> str:
    return "set" if os.environ.get(name) else "unset"


def _is_opencode_local_model(agent: str | None, model: str) -> bool:
    return agent == "opencode" and model.startswith("local/")


def _normalize_opencode_local_model_for_litellm(model: str) -> str:
    return f"openai/{model.split('/', 1)[1]}"


def _configure_model_environment(args) -> tuple[str, str]:
    agent_model = args.model
    raw_judge_model = args.judge_model or args.model
    reasoning_effort = getattr(args, "reasoning_effort", None)
    normalizes_opencode_local_judge = _is_opencode_local_model(args.agent, raw_judge_model)
    judge_model = (
        _normalize_opencode_local_model_for_litellm(raw_judge_model)
        if normalizes_opencode_local_judge
        else raw_judge_model
    )

    os.environ["AGENT_MODEL_ID"] = agent_model
    os.environ["JUDGE_MODEL_ID"] = judge_model
    if reasoning_effort:
        os.environ["AGENT_REASONING_EFFORT"] = reasoning_effort
    else:
        os.environ.pop("AGENT_REASONING_EFFORT", None)

    if not getattr(args, "judge_model", None) or normalizes_opencode_local_judge:
        if not os.environ.get("JUDGE_API_BASE") and os.environ.get("AGENT_API_BASE"):
            os.environ["JUDGE_API_BASE"] = os.environ["AGENT_API_BASE"]
        if not os.environ.get("JUDGE_API_KEY") and os.environ.get("AGENT_API_KEY"):
            os.environ["JUDGE_API_KEY"] = os.environ["AGENT_API_KEY"]

    if normalizes_opencode_local_judge:
        if not os.environ.get("JUDGE_API_BASE"):
            raise ValueError("AGENT_API_BASE or JUDGE_API_BASE is required to use an OpenCode local model as the judge")
        if not os.environ.get("JUDGE_API_KEY"):
            os.environ["JUDGE_API_KEY"] = "dummy"

    return agent_model, judge_model


_TOKEN_DELTA = Path(os.path.dirname(os.path.abspath(__file__))) / "scripts" / "benchmark" / "token_delta.py"

# How long an agent gets to finish writing after the harness decides it is done.
# The agent archives its Temporal histories on the way out, and that work is
# bounded by the driver's own per-export timeout.
AGENT_REAP_GRACE_SECONDS = 60

# Where an agent writes its own transcripts, outside the staging directory that
# finalization scans for the real problem id. Folded into the published tree by
# _publish_archive once finalization has run.
AGENT_ARCHIVE_DIR_ENV = "AGENT_ARCHIVE_DIR"


def _publish_archive(run, published_run_dir: Path) -> None:
    """Move the agent's transcripts into the published attempt directory.

    They are written outside the staging tree so finalization's scan for the
    real problem id never reads the agent's own prose. One model-authored slug
    matching the id would otherwise fail finalization and cost the attempt every
    artifact it produced, including the fault spec.

    Best effort: a transcript that cannot be moved is logged and the attempt
    keeps its results.
    """
    archive = getattr(run, "archive_dir", None)
    if archive is None or not archive.is_dir():
        return

    for path in sorted(archive.iterdir()):
        if not path.is_file():
            continue
        try:
            shutil.move(str(path), str(published_run_dir / path.name))
        except OSError as exc:
            logger.warning("could not publish %s: %s", path.name, exc)

    with contextlib.suppress(OSError):
        archive.rmdir()


# Lives in brain_graph.py because the conductor's post-fault wait needs the
# same component the fault spec records, and the conductor cannot import main.
_parse_structured_root_cause = brain_graph.parse_structured_root_cause


def _write_fault_spec(run_dir: Path, conductor, problem_id: str, attempt: int) -> None:
    """Record the injected fault's ground truth beside the attempt's artifacts.

    The field names are the ones the offline scorer unmarshals, so collecting a
    run's manifests is a concatenation of these files rather than a translation.
    The description and raw root cause ride along for debugging; the scorer
    ignores keys it does not know.

    Call this on the published directory, after finalize_and_publish returns.
    Two reasons it goes last rather than into the staging directory. The agent
    gets the staging directory as AGENT_LOGS_DIR, and this file names the faulty
    component, so it must not exist while the agent can read it. Finalization
    also rejects any staged artifact carrying the real problem id, which this
    file does carry; the published tree is where that id is the canonical one.

    The file is written for every published attempt, including one where nothing
    was injected. A missing file then means the attempt never published, and an
    empty component means the problem carried no parseable label; those are
    different bugs and the scorer reports them separately.
    """
    captured = getattr(conductor, "fault_spec", None) or {}
    root_cause = captured.get("root_cause")
    component, namespace, description = _parse_structured_root_cause(root_cause)

    injected_at = captured.get("fault_injected_at")
    if injected_at is not None:
        injected_at = datetime.fromtimestamp(injected_at, UTC).isoformat().replace("+00:00", "Z")

    spec = {
        "problem": captured.get("problem_id") or problem_id,
        "component": component,
        # The structured root cause is authoritative. The problem's own
        # namespace is the fallback, because a problem that names no namespace
        # leaves the scorer matching on the component alone.
        "namespace": namespace or (captured.get("namespace") or ""),
        "mechanism": captured.get("problem_id") or problem_id,
        "injected_at": injected_at,
        "attempt": attempt,
        "problem_class": captured.get("problem_class"),
        "description": description,
        "root_cause": root_cause,
    }

    path = run_dir / "fault-spec.json"
    try:
        path.write_text(json.dumps(spec, indent=2, sort_keys=True, default=str) + "\n")
    except OSError as exc:
        logger.warning("Could not write %s: %s", path, exc)


def _write_native_arm_evidence(run_dir: Path, conductor) -> None:
    """Record what made this attempt the native arm, beside its other artifacts.

    Two halves, gathered at the two moments each is knowable. The entity
    counts by type come from the conductor's pre-fault gate, which is the only
    point at which "the graph was populated before the fault" can be observed
    at all. The change events come from here, after the agent has run, because
    a change event on the mutated object cannot exist until the edge agent has
    published at least once since the injection.

    Written as JSON into the published directory, where archive_run.sh's
    existing *.json include already ships it, so no new plumbing carries it
    off the box.

    A no-op under the MCP arm, and non-fatal under either. The gate has
    already failed the attempt if the graph was empty; everything here is
    reporting, and a run's numbers must not be lost to a failed read of the
    evidence behind them.
    """
    evidence = dict(getattr(conductor, "native_arm_evidence", None) or {})
    if evidence.get("arm") != "native":
        return

    spec_path = run_dir / "fault-spec.json"
    try:
        spec = json.loads(spec_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        spec = {}
        evidence["fault_spec_error"] = f"{type(exc).__name__}: {exc}"

    cluster = evidence.get("cluster") or os.environ.get("KIND_CLUSTER_NAME", "")
    session = os.environ.get("BRAIN_SESSION", "")
    base = os.environ.get("BRAIN_ADMIN_API_URL", "").rstrip("/")

    if spec and cluster and session and base:
        try:
            evidence["change_events"] = brain_graph.change_events(
                base,
                session,
                cluster,
                spec.get("component", ""),
                spec.get("namespace", ""),
                brain_graph.parse_injected_at(spec.get("injected_at")),
            )
        except Exception as exc:  # noqa: BLE001 - evidence must never fail an attempt
            evidence["change_events"] = {"error": f"{type(exc).__name__}: {exc}"}

    try:
        (run_dir / "arm-native.json").write_text(json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n")
    except OSError as exc:
        logger.warning("Could not write %s: %s", run_dir / "arm-native.json", exc)


def _token_snapshot(out: Path) -> None:
    """Capture LLM counters. Silent on any failure, by design.

    A run's numbers are the product; its price tag is a nice-to-have. So every
    path here swallows: no scraper on this checkout, no metrics endpoint, a
    local run with no brain-go at all. The delta then reads as zero, and zero
    is distinguishable from a real count by the calls column beside it.
    """
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, str(_TOKEN_DELTA), "snapshot", str(out)],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - pricing must never fail a run
        console.log(f"⚠️  token snapshot failed (non-fatal): {type(exc).__name__}: {exc}")


@functools.cache
def _brain_account_id() -> str:
    """The brain-go account this slot spends against, or "" if unreadable.

    Every slot on a box shares one brain-go process whose LLM counters are
    process-wide, so a per-attempt delta has to be narrowed to the slot's own
    tenant or it also reports the other slots' spend (token_delta.py).
    seed_slot.sh creates that tenant per slot and exports the owner session
    that authenticates as it; GET /api/users/me answers with the account id
    behind the session, so the id needs no new bookkeeping of its own.

    The session's account is the API key's account too: both come from one
    `brainctl seed dev-stack` call, and clients/brain_go/driver.py's
    confirm_persisted logs NOT PERSISTED on every problem if they ever
    diverge.

    Cached because the tenant is seeded before the run starts and cannot
    change under it, while this sits on the per-attempt path.

    "" means "sum every account", which is right for a box running one slot
    and inflated for a box running several, so an unreadable id logs. The
    unset case is not an error: every agent other than brain_go runs with no
    brain-go tenant at all.
    """
    base = os.environ.get("BRAIN_ADMIN_API_URL", "").rstrip("/")
    session = os.environ.get("BRAIN_SESSION", "")

    if not base or not session:
        return ""

    try:
        resp = requests.get(f"{base}/api/users/me", cookies={"session": session}, timeout=30)
        if not resp.ok:
            console.log(f"⚠️  could not read the brain-go account id: GET /api/users/me returned {resp.status_code}")

            return ""

        account_id = (resp.json().get("user") or {}).get("account_id")
    except (requests.RequestException, ValueError, AttributeError) as exc:
        console.log(f"⚠️  could not read the brain-go account id: {type(exc).__name__}: {exc}")

        return ""

    if not account_id:
        console.log("⚠️  GET /api/users/me carried no account_id; per-attempt tokens will cover every slot on this box")

        return ""

    return str(account_id)


def _token_delta(before: Path, after: Path, account_id: str = "") -> dict[str, int]:
    """What the attempt spent, or an empty dict if it cannot be known."""
    try:
        done = subprocess.run(
            [sys.executable, str(_TOKEN_DELTA), "delta", str(before), str(after), *_account_args(account_id)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if done.returncode == 0 and done.stdout.strip():
            return json.loads(done.stdout)
    except Exception as exc:  # noqa: BLE001 - pricing must never fail a run
        console.log(f"⚠️  token delta failed (non-fatal): {type(exc).__name__}: {exc}")

    return {}


def _account_args(account_id: str) -> list[str]:
    """The --account flag for token_delta.py, or nothing when the id is unknown.

    Omitted rather than passed empty: token_delta.py reads an empty account id
    as "every account", and an explicit `--account ""` would rely on that
    rather than state it.
    """
    return ["--account", account_id] if account_id else []


def _token_delta_by_node(before: Path, after: Path, account_id: str = "") -> dict[str, dict[str, int]]:
    """The same attempt's spend, broken out by brain-go's own RCI node
    attribution (brain-go#2687), or an empty dict if it cannot be known.

    Kept separate from the row entirely (see _write_token_usage_by_node):
    the node set is bounded, but multiplying it against five token/call
    columns is still the kind of sparse, per-attempt-varying header #39
    already fixed once.
    """
    try:
        done = subprocess.run(
            [sys.executable, str(_TOKEN_DELTA), "delta-by-node", str(before), str(after), *_account_args(account_id)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if done.returncode == 0 and done.stdout.strip():
            return json.loads(done.stdout)
    except Exception as exc:  # noqa: BLE001 - pricing must never fail a run
        console.log(f"⚠️  token delta by node failed (non-fatal): {type(exc).__name__}: {exc}")

    return {}


def _write_token_usage_by_node(active_dir: Path, by_node: dict[str, dict[str, int]]) -> None:
    """Record the per-attempt node breakdown beside the attempt it belongs to,
    the same way write_tool_calls (brain_go/driver.py) already does for tool
    calls. Best effort: a bookkeeping failure must not cost a run its result.
    """
    if not by_node:
        return

    try:
        (active_dir / "token-usage-by-node.json").write_text(json.dumps(by_node, indent=2, sort_keys=True))
    except OSError as exc:
        console.log(f"⚠️  could not write token usage by node (non-fatal): {type(exc).__name__}: {exc}")


def _record_token_columns(snapshot: dict[str, object], before: Path, after: Path, active_dir: Path) -> None:
    """Put one attempt's token columns on its row and its node breakdown on
    disk, both narrowed to this slot's own brain-go tenant.

    The counters behind the delta belong to one brain-go process, shared by
    every slot on the box, so the account id is what keeps the neighbours'
    spend out of this attempt's row.

    tokens_account_id records which account did the narrowing, and is empty
    when none could be read. That case is not an error and must not cost a run
    its result, so it degrades to summing every account, which is right on a
    one-slot box and inflated on a shared one. Without the column the
    degradation is invisible in the archive: the warning goes to the slot log,
    the lookup is cached for the life of the process, so one failed lookup at
    startup silently reverts that slot's whole run while its neighbours stay
    narrowed. The column lets a reader of the archived CSV tell an attributed
    row from an inflated one.

    Extracted from the row construction so it can be tested. The code around
    it drives a live Conductor, which this suite has no cluster for.
    """
    account_id = _brain_account_id()
    snapshot["tokens_account_id"] = account_id

    for name, count in _token_delta(before, after, account_id).items():
        # llm_calls rather than calls, so a column name says which calls it
        # counts. Tool calls are a different number.
        snapshot["llm_calls" if name == "calls" else f"tokens_{name}"] = count

    _write_token_usage_by_node(active_dir, _token_delta_by_node(before, after, account_id))


def _tool_calls(active_dir: Path) -> dict[str, int]:
    """The agent's tool calls, if it recorded them.

    Written by the agent driver into AGENT_LOGS_DIR, which is this same
    directory. Flattened onto the row as tool_calls (total) and
    tool_calls_<phase>, so a run can say whether a slow attempt was thinking or
    tool-thrashing without anyone parsing a console log (sregym#67).

    The per-tool breakdown stays in the JSON rather than becoming columns:
    the tool set differs per agent and per stage, so promoting it would grow an
    unbounded and mostly-empty header, and #39 is already about header shapes.

    Absent file means the agent does not record them, which is every agent but
    brain_go today. Reads as unmeasured, not as zero.
    """
    path = active_dir / "tool-calls.json"
    if not path.exists():
        return {}

    try:
        payload = json.loads(path.read_text())
        flat = {"tool_calls": int(payload.get("total", 0))}
        for phase, count in (payload.get("by_phase") or {}).items():
            flat[f"tool_calls_{phase}"] = int(count)

        return flat
    except (OSError, ValueError, TypeError) as exc:
        console.log(f"⚠️  could not read tool calls (non-fatal): {type(exc).__name__}: {exc}")

        return {}


@contextlib.contextmanager
def _artifact_environment(run: RunArtifacts):
    previous = {
        "AGENT_LOGS_DIR": os.environ.get("AGENT_LOGS_DIR"),
        AGENT_ARCHIVE_DIR_ENV: os.environ.get(AGENT_ARCHIVE_DIR_ENV),
        HARNESS_ARTIFACT_ID_ENV: os.environ.get(HARNESS_ARTIFACT_ID_ENV),
        HARNESS_PROBLEM_ID_ENV: os.environ.get(HARNESS_PROBLEM_ID_ENV),
    }
    os.environ["AGENT_LOGS_DIR"] = str(run.active_dir.resolve())
    os.environ[AGENT_ARCHIVE_DIR_ENV] = str(run.archive_dir.resolve())
    os.environ[HARNESS_ARTIFACT_ID_ENV] = run.artifact_id
    os.environ.pop(HARNESS_PROBLEM_ID_ENV, None)
    try:
        yield
    finally:
        for name, value in previous.items():
            _restore_env_var(name, value)


def _deploy_exhausted_problems(agent_results: list[dict]) -> set[str]:
    """Problem ids with a deploy_failed marker row: every deploy retry was
    spent and no attempt for that problem was ever scored.

    driver_loop() appends the marker as a bool; a row read back in through
    --resume is a string, since it round-tripped through a CSV first. Both
    are checked so a resumed run does not lose the signal a fresh one has.

    A row with no problem_id at all is skipped rather than folded into the
    result as the literal string "None": not reachable today (every
    snapshot dict and every CSV-resumed row carries the column), but a
    caller matching against this set should never see a value that was
    never a real problem id.
    """
    return {
        str(row["problem_id"])
        for row in agent_results
        if row.get("problem_id") is not None and row.get("deploy_failed") in (True, "True")
    }


def driver_loop(
    conductor: Conductor,
    problem_selection: Sequence[str] | None = None,
    agent_to_run: str | None = None,
    use_external_harness: bool = False,
    n_attempts: int = 1,
    agent_timeout: int = 1800,
    resume_csv: str | None = None,
):
    """
    Deploy each problem and wait for HTTP grading via POST /submit.
    Returns a list of flattened dicts with results per problem.

    Args:
        conductor: The Conductor instance
        problem_selection: Optional ordered problem IDs to run. If omitted, use the registry's default selection.
        agent_to_run: Agent name to run (required unless use_external_harness is True).
        use_external_harness: If True, inject fault and exit without running evaluation logic.
        n_attempts: Number of end-to-end attempts to run each problem.
        resume_csv: Path to a previous results CSV to resume from (skip completed problems).
    """

    async def driver():
        base_dir = Path("results") / get_current_datetime_formatted()
        base_dir.mkdir(parents=True, exist_ok=True)
        global _driver_base_dir
        _driver_base_dir = base_dir
        # give the API a moment to bind
        await asyncio.sleep(1)

        # Verify agent exists in registry (skip if using external harness)
        if not use_external_harness:
            available_agents = list_agents(path=Path(os.path.dirname(os.path.abspath(__file__))) / "agents.yaml").keys()
            if agent_to_run not in available_agents:
                console.log(f"⚠️ Agent '{agent_to_run}' not found in registry. Available agents: {available_agents}")
                sys.exit(1)

            console.log(f"Starting agent now: {agent_to_run}")
            conductor.register_agent(agent_to_run)

            # Start K8s API proxy to hide chaos engineering namespaces from the agent
            console.log("🔒 Starting Kubernetes API proxy to hide chaos namespaces...")
            conductor.start_k8s_proxy()
            LAUNCHER.set_agent_kubeconfig(conductor.get_agent_kubeconfig_path())

        all_results_for_agent = []

        # Get all problem IDs and apply an explicit CLI selection when supplied.
        problem_ids = conductor.problems.get_problem_ids()
        all_problem_ids = conductor.problems.get_problem_ids(all=True)
        if problem_selection is not None:
            unknown_problem_ids = set(problem_selection) - set(all_problem_ids)
            if unknown_problem_ids:
                console.log(
                    f"⚠️  Problems not found in registry: {sorted(unknown_problem_ids)}. "
                    f"Available problems: {all_problem_ids}"
                )
                sys.exit(1)
            problem_ids = list(problem_selection)
            if len(problem_ids) == 1:
                console.log(f"🎯 Running single problem: {problem_ids[0]}")
            else:
                console.log(f"🎯 Running selected benchmark suite: {len(problem_ids)} problems")

        # sanity check: are there any specified problem ids that do not exist in the registry?
        unknown_problem_ids = set(problem_ids) - set(all_problem_ids)
        if unknown_problem_ids:
            console.log(
                f"⚠️  These problem ids do not exist in the registry and they will be skipped: {unknown_problem_ids}"
            )
        for unknown_problem_id in unknown_problem_ids:
            problem_ids.remove(unknown_problem_id)

        # Resume support: load completed problems from previous CSV and pre-seed results
        from collections import Counter

        completed_problems: set[str] = set()
        attempt_counts: Counter[str] = Counter()
        if resume_csv:
            try:
                with open(resume_csv, newline="") as f:
                    reader = csv.DictReader(f)
                    resume_rows = list(reader)
                # Group by problem_id and count attempts
                attempt_counts = Counter(r["problem_id"] for r in resume_rows)
                completed_problems = {pid for pid, count in attempt_counts.items() if count >= n_attempts}

                for row in resume_rows:
                    all_results_for_agent.append(row)
                console.log(
                    f"📋 Resuming from {resume_csv}: {len(completed_problems)} problems already done, skipping them"
                )
            except Exception as e:
                console.log(f"⚠️  Failed to load resume CSV: {e}")

        # Bar tracks attempts (problems × n_attempts), not problems, so the
        # 0/N total reflects total work even when n_attempts > 1.
        already_done = sum(min(attempt_counts.get(p, 0), n_attempts) for p in problem_ids)
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
        )
        task_id = progress.add_task(
            f"[cyan]Benchmarking {agent_to_run or 'agent'}",
            total=len(problem_ids) * n_attempts,
            completed=already_done,
        )
        progress.start()

        for pid in problem_ids:
            if pid in completed_problems:
                console.log(f"⏭️  Skipping already-completed problem: {pid}")
                progress.advance(task_id, n_attempts)
                continue

            conductor.problem_id = pid

            # Keep a record of results for this problem in a temp file in case an attempt fails
            tmp_path = f"_running_{pid}_{agent_to_run}_results.csv"

            for attempt in range(1, n_attempts + 1):
                progress.update(
                    task_id,
                    description=f"[cyan]Benchmarking {agent_to_run or 'agent'} — {pid} (attempt {attempt}/{n_attempts})",
                )
                console.log(f"\n🔍 Starting problem: {pid} (Attempt {attempt} of {n_attempts})")

                # Retry start_problem up to 3 times to handle transient deploy failures
                max_deploy_retries = 3
                result = None
                # How many deploys this attempt actually cost. Recorded on the
                # result row because a retry is expensive and otherwise
                # invisible: start_problem waits 600s on pod readiness before it
                # raises, and the retry then tears down and redeploys the whole
                # problem, so one retry is 35 to 40 minutes of a run's wall
                # clock. run-20260902-0606 spent 12 of them and scored the same
                # as a run that spent none (#53).
                deploy_attempts = 0
                for deploy_attempt in range(1, max_deploy_retries + 1):
                    deploy_attempts = deploy_attempt
                    try:
                        result = await conductor.start_problem()
                        break  # Success — exit retry loop
                    except Exception as e:
                        console.log(
                            f"❌ start_problem failed for '{pid}' "
                            f"(deploy attempt {deploy_attempt}/{max_deploy_retries}): {e}"
                        )
                        if deploy_attempt < max_deploy_retries:
                            console.log("🧹 Cleaning up before retry...")
                            try:
                                conductor._finish_problem()
                            except Exception as cleanup_err:
                                console.log(f"⚠️  Cleanup error (non-fatal): {cleanup_err}")
                            console.log(f"🔄 Retrying start_problem for '{pid}'...")
                        else:
                            console.log(
                                f"⛔ All {max_deploy_retries} deploy attempts failed for '{pid}', skipping this attempt"
                            )
                            result = None

                if result is None:
                    # The inner retry loop already logged the failure. Don't crash the
                    # entire driver — record the failure, clean up cluster state, and
                    # move on to the next problem so the benchmark can keep making progress.
                    try:
                        conductor._finish_problem()
                    except Exception as cleanup_err:
                        console.log(f"⚠️  Cleanup after exhausted deploy retries failed (non-fatal): {cleanup_err}")
                    snapshot = {
                        "problem_id": pid,
                        "attempt": attempt,
                        "deploy_failed": True,
                        "deploy_attempts": deploy_attempts,
                    }
                    all_results_for_agent.append(snapshot)
                    fieldnames = sorted({key for row in all_results_for_agent for key in row})
                    with open(tmp_path, "w", newline="") as csvfile:
                        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                        writer.writeheader()
                        writer.writerows(all_results_for_agent)
                    console.log(f"⏭️  Skipping remaining attempts for '{pid}' and moving to next problem")
                    # Account for this attempt + remaining skipped attempts on the bar.
                    progress.advance(task_id, n_attempts - attempt + 1)
                    break

                if result == StartProblemResult.SKIPPED_KHAOS_REQUIRED:
                    console.log(f"⏭️  Skipping problem '{pid}': requires Khaos but running on emulated cluster")
                    progress.advance(task_id, n_attempts - attempt + 1)
                    break  # Skip to next problem

                # If using external harness, fault is injected - exit now
                if use_external_harness:
                    console.log(f"✅ Fault injected for problem '{pid}'. Exiting for external harness.")
                    progress.stop()
                    return []

                assert agent_to_run is not None

                run = RunArtifacts.create(
                    staging_root=Path(".runtime"),
                    results_root=base_dir,
                    problem_id=pid,
                    agent=agent_to_run,
                    attempt=attempt,
                )
                agent_proc = None

                # Bracket the attempt with a counter scrape, so its token spend
                # is a delta rather than a thread_id join. brain-go's counters
                # are labelled by thread, and since #28 the driver uses the
                # investigation's thread (cli:inv-<uuid>), which carries no
                # problem id, so a single scrape cannot attribute spend to a
                # problem. Best effort throughout: pricing a run must never be
                # able to fail one (sregym#67).
                tokens_before = run.active_dir / "tokens-before.prom"
                _token_snapshot(tokens_before)

                with _artifact_environment(run):
                    reg = get_agent(
                        agent_to_run,
                        path=Path(os.path.dirname(os.path.abspath(__file__))) / "agents.yaml",
                    )
                    if reg:
                        agent_proc = await LAUNCHER.ensure_started(reg)

                timed_out = False
                agent_start_time = time.time()
                while conductor.submission_stage != "done":
                    if time.time() - agent_start_time > agent_timeout:
                        timed_out = True
                        console.log(f"⏰ Agent timeout ({agent_timeout}s) exceeded, killing agent")
                        # The normal path waits 60s for the agent before reaping
                        # it, and the agent's own archive step is sized against
                        # that. This path had cleanup_agent's 5s default, so the
                        # attempt most worth reading afterwards was the one that
                        # archived nothing. The agent still dies here; it just
                        # gets the same room to finish writing.
                        LAUNCHER.cleanup_agent(agent_to_run, timeout=AGENT_REAP_GRACE_SECONDS)
                        conductor.results["timed_out"] = True
                        conductor.results["agent_timeout_seconds"] = agent_timeout
                        console.log("🧹 Running conductor cleanup after agent timeout...")
                        conductor._finish_problem()
                        break

                    tracked_proc = LAUNCHER._procs.get(agent_to_run) or agent_proc
                    if tracked_proc:
                        tracked_proc.proc.poll()
                        if tracked_proc.proc.returncode is not None:
                            agent_proc = tracked_proc
                            console.log(f"⚠️  Agent process exited with return code {tracked_proc.proc.returncode}")
                            if conductor._submit_future is not None and not conductor._submit_future.done():
                                console.log("⏳ Waiting for conductor evaluation to complete...")
                                try:
                                    await asyncio.wait_for(
                                        asyncio.wrap_future(conductor._submit_future),
                                        timeout=300,
                                    )
                                except TimeoutError:
                                    console.log("⚠️  Conductor evaluation did not finish within 300s")
                                except Exception as e:
                                    console.log(f"⚠️  Conductor evaluation raised: {e}")
                            console.log("🧹 Running conductor cleanup after agent exit...")
                            conductor._finish_problem()
                            break
                    await asyncio.sleep(1)

                console.log(f"✅ Completed {pid}: results={conductor.results}", markup=False)
                # Wait for agent process to complete naturally before cleanup
                # This allows the agent to finish saving trajectories and other cleanup tasks
                if not use_external_harness:
                    agent_proc = LAUNCHER._procs.get(agent_to_run) or agent_proc
                    if agent_proc and not timed_out:
                        console.log("⏳ Waiting for agent process to complete...")
                        timeout = AGENT_REAP_GRACE_SECONDS
                        elapsed = 0
                        while elapsed < timeout:
                            agent_proc.proc.poll()
                            if agent_proc.proc.returncode is not None:
                                console.log(f"✅ Agent process completed with return code {agent_proc.proc.returncode}")
                                break
                            await asyncio.sleep(1)
                            elapsed += 1
                        else:
                            console.log(f"⚠️  Agent process did not complete within {timeout}s, will force cleanup")

                    # Publication must happen only after cleanup_agent has reaped
                    # the tracked docker client or shell process tree.
                    LAUNCHER.cleanup_agent(agent_to_run)
                    console.log(f"🧹 Cleaned up agent process for {agent_to_run}")

                snapshot = {
                    "problem_id": pid,
                    "attempt": attempt,
                    "deploy_attempts": deploy_attempts,
                }
                for name, count in _tool_calls(run.active_dir).items():
                    snapshot[name] = count
                tokens_after = run.active_dir / "tokens-after.prom"
                _token_snapshot(tokens_after)
                _record_token_columns(snapshot, tokens_before, tokens_after, run.active_dir)
                for stage, outcome in conductor.results.items():
                    if isinstance(outcome, dict):
                        for k, v in outcome.items():
                            snapshot[f"{stage}.{k}"] = v
                    else:
                        snapshot[stage] = outcome

                fieldnames = sorted({key for row in [*all_results_for_agent, snapshot] for key in row})
                ownership_image = (
                    LAUNCHER._container_runner.config.image
                    if LAUNCHER._container_runner is not None
                    else "sregym-agent-base:latest"
                )
                try:
                    published_run_dir = run.finalize_and_publish(
                        snapshot=snapshot,
                        fieldnames=fieldnames,
                        ownership_image=ownership_image,
                    )
                except ArtifactFinalizationError as exc:
                    snapshot["artifact_finalization_failed"] = True
                    snapshot["artifact_staging_path"] = str(run.active_dir)
                    logger.error(
                        "Artifact finalization failed for %s attempt %s; staging retained at %s: %s",
                        pid,
                        attempt,
                        run.active_dir,
                        exc,
                    )
                    published_run_dir = None

                if published_run_dir is not None:
                    _write_fault_spec(published_run_dir, conductor, pid, attempt)
                    # After the fault spec, which names the component this
                    # reads change events for.
                    _write_native_arm_evidence(published_run_dir, conductor)
                    _publish_archive(run, published_run_dir)

                all_results_for_agent.append(snapshot)
                fieldnames = sorted({key for row in all_results_for_agent for key in row})
                with open(tmp_path, "w", newline="") as csvfile:
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(all_results_for_agent)

                if published_run_dir is not None:
                    logger.info(
                        f"⏳ Attempt {attempt} of {n_attempts} for problem {pid} complete - "
                        f"Intermediate results written to {tmp_path}; artifacts published to {published_run_dir}"
                    )
                    # Normalize the agent's raw logs into an ATIF trajectory.json
                    # right after publication. Non-fatal: a conversion failure or
                    # an unsupported tool must never break the run.
                    trajectory_path = trace_postprocess.write_trajectory(published_run_dir)
                    if trajectory_path is not None:
                        logger.info(f"📝 ATIF trajectory written to {trajectory_path}")
                        try:
                            trace_store.ingest_trajectory_file(
                                trajectory_path,
                                base_dir.parent / trace_store.DEFAULT_DB_PATH,  # results/traces.db
                            )
                        except Exception as exc:  # defensive: never abort a run
                            logger.warning(f"⚠️ ATIF trajectory DB ingest failed: {exc}")
                    else:
                        logger.warning(f"⚠️ ATIF trajectory conversion skipped for {published_run_dir}")

                if attempt == n_attempts:
                    final_csv_path = base_dir / agent_to_run / pid / f"{pid}_{agent_to_run}_results.csv"
                    final_csv_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(tmp_path, final_csv_path)
                    logger.info(
                        f"✅ Problem {pid} for agent {agent_to_run} complete! Results written to {final_csv_path}"
                    )

                progress.advance(task_id)

        progress.stop()

        # Stop K8s API proxy when all problems are done
        if not use_external_harness:
            console.log("🔓 Stopping Kubernetes API proxy...")
            conductor.stop_k8s_proxy()

        # Each per-attempt CSV was published against whatever fieldnames the
        # loop above had accumulated so far, so a file from early in this run
        # can be missing a column a later attempt's oracle introduced. Now
        # that every attempt is in, reconcile them all to one header (#39).
        try:
            header_stabilization = stabilize_run_csv_headers(base_dir)
            if header_stabilization.rewritten:
                console.log(
                    f"🧹 Normalized headers on {len(header_stabilization.rewritten)} per-attempt result file(s)"
                )
            if header_stabilization.overflow_dropped:
                logger.warning(
                    f"⚠️ {len(header_stabilization.overflow_dropped)} result file(s) had a row with more "
                    "values than its own header -- something upstream wrote a malformed line. The "
                    f"unlabeled overflow was dropped, not attributed to any column: "
                    f"{header_stabilization.overflow_dropped}"
                )
        except (OSError, csv.Error) as exc:
            console.log(f"⚠️  Result CSV header normalization failed (non-fatal): {exc}")

        return [{agent_to_run: all_results_for_agent}]

    return asyncio.run(driver())


def _run_driver_and_shutdown(
    conductor: Conductor,
    problem_selection: Sequence[str] | None = None,
    agent_to_run: str | None = None,
    use_external_harness: bool = False,
    n_attempts: int = 1,
    agent_timeout: int = 1800,
    resume_csv: str | None = None,
):
    """Run the benchmark driver, stash results, then tell the API to exit."""
    try:
        results = driver_loop(
            conductor,
            problem_selection=problem_selection,
            agent_to_run=agent_to_run,
            use_external_harness=use_external_harness,
            n_attempts=n_attempts,
            agent_timeout=agent_timeout,
            resume_csv=resume_csv,
        )
        global _driver_results
        _driver_results = results
    except Exception:
        logger.exception("Driver thread crashed")
    finally:
        LAUNCHER.cleanup_all()
        request_shutdown()


def main(args):
    # set up the logger
    init_logger()

    agent_model, judge_model = _configure_model_environment(args)

    if args.noise:
        logger.info("Noise injection enabled.")
    # Defaults, not overrides: an already-exported value wins, so several runs
    # can share a host by each exporting its own ports. MCP_SERVER_URL is derived
    # from the resolved port rather than defaulted independently, since
    # overriding only the port would otherwise leave the URL pointing at 9954.
    os.environ.setdefault("API_HOSTNAME", "0.0.0.0")
    os.environ.setdefault("API_PORT", "8000")
    os.environ.setdefault("MCP_SERVER_PORT", "9954")
    os.environ.setdefault("MCP_SERVER_URL", f"http://127.0.0.1:{os.environ['MCP_SERVER_PORT']}")

    logger.info(
        f"🔧 Config — agent: {args.agent}, agent_model: {agent_model}, judge_model: {judge_model}, "
        f"reasoning_effort: {getattr(args, 'reasoning_effort', None) or 'agent default'}, "
        f"agent_api_base: {_env_status('AGENT_API_BASE')}, judge_api_base: {_env_status('JUDGE_API_BASE')}"
    )

    if not args.use_external_harness and os.getenv("SREGYM_SKIP_JUDGE_PREFLIGHT") != "1":
        run_judge_preflight_check()

    # Only build/check agent container image if the agent requires it
    agent_reg = (
        get_agent(args.agent, path=Path(os.path.dirname(os.path.abspath(__file__))) / "agents.yaml")
        if args.agent
        else None
    )
    if not agent_reg or agent_reg.container_isolation:
        LAUNCHER.enable_container_isolation(force_build=args.force_build)

    # Pre-flight check — makes a real (minimal) API call inside the agent
    # container to validate model and credentials in one shot.
    run_preflight_check(
        args.agent,
        container_runner=LAUNCHER._container_runner,
        install_script=agent_reg.install_script if agent_reg else None,
    )

    conductor_config = ConductorConfig(deploy_loki=not args.use_external_harness, enable_noise=args.noise)
    conductor = Conductor(config=conductor_config)

    suite = getattr(args, "suite", None)
    problem_selection = (args.problem,) if args.problem else PROBLEM_SETS.get(suite)
    if suite:
        logger.info(f"Running {suite}: {len(problem_selection)} problems")

    # Start the driver in the background; it will call request_shutdown() when finished
    driver_thread = threading.Thread(
        target=_run_driver_and_shutdown,
        args=(
            conductor,
            problem_selection,
            args.agent,
            args.use_external_harness,
            args.n_attempts,
            args.agent_timeout,
            args.resume,
        ),
        name="driver",
        daemon=True,
    )
    driver_thread.start()

    # Start the Conductor HTTP API in the MAIN thread (blocking)
    try:
        run_api(conductor)
    except KeyboardInterrupt:
        # If interrupted, still try to shut down cleanly
        LAUNCHER.cleanup_all()
        request_shutdown()
    finally:
        # Stop any remaining agent containers/processes
        LAUNCHER.cleanup_all()

        # Stop noise manager if it was enabled
        if args.noise:
            try:
                from sregym.generators.noise.manager import get_noise_manager

                logger.info("Stopping noise manager...")
                get_noise_manager().stop()
            except Exception as e:
                logger.error(f"⚠️ Error stopping noise manager: {e}")

        # Give driver a moment to finish setting results
        driver_thread.join(timeout=5)

    # When API shuts down, collect results from driver
    results = _driver_results

    deploy_exhausted_problems: set[str] = set()

    if results:
        aggregated = {}
        for entry in results:
            for agent_name, agent_rows in entry.items():
                aggregated.setdefault(agent_name, []).extend(agent_rows)

        for agent_name, agent_results in aggregated.items():
            fieldnames = sorted({key for row in agent_results for key in row})
            out_dir = _driver_base_dir if _driver_base_dir else Path("results")
            csv_path = out_dir / f"{agent_name}_ALL_results.csv"
            with open(csv_path, "w", newline="") as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(agent_results)
            logger.info(f"✅ Benchmark complete! Results for {agent_name} written to {csv_path}")

            deploy_exhausted_problems.update(_deploy_exhausted_problems(agent_results))
    else:
        logger.warning("⚠️ No results to write.")

    if deploy_exhausted_problems:
        logger.error(
            "❌ Deploy retries exhausted for "
            f"{len(deploy_exhausted_problems)} problem(s), no attempt recorded: "
            f"{sorted(deploy_exhausted_problems)}"
        )

    if __name__ == "__main__":
        # separate run, use exit. Non-zero when a problem exhausted its deploy
        # retries: that problem produced no attempt at all, which is not a
        # clean run, and run_shard.sh's `|| echo` guard only fires on this.
        sys.exit(1 if deploy_exhausted_problems else 0)
    else:
        # function call run, return results
        return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run SREGym benchmark suite")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--problem",
        type=str,
        default=None,
        help="Run only a specific problem by its ID (e.g., 'target_port')",
    )
    selection.add_argument(
        "--suite",
        choices=tuple(PROBLEM_SETS),
        default=None,
        help="Run a named problem set (e.g., 'sregym-lite')",
    )
    parser.add_argument(
        "--agent",
        type=str,
        default="stratus",
        help="Agent to run (default: stratus)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-5",
        help="LiteLLM model string (e.g. anthropic/claude-sonnet-4-6-20250627, gpt-5, gemini/gemini-2.5-pro)",
    )
    parser.add_argument(
        "--judge-model",
        type=str,
        default=None,
        help="Model for the LLM-as-a-judge evaluator (defaults to --model if not set)",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
        default=None,
        help="Reasoning effort for Codex, Copilot, OpenCode, and Claude Code (uses the agent default when omitted)",
    )
    parser.add_argument(
        "--use-external-harness", action="store_true", help="For use in external harnesses, deploy the fault and exit."
    )
    parser.add_argument(
        "--noise",
        action="store_true",
        help="Enable transient noise injection via Chaos Mesh during problem runs",
    )
    parser.add_argument(
        "--n-attempts",
        type=int,
        default=1,
        help="Number of attempts to run each problem (default: 1)",
    )
    parser.add_argument(
        "--force-build",
        action="store_true",
        help="Force rebuild the agent Docker image even if it already exists (use after updating dependencies or build scripts)",
    )
    parser.add_argument(
        "--agent-timeout",
        type=int,
        default=1800,
        help="Agent timeout in seconds after deployment (default: 1800)",
    )
    parser.add_argument(
        "--resume",
        type=str,
        default=None,
        help="Resume from a previous results CSV file. Problems already in the CSV will be skipped.",
    )
    args = parser.parse_args()

    if args.n_attempts is not None and args.n_attempts < 1:
        parser.error("--n-attempts must be a positive integer")
    if args.use_external_harness and args.suite:
        parser.error("--use-external-harness cannot be used with --suite; use --problem instead")

    main(args)
