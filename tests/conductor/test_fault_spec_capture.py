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
    stub._pre_agent_images = frozenset()
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


def test_inject_fault_snapshots_the_pre_agent_images():
    """_inject_fault must populate _pre_agent_images right after the fault
    runs, before the agent gets control: this is
    the baseline _detect_image_pull_harness_failure later checks a stuck
    image against, to tell a harness-owned image apart from one the agent
    introduced itself."""
    problem = _Problem()
    problem.app = types.SimpleNamespace(namespace="astronomy-shop")
    pod = types.SimpleNamespace(
        spec=types.SimpleNamespace(
            containers=[types.SimpleNamespace(image="docker.io/library/frontend:v1")],
            init_containers=[],
        )
    )
    stub = _conductor_stub(problem)
    stub.kubectl = types.SimpleNamespace(list_pods=lambda ns, timeout=None: types.SimpleNamespace(items=[pod]))

    Conductor._inject_fault(stub)

    assert stub._pre_agent_images == frozenset({"docker.io/library/frontend:v1"})


def test_inject_fault_snapshot_includes_a_cronjob_image():
    """A CronJob that has not fired yet has no live pod for the namespace
    scan to see, but its own object already declares the image its next Job
    will use. Missing this meant a CronJob run's stuck pull would otherwise
    look agent-introduced, since it was never part of the pre-agent
    snapshot."""
    problem = _Problem()
    problem.app = types.SimpleNamespace(namespace="astronomy-shop")
    cronjob = types.SimpleNamespace(
        spec=types.SimpleNamespace(
            job_template=types.SimpleNamespace(
                spec=types.SimpleNamespace(
                    template=types.SimpleNamespace(
                        spec=types.SimpleNamespace(
                            containers=[types.SimpleNamespace(image="docker.io/library/batch-job:v1")],
                            init_containers=[],
                        )
                    )
                )
            )
        )
    )
    stub = _conductor_stub(problem)
    stub.kubectl = types.SimpleNamespace(
        list_pods=lambda ns, timeout=None: types.SimpleNamespace(items=[]),
        list_cron_jobs=lambda ns, timeout=None: types.SimpleNamespace(items=[cronjob]),
    )

    Conductor._inject_fault(stub)

    assert stub._pre_agent_images == frozenset({"docker.io/library/batch-job:v1"})


def test_inject_fault_snapshot_survives_a_problem_without_an_app():
    problem = _Problem()  # no .app attribute
    stub = _conductor_stub(problem)

    Conductor._inject_fault(stub)

    assert stub._pre_agent_images == frozenset()


def test_inject_fault_snapshot_failure_records_a_results_field():
    """A pod-listing failure during the snapshot must not raise (fault
    injection cannot be blocked by it), but it must leave a trace scoring can
    see: self.results["pre_agent_snapshot"] = "failed". _conductor_stub does
    not set .results at all for the other tests in this file, so this one
    sets it explicitly."""
    problem = _Problem()
    problem.app = types.SimpleNamespace(namespace="astronomy-shop")
    stub = _conductor_stub(problem)
    stub.results = {}

    def _boom(ns, timeout=None):
        raise RuntimeError("API server unreachable")

    stub.kubectl = types.SimpleNamespace(list_pods=_boom)

    Conductor._inject_fault(stub)

    assert stub.results["pre_agent_snapshot"] == "failed"
    assert stub._pre_agent_images == frozenset()


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
