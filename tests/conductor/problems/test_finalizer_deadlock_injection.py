"""The order inject_fault does things in, and what it leaves behind on failure.

sregym#50: planting the finalizer before the controller was alive turned a slow
pod into an undeletable namespace. The finalizer blocks deletion until a live
controller removes it, so a readiness timeout left nothing that could, and the
retry loop's first act is to delete the namespace. All three attempts died on
the same wall, the problem scored nothing, and the run reported success.

These tests exercise the orchestration in inject_fault rather than a cluster:
the ordering and the failure paths are the whole of what changed.
"""

from types import SimpleNamespace
from unittest import mock

import pytest

from sregym.conductor.problems.finalizer_deadlock_controller import FinalizerDeadlockController

_STEPS = (
    "_delete_support_resources",
    "_create_service_account",
    "_create_clusterrole",
    "_create_clusterrolebinding",
    "_create_controller_script",
    "_deploy_controller",
    "_wait_for_controller_ready",
    "_create_finalized_configmap",
    "_replace_clusterrole",
    "_wait_for_forbidden_log",
    "_force_clear_finalizer",
)


def _problem(calls, failing=None, error=None):
    """A problem whose steps only record that they ran."""
    problem = object.__new__(FinalizerDeadlockController)
    problem.namespace = "hotel-reservation"
    problem.configmap_name = "reservation-cleanup-token"
    problem.controller_name = "cleanup-controller"
    problem.clusterrole_name = "reservation-cleanup"
    problem.finalizer = "cleanup.reservations.io/pending-cleanup"
    problem.kubectl = SimpleNamespace(
        core_v1_api=SimpleNamespace(delete_namespaced_config_map=lambda *a, **k: calls.append("delete_configmap"))
    )

    for name in _STEPS:

        def step(*_args, _name=name, **_kwargs):
            calls.append(_name)
            if _name == failing:
                raise error

        setattr(problem, name, step)

    return problem


def test_the_controller_is_ready_before_the_finalizer_is_planted():
    """The ordering that turns a transient failure back into a transient one."""
    calls = []
    _problem(calls).inject_fault()

    assert calls.index("_wait_for_controller_ready") < calls.index("_create_finalized_configmap"), (
        "the finalizer must not be planted before a controller exists to remove it"
    )
    assert calls.index("_deploy_controller") < calls.index("_wait_for_controller_ready")


def test_a_readiness_timeout_plants_no_finalizer_at_all():
    """Nothing to strand, so the namespace stays deletable and the retry works.

    This is the failure that actually happened. It needs no cleanup now,
    because the ordering means there is nothing to clean up.
    """
    calls = []
    problem = _problem(calls, failing="_wait_for_controller_ready", error=TimeoutError("not ready"))

    with pytest.raises(TimeoutError):
        problem.inject_fault()

    assert "_create_finalized_configmap" not in calls
    assert "_force_clear_finalizer" not in calls, "there is nothing to clear"


def test_a_failure_after_planting_restores_rbac_then_clears_the_finalizer():
    """Order matters: clearing the finalizer patches the ConfigMap, and the
    read-only ClusterRole is exactly what would refuse that patch."""
    calls = []
    problem = _problem(calls, failing="_wait_for_forbidden_log", error=RuntimeError("no forbidden log"))

    with pytest.raises(RuntimeError):
        problem.inject_fault()

    tail = calls[calls.index("_wait_for_forbidden_log") :]
    assert tail.count("_replace_clusterrole") == 1
    assert tail.index("_replace_clusterrole") < tail.index("_force_clear_finalizer")


def test_cleanup_failure_does_not_mask_the_original_error():
    """The cleanup runs from an except block. If it raised, the reader would
    get its error instead of the one that broke the injection."""
    calls = []
    problem = _problem(calls, failing="_wait_for_forbidden_log", error=RuntimeError("the real failure"))
    problem._force_clear_finalizer = mock.Mock(side_effect=Exception("cleanup also broke"))

    with pytest.raises(RuntimeError, match="the real failure"):
        problem.inject_fault()


def test_the_readiness_timeout_names_the_controller_pods():
    """A Docker Hub throttle, a crash loop and a Pending pod are identical from
    the old message and need different fixes."""
    problem = object.__new__(FinalizerDeadlockController)
    problem.namespace = "hotel-reservation"
    problem.controller_name = "cleanup-controller"
    pod = SimpleNamespace(
        metadata=SimpleNamespace(name="cleanup-controller-abc", owner_references=[]),
        status=SimpleNamespace(
            phase="Pending",
            container_statuses=[
                SimpleNamespace(
                    name="controller",
                    ready=False,
                    state=SimpleNamespace(waiting=SimpleNamespace(reason="ImagePullBackOff"), terminated=None),
                )
            ],
        ),
    )
    from sregym.service.kubectl import KubeCtl

    problem.kubectl = SimpleNamespace(
        core_v1_api=SimpleNamespace(list_namespaced_pod=lambda **k: SimpleNamespace(items=[pod])),
        apps_v1_api=SimpleNamespace(
            read_namespaced_deployment=lambda *a, **k: SimpleNamespace(status=SimpleNamespace(ready_replicas=0))
        ),
        blocking_reason=KubeCtl.blocking_reason,
    )

    with pytest.raises(TimeoutError) as caught:
        problem._wait_for_controller_ready(timeout_seconds=0)

    assert "cleanup-controller-abc [Pending] controller=ImagePullBackOff" in str(caught.value)


def test_the_readiness_timeout_says_when_no_pod_was_ever_created():
    problem = object.__new__(FinalizerDeadlockController)
    problem.namespace = "hotel-reservation"
    problem.controller_name = "cleanup-controller"
    problem.kubectl = SimpleNamespace(
        core_v1_api=SimpleNamespace(list_namespaced_pod=lambda **k: SimpleNamespace(items=[])),
        apps_v1_api=SimpleNamespace(
            read_namespaced_deployment=lambda *a, **k: SimpleNamespace(status=SimpleNamespace(ready_replicas=0))
        ),
    )

    with pytest.raises(TimeoutError, match="never created one"):
        problem._wait_for_controller_ready(timeout_seconds=0)
