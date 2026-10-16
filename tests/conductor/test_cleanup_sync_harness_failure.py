"""Conductor._cleanup_sync wires the two harness_failure sources together
(NOFireAI/sregym#145, #150): the observed-pod-state image-pull check that
runs before recover_fault, and fold_harness_failure's own recovery_error
pickup that runs after it.

Exercised against a duck-typed Conductor built with object.__new__ rather
than a real one: Conductor.__init__ opens a real kubeconfig via KubeCtl and
exits the process when one is not found, which makes it unusable as a unit
test fixture. Only the attributes _cleanup_sync actually touches are set.
"""

import logging
import types

from sregym.conductor.conductor import Conductor


def _bare_conductor(problem, results=None):
    conductor = object.__new__(Conductor)
    conductor.problem = problem
    conductor.logger = logging.getLogger("test.cleanup_sync_harness_failure")
    conductor.config = types.SimpleNamespace(enable_noise=False)
    conductor.results = results if results is not None else {}
    conductor._baseline_captured = False  # skip the reconcile-to-baseline branch
    return conductor


def _app(namespace="social-network"):
    return types.SimpleNamespace(namespace=namespace, cleanup=lambda: None)


def _pod_stuck_pulling(namespace, image):
    status = types.SimpleNamespace(
        name="wrk2",
        image=image,
        ready=False,
        state=types.SimpleNamespace(waiting=types.SimpleNamespace(reason="ImagePullBackOff")),
    )
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(namespace=namespace, name="wrk2-job-abc"),
        status=types.SimpleNamespace(container_statuses=[status], init_container_statuses=[]),
    )


def test_cleanup_sync_folds_a_recovery_error_set_by_recover_fault():
    """mark_fault_injected sets problem.harness_failure before recover_fault
    returns (it swallows the raise); _cleanup_sync must carry it into
    self.results."""
    problem = types.SimpleNamespace(
        app=_app(),
        recover_fault=lambda: None,
        harness_failure="recovery_error",
        harness_failure_detail="recover_fault: FileNotFoundError(...)",
    )
    conductor = _bare_conductor(problem)
    conductor.kubectl = types.SimpleNamespace(list_pods=lambda ns, timeout=None: types.SimpleNamespace(items=[]))

    conductor._cleanup_sync()

    assert conductor.results["harness_failure"] == "recovery_error"
    assert conductor.results["harness_failure_detail"] == "recover_fault: FileNotFoundError(...)"
    assert conductor.submission_stage == "done"


def test_cleanup_sync_does_nothing_when_recovery_is_clean():
    problem = types.SimpleNamespace(
        app=_app(), recover_fault=lambda: None, harness_failure=None, harness_failure_detail=None
    )
    conductor = _bare_conductor(problem)
    conductor.kubectl = types.SimpleNamespace(list_pods=lambda ns, timeout=None: types.SimpleNamespace(items=[]))

    conductor._cleanup_sync()

    assert "harness_failure" not in conductor.results


def test_cleanup_sync_detects_an_image_pull_failure_from_observed_pod_state():
    app = _app(namespace="social-network")
    problem = types.SimpleNamespace(
        app=app, recover_fault=lambda: None, harness_failure=None, harness_failure_detail=None
    )
    conductor = _bare_conductor(problem)
    pod = _pod_stuck_pulling("social-network", "deathstarbench/wrk2-client:latest")
    conductor.kubectl = types.SimpleNamespace(list_pods=lambda ns, timeout=None: types.SimpleNamespace(items=[pod]))

    conductor._cleanup_sync()

    assert conductor.results["harness_failure"] == "image_pull"
    assert "deathstarbench/wrk2-client:latest" in conductor.results["harness_failure_detail"]


def test_cleanup_sync_does_not_flag_an_intentional_image_fault():
    """incorrect_image's own fault (a tag that resolves on no registry) must
    never read as a harness failure: it is the thing under test."""
    app = _app(namespace="astronomy-shop")
    problem = types.SimpleNamespace(
        app=app, recover_fault=lambda: None, harness_failure=None, harness_failure_detail=None
    )
    conductor = _bare_conductor(problem)
    pod = _pod_stuck_pulling("astronomy-shop", "app-image:latest")
    conductor.kubectl = types.SimpleNamespace(list_pods=lambda ns, timeout=None: types.SimpleNamespace(items=[pod]))

    conductor._cleanup_sync()

    assert "harness_failure" not in conductor.results


