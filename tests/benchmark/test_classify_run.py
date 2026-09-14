"""Failure-class splitting, validated against the run it was derived from.

An attempt that gathers evidence and reasons to the wrong conclusion and an
attempt that gathers evidence and then submits a template saying it collected
none both score zero. The first is a reasoning result; the second is plumbing
eating the answer. These tests pin that the two stay apart, because the whole
value of the split is that a score cannot tell them apart.

The template marker is not a guess. Grouping run-20260908-0739's 63 submissions
by their opening produced one cluster of 18 byte-identical texts and 45
singletons, and 18 is the count that run is known for.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("classify_run", ROOT / "scripts" / "benchmark" / "classify_run.py")
classify_run = importlib.util.module_from_spec(_SPEC)
sys.modules["classify_run"] = classify_run
_SPEC.loader.exec_module(classify_run)

TEMPLATE = (
    "## Summary\n\nInvestigation of investigation could not produce a diagnosis "
    "because no observability signals (metrics, logs, traces) were collected."
)

HEADER = [
    "Diagnosis.submission",
    "Diagnosis.success",
    "Mitigation.success",
    "attempt",
    "problem_id",
    "tool_calls",
    "tool_calls_diagnosis",
]


def _run(tmp_path, attempts, tool_calls=None, runtime_dupes=None):
    """Write a run directory in the shape the harness archives.

    tool_calls entries are (problem, attempt, payload) and land on the canonical
    results path. runtime_dupes land under the hidden .runtime directory the
    harness also writes, and must not be counted.
    """
    slot = tmp_path / "shard-0" / "slots" / "0"
    results = slot / "results" / "0908_0744" / "brain_go"
    results.mkdir(parents=True)

    lines = [",".join(HEADER)]
    for row in attempts:
        cells = []
        for key in HEADER:
            value = str(row.get(key, ""))
            needs_quotes = any(ch in value for ch in ',"\n')
            cells.append('"' + value.replace('"', '""') + '"' if needs_quotes else value)
        lines.append(",".join(cells))
    (results / "brain_go_ALL_results.csv").write_text("\n".join(lines) + "\n")

    for problem, attempt, payload in tool_calls or []:
        run_dir = results / problem / f"run_{attempt}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "tool-calls.json").write_text(json.dumps(payload))

    for index, payload in enumerate(runtime_dupes or []):
        anon = slot / ".runtime" / "brain_go" / f"anon_{index}"
        anon.mkdir(parents=True)
        (anon / "tool-calls.json").write_text(json.dumps(payload))

    return tmp_path


def _attempt(**over):
    row = {
        "Diagnosis.submission": "## Summary\n\nA real diagnosis naming a real cause.",
        "Diagnosis.success": "False",
        "Mitigation.success": "False",
        "attempt": "1",
        "problem_id": "some_problem",
        "tool_calls": "100",
        "tool_calls_diagnosis": "60",
    }
    row.update(over)

    return row


def test_a_template_submission_is_not_counted_as_reasoning(tmp_path):
    """The defect this script exists for: a template attempt scores like a wrong
    answer, so a score alone reports a plumbing failure as a reasoning one."""
    run = _run(
        tmp_path,
        [
            _attempt(**{"Diagnosis.submission": TEMPLATE}),
            _attempt(),
        ],
    )
    summary = classify_run.classify(classify_run.load_attempts(str(run)))

    assert summary["boilerplate"] == 1
    assert summary["wrong"] == 1
    assert summary["correct"] == 0


def test_a_correct_diagnosis_wins_over_the_template_match(tmp_path):
    """Order matters: a graded-correct attempt is correct even if its text
    happened to carry the marker, so the classes cannot double count."""
    run = _run(
        tmp_path,
        [
            _attempt(**{"Diagnosis.success": "True", "Diagnosis.submission": TEMPLATE}),
        ],
    )
    summary = classify_run.classify(classify_run.load_attempts(str(run)))

    assert summary["correct"] == 1
    assert summary["boilerplate"] == 0
    assert summary["attempts"] == summary["correct"] + summary["wrong"] + summary["boilerplate"]


def test_the_reasoned_rate_excludes_template_attempts(tmp_path):
    """Two of four graded correct, one of the rest a template: 2/4 overall and
    2/3 among attempts that answered. The gap is what the plumbing costs."""
    run = _run(
        tmp_path,
        [
            _attempt(**{"Diagnosis.success": "True"}),
            _attempt(**{"Diagnosis.success": "True"}),
            _attempt(),
            _attempt(**{"Diagnosis.submission": TEMPLATE}),
        ],
    )
    summary = classify_run.classify(classify_run.load_attempts(str(run)))

    assert summary["diagnosis_rate"] == 0.5
    assert summary["diagnosis_rate_reasoned"] == 2 / 3


def test_an_attempt_that_mitigated_then_reported_no_evidence_is_counted(tmp_path):
    """The sharpest case, and 11 of run-20260908-0739's 18: the agent fixed the
    fault and then submitted a template saying it gathered nothing. Evidence
    that the reasoning worked and the reporting threw it away."""
    run = _run(
        tmp_path,
        [
            _attempt(**{"Diagnosis.submission": TEMPLATE, "Mitigation.success": "True"}),
            _attempt(**{"Diagnosis.submission": TEMPLATE, "Mitigation.success": "False"}),
        ],
    )
    summary = classify_run.classify(classify_run.load_attempts(str(run)))

    assert summary["boilerplate"] == 2
    assert summary["boilerplate_mitigated"] == 1


def test_truthiness_survives_the_csv_round_trip(tmp_path):
    """The verdicts arrive as text, and "true"/"True"/"1" all mean the same
    thing. A bare bool() on the string "False" would count every attempt."""
    run = _run(
        tmp_path,
        [
            _attempt(**{"Diagnosis.success": "true"}),
            _attempt(**{"Diagnosis.success": "1"}),
            _attempt(**{"Diagnosis.success": "False"}),
            _attempt(**{"Diagnosis.success": ""}),
        ],
    )
    summary = classify_run.classify(classify_run.load_attempts(str(run)))

    assert summary["correct"] == 2


def test_tool_calls_are_attributed_by_node(tmp_path):
    """The planner share is the run's own answer to where the calls went, and it
    comes from the driver's count of its attempt rather than from Prometheus,
    which is process-wide across a box's three slots."""
    run = _run(
        tmp_path,
        [_attempt()],
        tool_calls=[
            (
                "some_problem",
                "1",
                {
                    "total": 10,
                    "by_node": {"hypothesis_gen_tools": 7, "metrics": 3},
                    "by_phase_and_node": {"diagnosis": {"hypothesis_gen_tools": 5, "metrics": 3}},
                },
            ),
        ],
    )
    by_node, by_phase_node, total, per_attempt = classify_run.load_tool_calls(str(run))

    assert total == 10
    assert by_node["hypothesis_gen_tools"] == 7
    assert by_phase_node["diagnosis"]["metrics"] == 3
    assert ("some_problem", "1") in per_attempt


