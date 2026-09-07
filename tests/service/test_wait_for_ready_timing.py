"""Timing behaviour of wait_for_ready's poll loop.

Two defects existed here, both found while writing tests for the pod-naming
fix (sregym#61) and deliberately left for sregym#56, since fixing them
changes when a deploy fails rather than what its error message says:

- `wait += sleep` never advances when a caller passes sleep=0, so the loop
  never exits on its own and max_wait is effectively unenforceable.
- The same counter tracked accumulated sleep, not wall time, so time spent
  inside the pod-list API call was never counted, and a `max_wait=600` wait
  could run for materially longer than 600 seconds under cluster load.

Both are fixed by deriving a deadline once from time.monotonic() before the
loop and comparing against it on every iteration, which also turns `sleep`
into a pure poll interval rather than a countdown step.
"""

import time
from types import SimpleNamespace

import pytest

from sregym.service.kubectl import KubeCtl


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
        ),
    )


class _NeverReady(KubeCtl):
    """A KubeCtl whose one pod never becomes ready, so the loop can only
    exit through the deadline, never by seeing everything ready."""

    def __init__(self, api_latency: float = 0.0):
        self.polls = 0
        self.api_latency = api_latency

    def list_pods(self, namespace):
        self.polls += 1
        time.sleep(self.api_latency)  # simulates time spent inside the real API call
        return SimpleNamespace(items=[_never_ready_pod()])


def test_sleep_zero_still_terminates(monkeypatch):
    """`wait += sleep` never advances when sleep=0; a wall-clock deadline
    terminates the loop regardless of what sleep is."""
    kube = _NeverReady()

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def fake_sleep(seconds):
        # A real sleep(0) still lets wall time pass (scheduling, syscalls);
        # advance the fake clock a little so the test can't pass merely
        # because time never moves at all.
        clock[0] += 0.05

    monkeypatch.setattr(time, "sleep", fake_sleep)

    with pytest.raises(Exception, match="Timeout"):
        kube.wait_for_ready("openebs", sleep=0, max_wait=1)

    assert kube.polls > 1, "the loop must have polled more than once before the deadline hit"


def test_max_wait_bounds_wall_time_not_accumulated_sleep(monkeypatch):
    """API latency must count against max_wait, not just the sleep() calls
    between polls, or a max_wait=600 wait can run far longer in practice
    under cluster load (sregym#56, sregym#53's setup tail)."""
    api_latency = 0.5
    kube = _NeverReady(api_latency=api_latency)

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def fake_sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(time, "sleep", fake_sleep)

    max_wait = 1
    with pytest.raises(Exception, match="Timeout"):
        kube.wait_for_ready("openebs", sleep=0.01, max_wait=max_wait)

    # The old counter (`wait += sleep`) never counts api_latency, so it would
    # need max_wait / sleep = 100 polls to hit the deadline, each burning
    # another api_latency of simulated wall time (50s for a stated 1s wait).
    # A wall-clock deadline exits within a couple of polls instead.
    assert kube.polls <= 3
    assert clock[0] < max_wait + (2 * api_latency)


def test_normal_case_still_returns_once_ready(monkeypatch):
    """A positive sleep and a wait that completes well within max_wait must
    behave exactly as before: return, not raise, on the first ready poll."""

    class _ReadyImmediately(KubeCtl):
        def __init__(self):
            self.polls = 0

        def list_pods(self, namespace):
            self.polls += 1
            pod = SimpleNamespace(
                metadata=SimpleNamespace(name="p", owner_references=[]),
                status=SimpleNamespace(
                    phase="Running",
                    container_statuses=[SimpleNamespace(name="c", ready=True, state=None)],
                ),
            )
            return SimpleNamespace(items=[pod])

    kube = _ReadyImmediately()

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    kube.wait_for_ready("openebs", sleep=2, max_wait=600)  # must not raise

    assert kube.polls == 1, "must return on the first poll that sees everything ready, no extra sleep needed"