def test_cleanup_sync_lets_the_image_pull_detection_win_over_a_later_recovery_error():
    """First cause, not last: the image-pull check runs before
    recover_fault, so it must not be clobbered by fold_harness_failure's own
    recovery_error pickup afterward."""
    app = _app(namespace="social-network")
    problem = types.SimpleNamespace(
        app=app,
        recover_fault=lambda: None,
        harness_failure="recovery_error",
        harness_failure_detail="boom",
    )
    conductor = _bare_conductor(problem)
    pod = _pod_stuck_pulling("social-network", "deathstarbench/wrk2-client:latest")
    conductor.kubectl = types.SimpleNamespace(list_pods=lambda ns, timeout=None: types.SimpleNamespace(items=[pod]))

    conductor._cleanup_sync()

    assert conductor.results["harness_failure"] == "image_pull"


def test_cleanup_sync_detects_image_pull_even_when_recover_fault_deletes_the_pod():
    """Detection must run BEFORE recover_fault: recovery can delete or
    replace the stuck pod as part of its own cleanup, and the attempt must
    still be flagged image_pull from the snapshot of how it actually ran.
    If the detection call were moved to after recover_fault, the pod would
    already be gone by the time pods are listed and this would fail."""
    app = _app(namespace="social-network")
    pod_list = [_pod_stuck_pulling("social-network", "deathstarbench/wrk2-client:latest")]

    def recover_fault():
        pod_list.clear()  # recovery "fixes" the stuck pod away

    problem = types.SimpleNamespace(
        app=app, recover_fault=recover_fault, harness_failure=None, harness_failure_detail=None
    )
    conductor = _bare_conductor(problem)
    conductor.kubectl = types.SimpleNamespace(
        list_pods=lambda ns, timeout=None: types.SimpleNamespace(items=list(pod_list))
    )

    conductor._cleanup_sync()

    assert conductor.results["harness_failure"] == "image_pull"


def test_cleanup_sync_does_not_flag_an_agent_introduced_tag():
    """An image stuck pulling that was not part of the pre-agent snapshot
    (self._pre_agent_images, taken right after fault injection) is the
    agent's own mistake, not a harness failure, even though it is not one
    of the fixed fault images either."""
    app = _app(namespace="social-network")
    problem = types.SimpleNamespace(
        app=app, recover_fault=lambda: None, harness_failure=None, harness_failure_detail=None
    )
    conductor = _bare_conductor(problem)
    conductor._pre_agent_images = frozenset({"docker.io/library/checkout:v1"})
    pod = _pod_stuck_pulling("social-network", "my-registry/checkout:agent-typo-tag")
    conductor.kubectl = types.SimpleNamespace(list_pods=lambda ns, timeout=None: types.SimpleNamespace(items=[pod]))

    conductor._cleanup_sync()

    assert "harness_failure" not in conductor.results


def test_cleanup_sync_still_flags_a_pre_agent_image_with_a_snapshot_set():
    app = _app(namespace="social-network")
    problem = types.SimpleNamespace(
        app=app, recover_fault=lambda: None, harness_failure=None, harness_failure_detail=None
    )
    conductor = _bare_conductor(problem)
    conductor._pre_agent_images = frozenset({"deathstarbench/wrk2-client:latest"})
    pod = _pod_stuck_pulling("social-network", "deathstarbench/wrk2-client:latest")
    conductor.kubectl = types.SimpleNamespace(list_pods=lambda ns, timeout=None: types.SimpleNamespace(items=[pod]))

    conductor._cleanup_sync()

    assert conductor.results["harness_failure"] == "image_pull"


