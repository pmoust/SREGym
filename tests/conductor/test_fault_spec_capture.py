"""The conductor captures fault ground truth in memory, never to disk."""

import types

from sregym.conductor.conductor import Conductor


class _Problem:
    namespace = "astronomy-shop"
    root_cause = "[fault_spec] component=frontend; namespace=astronomy-shop || bad tag"

    def __init__(self):
        self.injected = False

    def inject_fault(self):
        self.injected = True


def _conductor_stub(problem, problem_id="incorrect_image"):
    stub = Conductor.__new__(Conductor)
    stub.problem = problem
    stub.problem_id = problem_id
    stub.logger = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    stub.fault_injected = False
    stub.fault_injected_at = None
    stub.fault_spec = None
    return stub


def test_capture_records_the_problem_ground_truth():
    problem = _Problem()
    stub = _conductor_stub(problem)

    Conductor._inject_fault(stub)

    assert problem.injected
    assert stub.fault_spec["problem_id"] == "incorrect_image"
    assert stub.fault_spec["problem_class"] == "_Problem"
    assert stub.fault_spec["namespace"] == "astronomy-shop"
    assert stub.fault_spec["root_cause"] == _Problem.root_cause
    assert stub.fault_spec["fault_injected_at"] == stub.fault_injected_at


def test_capture_survives_a_problem_without_a_label():
    problem = _Problem()
    problem.root_cause = None
    stub = _conductor_stub(problem)

    Conductor._inject_fault(stub)

    assert stub.fault_spec["root_cause"] is None
    assert stub.fault_spec["fault_injected_at"] is not None


def test_stage_rebuild_clears_the_previous_attempts_capture():
    # An attempt that fails to deploy never injects. It must not publish the
    # previous attempt's spec or timestamp.
    stub = Conductor.__new__(Conductor)
    stub.logger = types.SimpleNamespace(warning=lambda *a, **k: None)
    stub.tasklist = None
    stub.fault_injected = True
    stub.fault_injected_at = 1.0
    stub.fault_spec = {"root_cause": "stale"}

    Conductor._build_stage_sequence(stub)

    assert stub.fault_injected is False
    assert stub.fault_injected_at is None
    assert stub.fault_spec is None
