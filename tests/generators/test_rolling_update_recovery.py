import builtins
import os
import tempfile
from types import SimpleNamespace

import pytest
import yaml

from sregym.generators.fault.inject_virtual import VirtualizationFaultInjector


class _RecordingKubeCtl:
    def __init__(self):
        self.commands = []

    def exec_command(self, command):
        self.commands.append(command)
        return "OK\n"


class _InjectionKubeCtl:
    def __init__(self):
        self.events = []
        self.apps_v1_api = self

    def exec_command(self, command):
        self.events.append(("command", command))
        return "OK\n"

    def read_namespaced_deployment(self, name, namespace):
        self.events.append(("read", f"{namespace}/{name}"))
        return SimpleNamespace(
            metadata=SimpleNamespace(generation=1),
            spec=SimpleNamespace(replicas=3),
            status=SimpleNamespace(
                observed_generation=1,
                updated_replicas=3,
                ready_replicas=3,
                available_replicas=3,
                unavailable_replicas=0,
            ),
        )


def test_rolling_update_recovery_uses_saved_original_and_waits_for_readiness(monkeypatch):
    # recover_rolling_update_misconfigured now checks the baseline manifest
    # exists before applying it (NOFireAI/sregym#145), so this fake-command
    # test, which exercises the command sequence, not real file content,
    # tells it the manifest is there without writing to the real /tmp.
    monkeypatch.setattr("os.path.exists", lambda path: True)

    injector = object.__new__(VirtualizationFaultInjector)
    injector.namespace = "social-network"
    injector.kubectl = _RecordingKubeCtl()

    injector.recover_rolling_update_misconfigured(["custom-service"])

    assert injector.kubectl.commands == [
        "kubectl delete deployment custom-service -n social-network",
        "kubectl apply -f /tmp/custom-service-orig.yaml -n social-network",
        "kubectl rollout status deployment/custom-service -n social-network --timeout=120s",
    ]


def test_rolling_update_recovery_raises_when_the_baseline_manifest_is_missing(monkeypatch):
    """The actual bug behind sregym#145's recovery-error finding: when
    inject_rolling_update_misconfigured's own baseline rollout times out, it
    raises before ever writing /tmp/<service>-orig.yaml, so a later recover
    call had nothing to restore. Before this guard, that produced a
    confusing kubectl "path does not exist" error logged as a plain string,
    not an exception, so recovery looked like it ran. Now it raises a clear,
    specific error so mark_fault_injected can record a harness_failure.
    """
    monkeypatch.setattr("os.path.exists", lambda path: False)

    injector = object.__new__(VirtualizationFaultInjector)
    injector.namespace = "social-network"
    kube = _RecordingKubeCtl()
    injector.kubectl = kube

    with pytest.raises(FileNotFoundError, match="custom-service-orig.yaml"):
        injector.recover_rolling_update_misconfigured(["custom-service"])

    # The faulty deployment is still deleted even though nothing could be
    # restored: that part of cleanup is real and does not depend on the
    # missing file.
    assert kube.commands == ["kubectl delete deployment custom-service -n social-network"]


def test_rolling_update_injection_waits_for_healthy_baseline_before_fault_patch(monkeypatch, tmp_path):
    real_named_temporary_file = tempfile.NamedTemporaryFile
    monkeypatch.setattr(
        tempfile,
        "NamedTemporaryFile",
        lambda *args, **kwargs: real_named_temporary_file(*args, dir=tmp_path, **kwargs),
    )

    real_open = builtins.open

    def redirected_open(path, *args, **kwargs):
        if path == "/tmp/custom-service-orig.yaml":
            path = tmp_path / "custom-service-orig.yaml"
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", redirected_open)

    # inject_rolling_update_misconfigured now unlinks this same path at the
    # start of injection (#145); redirect that too, so the test never
    # touches the real /tmp.
    real_remove = os.remove

    def redirected_remove(path, *args, **kwargs):
        if path == "/tmp/custom-service-orig.yaml":
            path = tmp_path / "custom-service-orig.yaml"
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(os, "remove", redirected_remove)

    injector = object.__new__(VirtualizationFaultInjector)
    injector.namespace = "social-network"
    injector.kubectl = _InjectionKubeCtl()

    injector.inject_rolling_update_misconfigured(["custom-service"])

    apply_index = next(
        index
        for index, event in enumerate(injector.kubectl.events)
        if event[0] == "command" and event[1].startswith("kubectl apply")
    )
    ready_index = next(index for index, event in enumerate(injector.kubectl.events) if event[0] == "read")
    patch_index = next(
        index
        for index, event in enumerate(injector.kubectl.events)
        if event[0] == "command" and event[1].startswith("kubectl patch")
    )

    assert apply_index < ready_index < patch_index

    patch_command = injector.kubectl.events[patch_index][1]
    patch_path = patch_command.split("--patch-file ", 1)[1]
    with real_open(patch_path) as patch_file:
        patch = yaml.safe_load(patch_file)
    init_container = patch["spec"]["template"]["spec"]["initContainers"][0]
    assert init_container["image"] == "busybox:1.36"
    assert init_container["imagePullPolicy"] == "IfNotPresent"


