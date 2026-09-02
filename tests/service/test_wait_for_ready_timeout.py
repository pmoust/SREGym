"""What the readiness timeout says when it fires.

The message is the whole diagnostic. main.py's deploy retry catches this
exception, tears the problem down and redeploys, so a message naming only the
namespace makes every occurrence equally unexplainable after the fact. Four of
63 deploys in run-20260902-0606 timed out on the openebs namespace and nobody
could say which pod or why, because the instances were gone before anyone
looked (sregym#56).
"""

from types import SimpleNamespace

import pytest

from sregym.service.kubectl import KubeCtl


def _pod(name, phase="Running", containers=()):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, owner_references=[]),
        status=SimpleNamespace(
            phase=phase,
            container_statuses=[
                SimpleNamespace(name=c_name, ready=ready, state=state) for c_name, ready, state in containers
            ]
            or None,
        ),
    )


def _waiting(reason):
    return SimpleNamespace(waiting=SimpleNamespace(reason=reason), terminated=None)


def _terminated(reason):
    return SimpleNamespace(waiting=None, terminated=SimpleNamespace(reason=reason))


def _ready_state():
    return SimpleNamespace(waiting=None, terminated=None)


class _Kube(KubeCtl):
    """A KubeCtl whose only behaviour is the pod list it returns."""

    def __init__(self, pages):
        self._pages = list(pages)

    def list_pods(self, namespace):
        page = self._pages.pop(0) if len(self._pages) > 1 else self._pages[0]
        if isinstance(page, Exception):
            raise page

        return SimpleNamespace(items=page)


def _timeout_message(pages):
    kube = _Kube(pages)
    with pytest.raises(Exception) as caught:
        kube.wait_for_ready("openebs", sleep=0.01, max_wait=0.01)

    return str(caught.value)


def test_the_timeout_names_the_pods_and_why_each_is_blocked():
    """ImagePullBackOff and CrashLoopBackOff need different fixes, so the
    reason has to survive into the message, not just the pod name."""
    message = _timeout_message(
        [
            [
                _pod("init-pvc-02583368", containers=[("local-path-init", False, _waiting("ImagePullBackOff"))]),
                _pod("openebs-ndm-abcde", containers=[("ndm", False, _waiting("CrashLoopBackOff"))]),
                _pod("openebs-ready", containers=[("provisioner", True, _ready_state())]),
            ]
        ]
    )

    assert "2 not ready" in message
    assert "init-pvc-02583368 [Running] local-path-init=ImagePullBackOff" in message
    assert "openebs-ndm-abcde [Running] ndm=CrashLoopBackOff" in message
    assert "openebs-ready" not in message, "a ready pod must not be blamed"


def test_a_pod_the_scheduler_never_placed_is_still_named():
    """No container statuses at all, which is what an unschedulable pod looks
    like. The phase is the only signal, so the message has to carry it."""
    message = _timeout_message([[_pod("frontend-pending", phase="Pending")]])

    assert "frontend-pending [Pending] no container statuses" in message


def test_a_terminated_container_reports_its_reason():
    message = _timeout_message([[_pod("job-x", containers=[("run", False, _terminated("Error"))])]])

    assert "job-x [Running] run=Terminated:Error" in message


def test_an_unreachable_api_reads_differently_from_unready_pods():
    """Two different failures. If the API never answered, no pod is at fault,
    and a message naming pods would send the reader after the wrong thing."""
    message = _timeout_message([RuntimeError("connection refused")])

    assert "no pod list was ever returned" in message
    assert "not ready" not in message


def test_an_empty_namespace_says_so():
    """Waiting on a namespace with no pods times out looking identical to
    waiting on unready ones, and the fix is entirely different."""
    assert "held no pods at all" in _timeout_message([[]])
