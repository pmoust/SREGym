"""mark_fault_injected records a harness_failure when recover_fault raises
(NOFireAI/sregym#145).

Before this, a recover_fault that raised was logged and swallowed with no
trace left on the Problem instance, so a cleanup that broke (e.g. a baseline
manifest recover_fault needed but inject_fault never got to write) was
indistinguishable from a clean one once the attempt's row was built.
"""

from sregym.utils.decorators import mark_fault_injected


class _FakeProblem:
    def __init__(self):
        self.fault_injected = False
        self.harness_failure = None
        self.harness_failure_detail = None

    @mark_fault_injected
    def inject_fault(self):
        pass

    @mark_fault_injected
    def recover_fault(self):
        raise FileNotFoundError("no baseline manifest")


class _FakeProblemCleanRecovery:
    def __init__(self):
        self.fault_injected = False
        self.harness_failure = None
        self.harness_failure_detail = None
        self.recovered = False

    @mark_fault_injected
    def recover_fault(self):
        self.recovered = True


class _FakeProblemFailingInject:
    def __init__(self):
        self.fault_injected = False
        self.harness_failure = None
        self.harness_failure_detail = None

    @mark_fault_injected
    def inject_fault(self):
        raise RuntimeError("cluster unreachable")


def test_a_raising_recover_fault_is_recorded_as_a_recovery_error():
    problem = _FakeProblem()

    problem.recover_fault()  # swallowed, not raised

    assert problem.harness_failure == "recovery_error"
    assert "no baseline manifest" in problem.harness_failure_detail
    assert problem.fault_injected is False


def test_a_clean_recover_fault_records_no_harness_failure():
    problem = _FakeProblemCleanRecovery()

    problem.recover_fault()

    assert problem.recovered is True
    assert problem.harness_failure is None


def test_inject_fault_still_raises_and_records_no_harness_failure():
    """inject_fault's own exception is not a recovery_error: it is this
    attempt's deploy_failed path instead, handled at the main.py
    retry-loop level, not here."""
    problem = _FakeProblemFailingInject()

    try:
        problem.inject_fault()
        raised = False
    except RuntimeError:
        raised = True

    assert raised is True
    assert problem.harness_failure is None


def test_a_second_recovery_error_does_not_overwrite_the_first():
    class _TwiceFailing:
        def __init__(self):
            self.fault_injected = False
            self.harness_failure = None
            self.harness_failure_detail = None
            self.calls = 0

        @mark_fault_injected
        def recover_fault(self):
            self.calls += 1
            raise RuntimeError(f"failure #{self.calls}")

    problem = _TwiceFailing()
    problem.recover_fault()
    problem.recover_fault()

    assert "failure #1" in problem.harness_failure_detail
    assert problem.calls == 2
