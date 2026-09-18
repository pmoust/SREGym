"""Tests for the per-attempt fault spec that offline scoring reads."""

import json
import tempfile
import types
from pathlib import Path

import pytest

from main import _parse_structured_root_cause, _write_fault_spec

STRUCTURED = "[fault_spec] component=frontend; namespace=astronomy-shop || the image tag does not exist"


def _conductor(spec):
    return types.SimpleNamespace(fault_spec=spec)


def _write(spec, problem_id="incorrect_image", attempt=1):
    with tempfile.TemporaryDirectory() as tmp:
        _write_fault_spec(Path(tmp), _conductor(spec), problem_id, attempt)
        return json.loads((Path(tmp) / "fault-spec.json").read_text())


def test_parses_the_structured_root_cause():
    assert _parse_structured_root_cause(STRUCTURED) == (
        "frontend",
        "astronomy-shop",
        "the image tag does not exist",
    )


@pytest.mark.parametrize("text", [None, "", "the frontend image tag is wrong"])
def test_unstructured_root_cause_keeps_the_description(text):
    component, namespace, description = _parse_structured_root_cause(text)

    assert component == ""
    assert namespace == ""
    assert description == (text or "")


def test_emits_the_field_names_the_scorer_reads():
    written = _write(
        {
            "problem_id": "incorrect_image",
            "problem_class": "IncorrectImage",
            "namespace": "astronomy-shop",
            "root_cause": STRUCTURED,
            "fault_injected_at": 1758153600.0,
        },
        attempt=2,
    )

    assert written["problem"] == "incorrect_image"
    assert written["component"] == "frontend"
    assert written["namespace"] == "astronomy-shop"
    assert written["mechanism"] == "incorrect_image"
    assert written["injected_at"] == "2025-09-18T00:00:00Z"
    assert written["attempt"] == 2
    assert written["description"] == "the image tag does not exist"
    assert written["root_cause"] == STRUCTURED


def test_namespace_falls_back_to_the_problems_own():
    # A root cause that names no namespace still scores, on the component alone,
    # but the problem's namespace is the better answer when it has one.
    written = _write(
        {
            "problem_id": "kubelet_crash",
            "namespace": "kube-system",
            "root_cause": "[fault_spec] component=kubelet || the kubelet is down",
            "fault_injected_at": 1758153600.0,
        }
    )

    assert written["component"] == "kubelet"
    assert written["namespace"] == "kube-system"


def test_attempt_always_comes_from_the_caller():
    # The conductor captures once per injection; the attempt number does not
    # belong to that capture and must not be inherited from a stale one.
    assert _write({"attempt": 1}, attempt=3)["attempt"] == 3


@pytest.mark.parametrize("spec", [None, {}])
def test_uninjected_attempt_writes_an_explicit_null_timestamp(spec):
    # "File present, nothing injected" and "file missing" are different bugs, so
    # an attempt that never injected still gets a file.
    written = _write(spec, problem_id="kubelet_crash")

    assert written["problem"] == "kubelet_crash"
    assert written["mechanism"] == "kubelet_crash"
    assert written["injected_at"] is None
    assert written["component"] == ""
    assert written["root_cause"] is None


def test_unwritable_directory_does_not_raise(tmp_path):
    # A failed spec write must not lose the attempt's real results.
    missing = tmp_path / "never-created"
    _write_fault_spec(missing, _conductor({"root_cause": STRUCTURED}), "incorrect_image", 1)

    assert not missing.exists()
