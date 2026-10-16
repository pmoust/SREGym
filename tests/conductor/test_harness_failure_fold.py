"""fold_harness_failure: carrying a problem's own harness_failure (set by
mark_fault_injected when recover_fault raises) into the conductor's
per-attempt results dict (NOFireAI/sregym#145).

A free function, tested against plain duck-typed stand-ins rather than a
real Problem/Conductor: Problem is an ABC and Conductor's __init__ opens a
real kubeconfig, neither of which this needs.
"""

import types

from sregym.conductor.problems.base import fold_harness_failure


def _problem(harness_failure=None, harness_failure_detail=None):
    return types.SimpleNamespace(harness_failure=harness_failure, harness_failure_detail=harness_failure_detail)


def test_folds_a_recovery_error_into_empty_results():
    problem = _problem("recovery_error", "recover_fault: FileNotFoundError(...)")
    results: dict = {}

    fold_harness_failure(problem, results)

    assert results["harness_failure"] == "recovery_error"
    assert results["harness_failure_detail"] == "recover_fault: FileNotFoundError(...)"


def test_does_nothing_when_the_problem_recorded_no_harness_failure():
    results: dict = {}
    fold_harness_failure(_problem(), results)
    assert results == {}


def test_does_not_overwrite_an_image_pull_failure_recorded_earlier():
    """Pre-pull's image_pull, set during deploy, wins over a recovery_error
    found later in the same attempt's cleanup: first cause, not last."""
    results = {"harness_failure": "image_pull", "harness_failure_detail": "kind-a: img:1: timeout"}

    fold_harness_failure(_problem("recovery_error", "boom"), results)

    assert results["harness_failure"] == "image_pull"
    assert results["harness_failure_detail"] == "kind-a: img:1: timeout"


def test_detail_is_optional():
    results: dict = {}
    fold_harness_failure(_problem("recovery_error", None), results)
    assert results["harness_failure"] == "recovery_error"
    assert "harness_failure_detail" not in results


def test_works_against_a_plain_object_missing_the_attributes_entirely():
    """Defensive: getattr defaults mean this never raises even for an object
    that is not actually a Problem at all."""
    results: dict = {}
    fold_harness_failure(object(), results)
    assert results == {}
