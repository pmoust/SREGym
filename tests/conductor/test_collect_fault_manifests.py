"""Tests for collecting per-attempt fault specs into a manifest array."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "benchmark"))

from collect_fault_manifests import collect  # noqa: E402


def _spec(tmp_path, name, **fields):
    attempt_dir = tmp_path / name
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "fault-spec.json").write_text(json.dumps(fields))
    return attempt_dir


def test_collects_one_entry_per_problem(tmp_path):
    _spec(
        tmp_path,
        "a",
        problem="incorrect_image",
        component="frontend",
        namespace="astronomy-shop",
        mechanism="incorrect_image",
        injected_at="2025-09-18T00:00:00Z",
        attempt=1,
    )
    _spec(tmp_path, "b", problem="kubelet_crash", component="kubelet", mechanism="kubelet_crash", attempt=1)

    manifests, unlabeled = collect(tmp_path)

    assert [m["problem"] for m in manifests] == ["incorrect_image", "kubelet_crash"]
    assert manifests[0]["component"] == "frontend"
    assert unlabeled == []


def test_a_later_attempt_wins(tmp_path):
    _spec(tmp_path, "a1", problem="incorrect_image", component="old", attempt=1)
    _spec(tmp_path, "a2", problem="incorrect_image", component="new", attempt=2)

    manifests, _ = collect(tmp_path)

    assert len(manifests) == 1
    assert manifests[0]["component"] == "new"


def test_reports_a_spec_with_no_component(tmp_path):
    _spec(tmp_path, "a", problem="mystery", component="", attempt=1)

    manifests, unlabeled = collect(tmp_path)

    assert len(manifests) == 1
    assert len(unlabeled) == 1


def test_a_malformed_spec_does_not_stop_the_walk(tmp_path):
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "fault-spec.json").write_text("{not json")
    _spec(tmp_path, "good", problem="kubelet_crash", component="kubelet", attempt=1)

    manifests, _ = collect(tmp_path)

    assert [m["problem"] for m in manifests] == ["kubelet_crash"]


def test_missing_keys_fall_back_rather_than_raise(tmp_path):
    _spec(tmp_path, "a", problem="kubelet_crash", attempt=1)

    manifests, unlabeled = collect(tmp_path)

    assert manifests[0]["mechanism"] == "kubelet_crash"
    assert manifests[0]["component"] == ""
    assert manifests[0]["injected_at"] is None
    assert len(unlabeled) == 1


def test_unlabeled_counts_problems_not_attempt_files(tmp_path):
    # The summary sentence says "problems", so the number must be in that unit.
    # Three attempts of one unlabeled problem is one unlabeled problem.
    for i in (1, 2, 3):
        _spec(tmp_path, f"a{i}", problem="mystery", component="", attempt=i)

    manifests, unlabeled = collect(tmp_path)

    assert len(manifests) == 1
    assert len(unlabeled) == 1


def test_an_earlier_unlabeled_attempt_does_not_count_when_the_winner_has_a_component(tmp_path):
    _spec(tmp_path, "a1", problem="incorrect_image", component="", attempt=1)
    _spec(tmp_path, "a2", problem="incorrect_image", component="frontend", attempt=2)

    manifests, unlabeled = collect(tmp_path)

    assert manifests[0]["component"] == "frontend"
    assert unlabeled == []