def test_tool_calls_sum_across_attempts(tmp_path):
    """One file per attempt, so a run's figure is their sum."""
    run = _run(
        tmp_path,
        [_attempt(), _attempt(**{"attempt": "2"})],
        tool_calls=[
            ("some_problem", "1", {"total": 4, "by_node": {"state": 4}}),
            ("some_problem", "2", {"total": 6, "by_node": {"state": 1, "logs": 5}}),
        ],
    )
    by_node, _, total, per_attempt = classify_run.load_tool_calls(str(run))

    assert total == 10
    assert by_node["state"] == 5
    assert by_node["logs"] == 5
    assert len(per_attempt) == 2


def test_the_runtime_copy_of_the_same_file_is_not_counted(tmp_path):
    """The harness writes tool-calls.json twice, once per attempt under results
    and once under .runtime keyed by an investigation hash. run-20260908-0739
    archived 115 for 63 attempts. Counting both would inflate every share.
    Matching on the results/.../run_N/ path is what excludes the .runtime
    copy here; a pattern that matched the file name anywhere, with no such
    path constraint, would get the right answer only because glob happens to
    skip dot-directories today, which is luck rather than a rule."""
    run = _run(
        tmp_path,
        [_attempt()],
        tool_calls=[("some_problem", "1", {"total": 10, "by_node": {"state": 10}})],
        runtime_dupes=[{"total": 10, "by_node": {"state": 10}}],
    )
    by_node, _, total, per_attempt = classify_run.load_tool_calls(str(run))

    assert total == 10, "the .runtime duplicate must not be added to the total"
    assert by_node["state"] == 10
    assert len(per_attempt) == 1


