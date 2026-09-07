"""Timing behaviour of wait_for_namespace_deletion and wait_for_stable.

Both had the same accumulated-sleep bug wait_for_ready did (sregym#56):
`wait += sleep` never advances when sleep=0, and never counts time spent
inside the API call each iteration makes, so max_wait was unenforceable in
the first case and understated in the second. Fixed the same way: a
deadline derived once from time.monotonic(), checked every iteration.
"""

import time
from types import SimpleNamespace

import pytest

from sregym.service.kubectl import KubeCtl


def test_namespace_deletion_sleep_zero_still_terminates(monkeypatch):
    """wait += sleep never advances when sleep=0; a wall-clock deadline
    terminates the loop regardless of what sleep is."""

    class _NeverDeleted(KubeCtl):
        def __init__(self):
            self.polls = 0
            self.core_v1_api = SimpleNamespace(read_namespace=self._read_namespace)

        def _read_namespace(self, name):
            self.polls += 1
            return SimpleNamespace(metadata=SimpleNamespace(name=name))  # still exists

    kube = _NeverDeleted()

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + 0.05))

    with pytest.raises(Exception, match="Timeout"):
        kube.wait_for_namespace_deletion("gone-soon", sleep=0, max_wait=1)

    assert kube.polls > 1, "the loop must have polled more than once before the deadline hit"


def test_namespace_deletion_returns_once_the_namespace_is_gone(monkeypatch):
    """A namespace that disappears on the first poll must return immediately,
    not sleep or poll again first."""

    class _AlreadyGone(KubeCtl):
        def __init__(self):
            self.polls = 0
            self.core_v1_api = SimpleNamespace(read_namespace=self._read_namespace)

        def _read_namespace(self, name):
            self.polls += 1
            raise Exception("not found")

    kube = _AlreadyGone()

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    kube.wait_for_namespace_deletion("gone-already", sleep=2, max_wait=600)  # must not raise

    assert kube.polls == 1, "must return on the first poll that sees the namespace gone"


def _never_ready_pod():
    return SimpleNamespace(
        metadata=SimpleNamespace(name="p", owner_references=[]),
        status=SimpleNamespace(
            phase="Running",
            container_statuses=[
                SimpleNamespace(
                    name="c",
                    ready=False,
                    state=SimpleNamespace(waiting=SimpleNamespace(reason="Pending"), terminated=None),
                )
            ],
            conditions=[],
        ),
    )


def test_stable_sleep_zero_still_terminates(monkeypatch):
    """Same bug, same fix, in wait_for_stable's own poll loop."""

    class _NeverStable(KubeCtl):
        def __init__(self):
            self.polls = 0

        def list_pods(self, namespace):
            self.polls += 1
            return SimpleNamespace(items=[_never_ready_pod()])

    kube = _NeverStable()

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + 0.05))

    with pytest.raises(Exception, match="Timeout"):
        kube.wait_for_stable("flaky", sleep=0, max_wait=1)

    assert kube.polls > 1, "the loop must have polled more than once before the deadline hit"


def test_stable_returns_once_every_pod_is_ready(monkeypatch):
    """A namespace whose pods are already all ready must return immediately."""

    class _AlreadyStable(KubeCtl):
        def __init__(self):
            self.polls = 0

        def list_pods(self, namespace):
            self.polls += 1
            pod = SimpleNamespace(
                metadata=SimpleNamespace(name="p", owner_references=[]),
                status=SimpleNamespace(
                    phase="Running",
                    container_statuses=[SimpleNamespace(name="c", ready=True, state=None)],
                    conditions=[],
                ),
            )
            return SimpleNamespace(items=[pod])

    kube = _AlreadyStable()

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    kube.wait_for_stable("steady", sleep=2, max_wait=600)  # must not raise

    assert kube.polls == 1, "must return on the first poll that sees everything ready, no extra sleep needed"
