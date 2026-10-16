"""What the readiness timeout says when it fires.

The message is the whole diagnostic. main.py's deploy retry catches this
exception, tears the problem down and redeploys, so a message naming only the
namespace makes every occurrence equally unexplainable after the fact. Four of
63 deploys in run-20260902-0606 timed out on the openebs namespace and nobody
could say which pod or why, because the instances were gone before anyone
looked (sregym#56).
"""

import logging
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


def _waiting_with(reason, message):
    return SimpleNamespace(waiting=SimpleNamespace(reason=reason, message=message), terminated=None)


def test_the_pull_failure_reason_comes_with_its_message():
    """ImagePullBackOff alone cannot be acted on.

    run-20260903-0822 lost all 63 problem-runs to 61 deploy failures, 39
    reporting ImagePullBackOff, and the archive cannot say why a single pull
    failed: a registry rate limit, a full disk, a network timeout and a bad
    credential all render as that one word.
    """
    message = _timeout_message(
        [
            [
                _pod(
                    "loki-results-cache-0",
                    containers=[
                        (
                            "exporter",
                            False,
                            _waiting_with(
                                "ImagePullBackOff",
                                'Back-off pulling image "memcached:1.6-alpine": '
                                "failed to pull and unpack: no space left on device",
                            ),
                        )
                    ],
                )
            ]
        ]
    )

    assert "exporter=ImagePullBackOff" in message
    assert "no space left on device" in message


def test_a_multiline_message_stays_on_one_line():
    """Runtime errors arrive with embedded newlines and this is a summary line."""
    message = _timeout_message(
        [[_pod("p", containers=[("c", False, _waiting_with("CrashLoopBackOff", "line one\nline two"))])]]
    )

    assert "line one line two" in message
    assert "\n" not in message.split("1 not ready:")[1]


def test_a_very_long_message_is_truncated():
    """A pull failure quotes the full registry reference and digest, which is
    rarely the informative part, and this line is one of several per failure."""
    message = _timeout_message([[_pod("p", containers=[("c", False, _waiting_with("ImagePullBackOff", "x" * 400))])]])

    assert "…" in message
    assert len(message) < 400


def test_a_reason_with_no_message_reads_as_before():
    """Most states carry no message, and an empty parenthesis would be noise."""
    message = _timeout_message([[_pod("p", containers=[("c", False, _waiting_with("Pending", None))])]])

    assert "c=Pending" in message
    assert "()" not in message


def test_a_state_object_without_a_message_field_does_not_break_the_report():
    """The regression this nearly shipped.

    blocking_reason runs inside the poll loop, whose except arm swallows
    anything and lets the wait run to its timeout. An AttributeError there did
    not surface as an error: the timeout reported "no pod list was ever
    returned, so the API was unreachable for the whole wait" while the API had
    answered every poll. A diagnostic that can turn itself into a false one is
    worse than the missing detail.

    The fixtures above deliberately omit `message` for this reason.
    """
    bare = SimpleNamespace(waiting=SimpleNamespace(reason="ImagePullBackOff"), terminated=None)
    message = _timeout_message([[_pod("p", containers=[("c", False, bare)])]])

    assert "c=ImagePullBackOff" in message
    assert "1 not ready" in message
    assert "unreachable" not in message


def test_the_timeout_logs_the_full_untruncated_pull_failure_message():
    """wait_for_ready passes the last poll's pods to
    _log_full_pull_failure_details at its own timeout (#145), which is what
    actually lets a pull failure's full kubelet message reach the archived
    log. The raised exception's own message above is truncated to 160
    characters. This pins the wiring, not just _log_full_pull_failure_details
    in isolation (already covered in test_kubectl_image_pull_logging.py)."""
    long_message = 'Back-off pulling image "memcached:1.6-alpine": ' + ("detail " * 40)
    pods = [
        _pod(
            "loki-results-cache-0",
            containers=[("exporter", False, _waiting_with("ImagePullBackOff", long_message))],
        )
    ]

    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("all.infra.kubectl")
    handler = _Collector()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        _timeout_message([pods])
    finally:
        logger.removeHandler(handler)

    assert any(long_message in record.getMessage() for record in records)