def test_attempt_key_reads_problem_and_attempt_from_the_path(tmp_path):
    """The file holds counts and no identifier, so the path is the only place
    the attempt's identity lives."""
    key = classify_run.attempt_key(
        "/x/shard-0/slots/0/results/0908_0744/brain_go/network_policy_block/run_3/tool-calls.json"
    )

    assert key == ("network_policy_block", "3")
    assert classify_run.attempt_key("/x/.runtime/brain_go/anon_ab/tool-calls.json") is None


def test_a_missing_tool_calls_file_shows_up_as_a_mismatch(tmp_path):
    """The CSV and the JSON count the same calls two ways. When they disagree
    every share printed below them is wrong, so the report says so rather than
    rendering a plausible number."""
    run = _run(
        tmp_path,
        [_attempt(**{"tool_calls": "100"}), _attempt(**{"attempt": "2", "tool_calls": "100"})],
        tool_calls=[("some_problem", "1", {"total": 100, "by_node": {"state": 100}})],
    )
    rows = classify_run.load_attempts(str(run))
    _, _, json_total, _ = classify_run.load_tool_calls(str(run))
    csv_total = sum(int(r["tool_calls"]) for r in rows)

    assert csv_total == 200
    assert json_total == 100


def test_a_run_with_no_tool_call_files_still_classifies(tmp_path):
    """An older run, or one whose results were partly archived, must still give
    its failure classes rather than failing the whole report."""
    run = _run(tmp_path, [_attempt(**{"Diagnosis.submission": TEMPLATE})])
    summary = classify_run.classify(classify_run.load_attempts(str(run)))
    by_node, by_phase_node, total, per_attempt = classify_run.load_tool_calls(str(run))

    assert summary["boilerplate"] == 1
    assert total == 0
    assert not by_node and not by_phase_node and not per_attempt


def test_the_marker_matches_the_template_actually_submitted(tmp_path):
    """Pins the criterion against the real text. The wording around it has
    already changed once, so the marker is the clause carrying the claim rather
    than the whole paragraph."""
    assert classify_run.ZERO_EVIDENCE_MARK in TEMPLATE
    assert classify_run.ZERO_EVIDENCE_MARK not in "A real diagnosis naming a real cause."


# --- the print layer: report(), compare(), main() ---------------------------
#
# The reconciliation check above is only real if report() itself is exercised.
# A test that recomputes both totals by hand and compares them proves nothing
# about the code that decides what to print: hardcoding report()'s status to
# "ok" left every prior test green. These call report()/compare()/main() and
# assert on what they actually print, the way tests/benchmark/test_score_run.py
# asserts on render()'s output rather than on score()'s dict alone.


def test_report_prints_ok_when_the_csv_and_json_totals_agree(tmp_path, capsys):
    run = _run(
        tmp_path,
        [_attempt(**{"tool_calls": "10"})],
        tool_calls=[("some_problem", "1", {"total": 10, "by_node": {"state": 10}})],
    )
    classify_run.report("run-x", str(run))
    out = capsys.readouterr().out

    assert "tool-call reconciliation: ok" in out


def test_report_prints_a_mismatch_naming_both_totals_when_a_file_is_missing(tmp_path, capsys):
    """The scenario the reconciliation check exists for: one attempt's
    tool-calls.json never made it into the archive, so the json total falls
    short of the csv total the driver itself recorded per attempt."""
    run = _run(
        tmp_path,
        [
            _attempt(**{"tool_calls": "100"}),
            _attempt(**{"attempt": "2", "tool_calls": "100"}),
        ],
        tool_calls=[("some_problem", "1", {"total": 100, "by_node": {"state": 100}})],
    )
    classify_run.report("run-x", str(run))
    out = capsys.readouterr().out

    assert "tool-call reconciliation: MISMATCH (json 100 vs csv 200)" in out