def test_cleanup_sync_flags_a_probe_image_known_only_through_the_image_cache():
    """An oracle or probe pod the harness deploys outside the app's own
    namespace(s) (or after the pre-agent snapshot ran) is never part of
    self._pre_agent_images, but it is still harness-owned, not something the
    agent introduced: a stuck pull on it must be flagged, not silently
    excused as an agent mistake.
    "ubuntu:22.04" is in scripts/benchmark/image_cache.list, which
    _detect_image_pull_harness_failure now unions into known_images; before
    that fix, a pre-agent snapshot that saw a different image (wrk2 below,
    not ubuntu) would have treated this pull as agent-introduced and
    dropped it from harness_failure entirely."""
    app = _app(namespace="social-network")
    problem = types.SimpleNamespace(
        app=app, recover_fault=lambda: None, harness_failure=None, harness_failure_detail=None
    )
    conductor = _bare_conductor(problem)
    conductor._pre_agent_images = frozenset({"deathstarbench/wrk2-client:latest"})
    pod = _pod_stuck_pulling("social-network", "ubuntu:22.04")
    conductor.kubectl = types.SimpleNamespace(list_pods=lambda ns, timeout=None: types.SimpleNamespace(items=[pod]))

    conductor._cleanup_sync()

    assert conductor.results["harness_failure"] == "image_pull"


def test_cleanup_sync_image_cache_union_does_not_admit_an_agent_tag():
    """The image_cache.list union must not widen known_images enough to
    admit an agent-introduced tag that merely resembles a cached image's
    path: "my-registry/checkout:agent-typo-tag" is in no namespace snapshot
    and in no image_cache.list line, so it must still read as the agent's
    own mistake."""
    app = _app(namespace="social-network")
    problem = types.SimpleNamespace(
        app=app, recover_fault=lambda: None, harness_failure=None, harness_failure_detail=None
    )
    conductor = _bare_conductor(problem)
    conductor._pre_agent_images = frozenset({"docker.io/library/checkout:v1"})
    pod = _pod_stuck_pulling("social-network", "my-registry/checkout:agent-typo-tag")
    conductor.kubectl = types.SimpleNamespace(list_pods=lambda ns, timeout=None: types.SimpleNamespace(items=[pod]))

    conductor._cleanup_sync()

    assert "harness_failure" not in conductor.results


def test_cleanup_sync_falls_back_to_the_harness_cache_when_the_snapshot_failed():
    """self.results["pre_agent_snapshot"] == "failed" means there is no real
    pre-agent namespace snapshot to union with image_cache.list, but the
    static cache alone is still used as known_images, not None. A stuck
    pull on an image that genuinely is in the harness cache is still
    flagged as a harness failure."""
    app = _app(namespace="social-network")
    problem = types.SimpleNamespace(
        app=app, recover_fault=lambda: None, harness_failure=None, harness_failure_detail=None
    )
    conductor = _bare_conductor(problem, results={"pre_agent_snapshot": "failed"})
    conductor._pre_agent_images = frozenset()
    pod = _pod_stuck_pulling("social-network", "ubuntu:22.04")
    conductor.kubectl = types.SimpleNamespace(list_pods=lambda ns, timeout=None: types.SimpleNamespace(items=[pod]))

    conductor._cleanup_sync()

    assert conductor.results["harness_failure"] == "image_pull"


def test_cleanup_sync_snapshot_failure_no_longer_excuses_an_agent_tag_as_harness():
    """The old fallback (known_images=None on a snapshot failure) flagged
    every non-excluded stuck image as harness_failure, including an agent's
    own bad mitigation tag, which excuses it from scoring as the agent's
    mistake (score_run.py's GRADED_EXCLUDING_HARNESS_FAILURES drops a
    harness_failure row from graded). The new fallback (known_images=
    _harness_cache_images()) must not flag an image that is in no namespace
    snapshot and in no image_cache.list line: that is the agent's own
    mistake, not the harness's."""
    app = _app(namespace="social-network")
    problem = types.SimpleNamespace(
        app=app, recover_fault=lambda: None, harness_failure=None, harness_failure_detail=None
    )
    conductor = _bare_conductor(problem, results={"pre_agent_snapshot": "failed"})
    conductor._pre_agent_images = frozenset()
    pod = _pod_stuck_pulling("social-network", "some-app/never-cached:v9")
    conductor.kubectl = types.SimpleNamespace(list_pods=lambda ns, timeout=None: types.SimpleNamespace(items=[pod]))

    conductor._cleanup_sync()

    assert "harness_failure" not in conductor.results


def test_cleanup_sync_without_a_problem_skips_both_harness_failure_checks():
    conductor = _bare_conductor(problem=None)
    conductor.kubectl = types.SimpleNamespace(list_pods=lambda ns, timeout=None: types.SimpleNamespace(items=[]))

    conductor._cleanup_sync()

    assert conductor.results == {}
    assert conductor.submission_stage == "done"