def test_injection_clears_a_stale_baseline_manifest_before_the_rollout(monkeypatch, tmp_path):
    """The actual bug behind #145's stale-file finding: a leftover
    /tmp/<service>-orig.yaml from an earlier attempt (a different namespace,
    a different baseline) passes recover's FileNotFoundError guard and gets
    applied against the wrong cluster state. Cleared up front, before the
    baseline rollout even starts, so a stale file can never survive into
    this attempt's recovery."""
    stale_path = tmp_path / "custom-service-orig.yaml"
    stale_path.write_text("stale: true\n")

    real_remove = os.remove

    def redirected_remove(path, *args, **kwargs):
        if path == "/tmp/custom-service-orig.yaml":
            path = stale_path
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(os, "remove", redirected_remove)

    # Make the baseline rollout itself time out immediately after the stale
    # file would have been cleared, so this test only has to prove the
    # unlink happened up front, not exercise the rest of injection.
    injector = object.__new__(VirtualizationFaultInjector)
    injector.namespace = "social-network"
    injector.kubectl = _RecordingKubeCtl()
    monkeypatch.setattr(injector, "_wait_for_deployment_rollout", lambda *a, **k: (_ for _ in ()).throw(TimeoutError))

    real_named_temporary_file = tempfile.NamedTemporaryFile
    monkeypatch.setattr(
        tempfile,
        "NamedTemporaryFile",
        lambda *args, **kwargs: real_named_temporary_file(*args, dir=tmp_path, **kwargs),
    )

    assert stale_path.exists()

    with pytest.raises(TimeoutError):
        injector.inject_rolling_update_misconfigured(["custom-service"])

    assert not stale_path.exists()


class _NeverReadyKubeCtl:
    """A deployment whose status never reaches healthy, so
    _wait_for_deployment_rollout always times out."""

    def __init__(self):
        self.commands = []
        self.apps_v1_api = self

    def read_namespaced_deployment(self, name, namespace):
        return SimpleNamespace(
            metadata=SimpleNamespace(generation=1),
            spec=SimpleNamespace(replicas=3),
            status=SimpleNamespace(
                observed_generation=1,
                updated_replicas=1,  # stuck: only 1 of 3 ever updates
                ready_replicas=1,
                available_replicas=1,
                unavailable_replicas=2,
            ),
        )

    def exec_command(self, command):
        self.commands.append(command)
        if command.startswith("kubectl get pods"):
            return "custom-service-abc\n"
        return "OK\n"


def test_a_rollout_timeout_logs_status_and_pod_describe_before_raising():
    """The bare TimeoutError names no pod and no reason (sregym#145): this is
    what the harness actually has to go on instead, logged rather than lost
    the moment the attempt is torn down and redeployed."""
    injector = object.__new__(VirtualizationFaultInjector)
    injector.namespace = "social-network"
    injector.kubectl = _NeverReadyKubeCtl()

    with pytest.raises(TimeoutError, match="did not complete its healthy baseline rollout"):
        injector._wait_for_deployment_rollout("custom-service", timeout=0.05, sleep=0.01)

    commands = injector.kubectl.commands
    assert any(c.startswith("kubectl rollout status deployment/custom-service") for c in commands)
    assert any(c.startswith("kubectl get pods") and "-l app=custom-service" in c for c in commands)
    assert any(c.startswith("kubectl describe pod custom-service-abc") for c in commands)


def test_rollout_timeout_diagnostics_never_replaces_the_real_error(monkeypatch):
    """A diagnostics failure must not hide the real TimeoutError behind a
    less useful one."""

    class _BrokenKubeCtl(_NeverReadyKubeCtl):
        def exec_command(self, command):
            raise RuntimeError("API server unreachable")

    injector = object.__new__(VirtualizationFaultInjector)
    injector.namespace = "social-network"
    injector.kubectl = _BrokenKubeCtl()

    with pytest.raises(TimeoutError, match="did not complete its healthy baseline rollout"):
        injector._wait_for_deployment_rollout("custom-service", timeout=0.02, sleep=0.01)
