def mark_fault_injected(method):
    def wrapper(self, *args, **kwargs):
        try:
            result = method(self, *args, **kwargs)
        except Exception as e:
            if method.__name__ == "inject_fault":
                # We exit if there's an error during fault injection, warning if in recovery
                raise
            else:
                print(f"[{method.__name__}] Warning: encountered error: {e!r}")
                # A recover_fault that raises leaves the cluster in a state
                # nobody verified: the fault may still be live, a baseline
                # manifest may be missing, or a teardown step was skipped.
                # Recorded as a harness_failure (not an agent failure) so
                # scoring can tell "the environment broke during cleanup"
                # apart from "the agent reasoned incorrectly". First one
                # wins: a later cleanup error during the same attempt is a
                # secondary symptom, not a second, unrelated failure.
                if method.__name__ == "recover_fault" and not getattr(self, "harness_failure", None):
                    self.harness_failure = "recovery_error"
                    self.harness_failure_detail = f"{method.__name__}: {e!r}"
                result = None

        self.fault_injected = method.__name__ == "inject_fault"
        return result

    return wrapper
