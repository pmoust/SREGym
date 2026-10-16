"""Problem base class"""

from abc import ABC, abstractmethod


def fold_harness_failure(problem, results: dict) -> None:
    """Copy a problem's own harness_failure into the conductor's per-attempt
    results dict, unless one is already recorded there.

    mark_fault_injected sets problem.harness_failure when recover_fault
    raises during cleanup. The conductor's own cleanup-time image-pull
    detection (Conductor._detect_image_pull_harness_failure,
    sregym/conductor/conductor.py) can also set "harness_failure" in results
    directly, at the start of cleanup, before recover_fault runs; that one
    wins, since a recovery problem at the end of an attempt is a secondary
    symptom, not a second, unrelated cause worth losing the first one for.

    A free function rather than a Problem method so it can be exercised
    against any duck-typed stand-in (a real Problem, or a plain object with
    the same three attributes) without instantiating the ABC or its
    subclasses, none of which this needs.
    """
    if getattr(problem, "harness_failure", None) and "harness_failure" not in results:
        results["harness_failure"] = problem.harness_failure
        detail = getattr(problem, "harness_failure_detail", None)
        if detail:
            results["harness_failure_detail"] = detail


class Problem(ABC):
    run_default_workload = True

    def __init__(self, app, namespace: str | None = None):
        self.app = app
        self.namespace = app.namespace if namespace is None else namespace
        self.fault_injected = False
        self.results = {}
        self.root_cause = None  # root cause of the problem in natural language

        # Set by mark_fault_injected when recover_fault raises: "recovery_error".
        # Read by the conductor after cleanup and folded into its own results
        # so scoring can separate a harness failure from an agent failure.
        self.harness_failure: str | None = None
        self.harness_failure_detail: str | None = None

        # Optional: attach oracles in subclass
        self.diagnosis_oracle = None
        self.mitigation_oracle = None

    def requires_khaos(self) -> bool:
        """Override this method to return True if the problem requires Khaos for fault injection."""
        return False

    @classmethod
    def build_structured_root_cause(
        cls,
        *,
        component: str,
        namespace: str,
        description: str,
    ) -> str:
        """Return canonical structured root_cause text for judge-side parsing.

        Format:
        [fault_spec] component=<...>; namespace=<...> || <human-readable-description>
        """
        kv = [("component", component), ("namespace", namespace)]
        meta = "; ".join(f"{k}={str(v).strip()}" for k, v in kv)

        return f"[fault_spec] {meta} || {description.strip()}"

    @abstractmethod
    def inject_fault(self):
        pass

    @abstractmethod
    def recover_fault(self):
        pass
