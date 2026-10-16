"""main.py's exit code when a deploy exhausted every retry (#51).

Before this, main.py exited 0 unconditionally, so run_shard.sh's `|| echo ...
failed, continuing` guard never fired for the one failure mode it exists to
catch: a problem that lost every attempt to a deploy that never came up. The
marker row driver_loop() wrote for it (deploy_failed) was real and archived,
but nothing between main.py's own exit and a person reading score_run.py's
output ever noticed it.

Only _deploy_exhausted_problems() is tested directly. The exit-code wiring
around it (main(), lines ~803-820) drives a live Conductor and an asyncio
API server, which this suite has no cluster to run against; the helper is
what carries the actual decision, extracted so it does not need one.
"""

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "main_under_test",
    Path(__file__).resolve().parents[2] / "main.py",
)
main_mod = importlib.util.module_from_spec(_SPEC)
sys.modules["main_under_test"] = main_mod
_SPEC.loader.exec_module(main_mod)


def test_a_clean_agent_result_set_has_no_exhausted_problem():
    rows = [{"problem_id": "a", "attempt": "1", "deploy_failed": ""}]
    assert main_mod._deploy_exhausted_problems(rows) == set()


def test_a_fresh_deploy_failed_marker_is_a_python_bool():
    """driver_loop() appends the marker as a real bool, not yet round-tripped
    through a CSV (see the snapshot dict it builds on exhaustion)."""
    rows = [{"problem_id": "a", "attempt": "1", "deploy_failed": True}]
    assert main_mod._deploy_exhausted_problems(rows) == {"a"}


def test_a_resumed_deploy_failed_marker_is_a_string():
    """A row read back in through --resume came from a CSV first, so the
    marker round-trips as the string "True" rather than a bool."""
    rows = [{"problem_id": "a", "attempt": "1", "deploy_failed": "True"}]
    assert main_mod._deploy_exhausted_problems(rows) == {"a"}


def test_multiple_exhausted_problems_are_all_named():
    rows = [
        {"problem_id": "a", "attempt": "1", "deploy_failed": "True"},
        {"problem_id": "b", "attempt": "1", "deploy_failed": ""},
        {"problem_id": "c", "attempt": "1", "deploy_failed": True},
    ]
    assert main_mod._deploy_exhausted_problems(rows) == {"a", "c"}


def test_the_literal_string_false_is_not_mistaken_for_exhaustion():
    """A completed attempt can carry this column present but unset; it must
    not read as a deploy failure."""
    rows = [{"problem_id": "a", "attempt": "1", "deploy_failed": "False"}]
    assert main_mod._deploy_exhausted_problems(rows) == set()


def test_no_rows_means_no_exhausted_problem():
    assert main_mod._deploy_exhausted_problems([]) == set()


# --- _deploy_failed_snapshot: harness_failure="deploy_failed" (#145) --------
#
# Exhausting every deploy retry means start_problem() never got through to a
# real attempt, for any of several reasons (a readiness-wait timeout, a
# Helm.install RuntimeError, the native arm's own gate abort, or
# inject_fault() itself raising). The row is a harness failure either way,
# not an agent one: nothing ran, so there is no verdict the agent could have
# gotten wrong.


def test_an_exhausted_deploy_is_marked_as_a_deploy_failed_harness_failure():
    snapshot = main_mod._deploy_failed_snapshot("p1", 2, 3, TimeoutError("did not complete its healthy rollout"))

    assert snapshot["problem_id"] == "p1"
    assert snapshot["attempt"] == 2
    assert snapshot["deploy_failed"] is True
    assert snapshot["deploy_attempts"] == 3
    assert snapshot["harness_failure"] == "deploy_failed"
    assert "did not complete" in snapshot["harness_failure_detail"]


def test_a_deploy_failed_snapshot_with_no_captured_error_still_names_the_kind():
    """Defensive: every real call site always has an exception by the time
    all retries are exhausted, but the helper must not crash if one isn't."""
    snapshot = main_mod._deploy_failed_snapshot("p1", 1, 3, None)

    assert snapshot["harness_failure"] == "deploy_failed"
    assert "harness_failure_detail" not in snapshot
