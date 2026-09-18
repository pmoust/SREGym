"""Every registered problem must carry a root cause the scorer can parse.

A problem whose root_cause is free-form still runs and still publishes. It just
produces a manifest with no component, which the offline scorer counts as
unlabeled. That is a corpus gap that nothing else fails on, so it is asserted
here rather than discovered in a scored run.

The check is static. Instantiating a problem needs a live cluster and a Helm
chart, so this walks the classes the registry names instead.
"""

import contextlib
import inspect
import re
from unittest import mock

import pytest

from sregym.conductor.problems import registry as registry_module
from sregym.conductor.problems.base import Problem
from sregym.conductor.problems.khaos_faults import _KHAOS_FAULT_COMPONENTS, KhaosFaultName


@pytest.fixture
def registry():
    """The problem registry, built without touching a cluster.

    ProblemRegistry.__init__ ends by constructing KubeCtl, which calls exit(1)
    when there is no kubeconfig rather than raising. Without this the whole
    file passes on a machine with a cluster and dies on every machine without
    one, which is every CI runner, so the coverage claim below would never be
    checked where it matters.
    """
    with mock.patch.object(registry_module, "KubeCtl"):
        return registry_module.ProblemRegistry().PROBLEM_REGISTRY


def _classes(entry):
    """The problem classes a registry entry names, directly or through a factory."""
    if inspect.isclass(entry):
        return [entry]
    try:
        source = inspect.getsource(entry)
    except (OSError, TypeError):
        return []
    namespace = getattr(entry, "__globals__", {})
    names = set(re.findall(r"\b([A-Z][A-Za-z0-9_]+)\b", source))
    return [namespace[n] for n in names if inspect.isclass(namespace.get(n))]


def _own_source(entry) -> str:
    """Source of the entry's classes, excluding Problem itself.

    Problem defines build_structured_root_cause, so including it matches every
    problem and the check proves nothing.
    """
    source = ""
    for cls in _classes(entry):
        for base in cls.__mro__:
            if base is Problem or not getattr(base, "__module__", "").startswith("sregym"):
                continue
            with contextlib.suppress(OSError, TypeError):
                source += inspect.getsource(base)
    return source


def test_every_registered_problem_builds_a_structured_root_cause(registry):
    unstructured = sorted(
        pid for pid, entry in registry.items() if "build_structured_root_cause" not in _own_source(entry)
    )

    assert unstructured == [], (
        f"{len(unstructured)} of {len(registry)} problems carry a free-form root cause, "
        f"so the offline scorer cannot match a hypothesis against them: {unstructured}"
    )


def test_the_check_would_notice_a_free_form_problem():
    # Guards the exclusion above. Problem alone must not satisfy the check,
    # otherwise every problem passes for the wrong reason.
    assert "build_structured_root_cause" not in _own_source(Problem)


def test_every_registered_khaos_fault_names_its_hardware(registry):
    # KhaosFaultProblem builds its root cause from a table, so the scan above is
    # satisfied by the call site whether or not a given fault is in that table.
    # This asserts the data instead of the code shape.
    missing = []
    for pid, entry in sorted(registry.items()):
        try:
            source = inspect.getsource(entry)
        except (OSError, TypeError):
            continue
        if "KhaosFaultProblem" not in source:
            continue
        for name in re.findall(r"KhaosFaultName\.([A-Za-z0-9_]+)", source):
            if KhaosFaultName(name) not in _KHAOS_FAULT_COMPONENTS:
                missing.append(f"{pid} ({name})")

    assert missing == [], (
        "these khaos faults have no component in _KHAOS_FAULT_COMPONENTS, so their "
        f"root cause stays free-form and the scorer cannot match it: {missing}"
    )
