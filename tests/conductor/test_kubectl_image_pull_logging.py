"""kubectl.py's full (untruncated) kubelet message, logged at a readiness
timeout alongside its already-truncated one-line summary (NOFireAI/sregym#145).

No cluster needed: KubeCtl's own network calls are stubbed.
"""

import logging
from contextlib import contextmanager
from types import SimpleNamespace

from sregym.service import kubectl as kubectl_module


@contextmanager
def _collect_records(logger_name="all.infra.kubectl"):
    """Collect records straight off the named logger, bypassing propagation.

    logger/__init__.py's init_logger() sets the "all" logger's propagate to
    False once it has run, which several other tests in this suite trigger
    by importing main.py, after which pytest's caplog fixture, which
    attaches its handler to the real root logger, never sees anything
    logged under "all.*" again for the rest of the session. Attaching
    directly to this module's own logger sidesteps that entirely, and is
    robust to whichever order the suite happens to run in.
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger(logger_name)
    handler = _Collector()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


# --- _log_full_pull_failure_details ---------------------------------------


def _pod_with(namespace, name, container, reason, message):
    return SimpleNamespace(
        metadata=SimpleNamespace(namespace=namespace, name=name),
        status=SimpleNamespace(
            container_statuses=[
                SimpleNamespace(
                    name=container,
                    ready=False,
                    state=SimpleNamespace(waiting=SimpleNamespace(reason=reason, message=message), terminated=None),
                )
            ]
        ),
    )


def test_full_pull_failure_detail_is_logged_untruncated():
    long_message = 'Back-off pulling image "deathstarbench/wrk2-client:latest": ' + ("detail " * 40)
    pod = _pod_with("social-network", "wrk2-job-abc", "wrk2", "ImagePullBackOff", long_message)

    with _collect_records() as records:
        kubectl_module._log_full_pull_failure_details([pod])

    assert any(long_message in record.getMessage() for record in records)


def test_full_pull_failure_detail_skips_ready_containers():
    pod = SimpleNamespace(
        metadata=SimpleNamespace(namespace="ns", name="ready-pod"),
        status=SimpleNamespace(
            container_statuses=[
                SimpleNamespace(name="c", ready=True, state=SimpleNamespace(waiting=None, terminated=None))
            ]
        ),
    )

    with _collect_records() as records:
        kubectl_module._log_full_pull_failure_details([pod])

    assert records == []


def test_full_pull_failure_detail_handles_no_pods():
    # Must not raise on None or an empty list. The timeout path calls this
    # even when the wait's very first poll never returned.
    kubectl_module._log_full_pull_failure_details(None)
    kubectl_module._log_full_pull_failure_details([])


def test_full_pull_failure_detail_is_silent_without_a_message():
    pod = _pod_with("ns", "p", "c", "Pending", None)

    with _collect_records() as records:
        kubectl_module._log_full_pull_failure_details([pod])

    assert records == []