def test_report_prints_the_three_way_split_and_the_reasoned_only_rate(tmp_path, capsys):
    run = _run(
        tmp_path,
        [
            _attempt(**{"Diagnosis.success": "True"}),
            _attempt(**{"Diagnosis.success": "True"}),
            _attempt(),
            _attempt(**{"Diagnosis.submission": TEMPLATE}),
        ],
    )
    classify_run.report("run-x", str(run))
    out = capsys.readouterr().out

    assert "attempts                4" in out
    assert "diagnosed correctly     2  (50.0%)" in out
    assert "reasoned but wrong      1" in out
    assert "zero-evidence template  1" in out
    assert "diagnosis rate, reasoned attempts only  66.7%" in out


def test_report_prints_the_by_node_shares_with_the_planner_flagged(tmp_path, capsys):
    run = _run(
        tmp_path,
        [_attempt()],
        tool_calls=[
            (
                "some_problem",
                "1",
                {
                    "total": 10,
                    "by_node": {"hypothesis_gen_tools": 7, "metrics": 3},
                    "by_phase_and_node": {"diagnosis": {"hypothesis_gen_tools": 5, "metrics": 3}},
                },
            ),
        ],
    )
    classify_run.report("run-x", str(run))
    out = capsys.readouterr().out

    assert "hypothesis_gen_tools          7   70.0%  <- planner" in out
    assert "metrics                       3   30.0%" in out
    assert "planner share, diagnosis phase only: 62.5%" in out


def test_report_exits_nonzero_when_the_run_has_no_results(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        classify_run.report("empty-run", str(tmp_path))

    assert excinfo.value.code == 1
    assert "no brain_go_ALL_results.csv" in capsys.readouterr().err


def test_compare_prints_the_deltas_between_two_runs(tmp_path):
    before = classify_run.analyze(
        str(
            _run(
                tmp_path / "before",
                [
                    _attempt(**{"Diagnosis.success": "True"}),
                    _attempt(**{"Diagnosis.submission": TEMPLATE}),
                ],
            )
        )
    )
    after = classify_run.analyze(
        str(
            _run(
                tmp_path / "after",
                [
                    _attempt(**{"Diagnosis.success": "True"}),
                    _attempt(**{"Diagnosis.success": "True"}),
                ],
            )
        )
    )

    out = classify_run.compare("before", before, "after", after)

    assert "=== before  ->  after ===" in out
    assert "diagnosed correctly          1       2   +50.0pp" in out
    assert "zero-evidence template       1       0   -1" in out
    assert "The template count is the number to read first" in out


def test_main_prints_a_single_report_with_no_baseline(tmp_path, capsys):
    run = _run(tmp_path, [_attempt(**{"Diagnosis.success": "True"})])
    rc = classify_run.main(["classify_run.py", str(run)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "diagnosed correctly     1  (100.0%)" in out
    assert "->" not in out


def test_main_prints_the_comparison_when_a_baseline_is_given(tmp_path, capsys):
    before = _run(tmp_path / "before", [_attempt()])
    after = _run(tmp_path / "after", [_attempt(**{"Diagnosis.success": "True"})])

    rc = classify_run.main(["classify_run.py", str(after), str(before)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "=== before" in out
    assert "->  after ===" in out


def test_spread_reports_min_median_max(tmp_path):
    rows = [{"tool_calls_diagnosis": v} for v in ("30", "10", "20")]

    assert classify_run.spread(rows, "tool_calls_diagnosis") == "min 10 / med 20 / max 30"


def test_spread_is_n_a_with_no_parseable_values(tmp_path):
    rows = [{"tool_calls_diagnosis": ""}, {"tool_calls_diagnosis": "not_a_number"}]

    assert classify_run.spread(rows, "tool_calls_diagnosis") == "n/a"


def test_pct_formats_a_fraction_and_says_n_a_for_none():
    assert classify_run._pct(0.5) == "50.0%"
    assert classify_run._pct(None) == "n/a"


def test_delta_is_signed_percentage_points_and_n_a_when_either_side_is_none():
    assert classify_run._delta(0.5, 0.6) == "+10.0pp"
    assert classify_run._delta(0.6, 0.5) == "-10.0pp"
    assert classify_run._delta(None, 0.5) == "n/a"
    assert classify_run._delta(0.5, None) == "n/a"
