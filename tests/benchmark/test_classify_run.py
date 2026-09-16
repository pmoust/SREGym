"""Failure-class splitting, validated against the runs it was derived from.

An attempt that gathers evidence and reasons to the wrong conclusion and an
attempt that gathers nothing and then submits a report saying so both score
zero. The first is a reasoning result; the second is plumbing eating the
answer. These tests pin that the two stay apart, because the whole value of the
split is that a score cannot tell them apart.

The submissions below are verbatim from archived runs, not invented. That is
deliberate. The first version of this script matched a clause of brain-go's
zero-evidence prose and the only test of that criterion asserted the marker was
a substring of a copy of the same prose kept in this file, so when brain-go
reworded the text in c17cfe9db the marker went dead and all 24 tests stayed
green for seven days. A test that compares a constant with a copy of itself
cannot notice anything.

What replaces it: the criterion is structural (see classify_run's docstring),
so a rewording cannot break it, and ZERO_EVIDENCE_NEW below is brain-go's
current wording rendered in the same shape - it fails against the old prose
marker and passes against the criterion in the script.

That removes one dependency on brain-go and does not remove the rest. The
script's docstring lists all three and says what each one does when it moves;
the tests here cannot see any of them, because every fixture in this file is a
copy taken at a point in time. Two of the three are guarded and the guards are
pinned below. The third, the evidence heading, is not: renaming it moves the
count by one or by nothing on the archives on file, so there is nothing to
assert on. Re-checking it means running the script over an archive with known
zero-evidence attempts and reading the count against the submissions by hand.
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

# Transcribed from run-20260908-0739-4fc0249e, where 18 of 63 attempts
# submitted this and nothing else, all the same 215 characters. Not a byte
# copy: the archive stores the trailing ellipsis double-encoded, as
# c3 a2 c2 80 c2 a6, and this is the single character that decodes to. Summary
# only, no Findings, no Theories, no Evidence, which is the part that matters.
ZERO_EVIDENCE_OLD = (
    "## Summary\n\nInvestigation of investigation could not produce a diagnosis "
    "because no observability signals (metrics, logs, traces) were collected. "
    "This is typically caused by missing data-source connections (e.g.…\n"
)

# The same report under brain-go's wording since c17cfe9db, from
# zeroEvidenceSummary's signal-bound branch in internal/chat/rci/diagnosis.go.
# The shape is unchanged and that is the point: the class is the shape, not the
# sentence. This string shares no clause with the one above.
ZERO_EVIDENCE_NEW = (
    "## Summary\n\nNo diagnosis could be produced for investigation: an observability "
    "signal was available for this account when the investigation started, but collection "
    "returned no evidence during this run. The cause is not recorded here, and the likely "
    "candidates differ in what to do next: a transient tool failure (retry), a connection "
    "that became unavailable after the run started (check connection health), or an "
    "incident with no signal in the queried window (widen the window or treat as "
    "no-fault).\n"
)

# Abridged from the 2026-09-15 control run, edge_request_filter_cpu_saturation
# attempt 3. The cluster this comes from is 16 of that run's 63 attempts, and
# it is the one a structural criterion is most at risk of swallowing: the
# summary says the investigation reached no conclusion, and the report is
# Summary plus Evidence with no Findings and no Theories. It is reasoned but
# wrong, and the populated Evidence section is what says so.
INCONCLUSIVE = (
    "## Summary\n\nInvestigation inconclusive: none of the evaluated hypotheses could be "
    "confirmed. Highest-probability unconfirmed theory: the frontend-proxy deployment was "
    "updated to replace the Envoy proxy with a single-threaded Perl WAF script whose regex "
    "backtracks catastrophically. (confidence: 8%, unverified).\n\n"
    "## Evidence\n\n"
    "- [e1] logs via frontend-proxy: Pod frontend-proxy-6c9669bb69-rnhxx logged 'edge request "
    "filter listening on 8080' at 08:48:01Z, 16 minutes before the hypothesis's stated change "
    "time, contradicting the deployment timestamp.\n"
    "- [e2] logs via frontend-proxy: Slow evaluations with candidateLength=5001 were already "
    "firing from 08:49:29Z, within the pre-change window.\n"
    "- [e3] logs via frontend-proxy: All pods in the ReplicaSet emit request_filter_eval "
    "events with the same rule, so the signature does not separate the revisions.\n"
)

# A run's ordinary reasoned answer, abridged from the same archive.
REASONED = (
    "## Summary\n\nA NetworkPolicy named deny-all-recommendation blocks all ingress and egress "
    "for the recommendation pods.\n\n"
    "## Findings\n\n- recommendation pods report 0/1 ready with connection timeouts to every peer.\n\n"
    "## Theories\n\n- The NetworkPolicy injected at fault time denies all traffic. — 94%\n\n"
    "## Evidence\n\n"
    "- [e1] state via recommendation: NetworkPolicy deny-all-recommendation selects app=recommendation "
    "with empty ingress and egress rules.\n"
    "- [e2] logs via recommendation: dial tcp 10.96.0.10:53 i/o timeout, repeating every 5s.\n"
)

# What the driver writes for an attempt whose signal workers ran, and for one
# where every diagnosis-phase call came from the planner. The second is the
# tool-call half of the zero-evidence class: on run-20260908-0739 all 18 of
# them look like this, with 71-111 diagnosis calls and not one worker call.
WORKERS_RAN = {
    "total": 88,
    "by_node": {"hypothesis_gen_tools": 52, "logs": 18, "state": 18},
    "by_phase_and_node": {
        "diagnosis": {"hypothesis_gen_tools": 43, "logs": 18, "state": 9},
        "mitigation": {"hypothesis_gen_tools": 9, "state": 9},
    },
    "by_phase_and_tool": {
        "diagnosis": {"exec_kubectl_cmd_safely": 47, "get_logs": 20, "get_alerts": 3},
        "mitigation": {"exec_kubectl_cmd_safely": 18},
    },
}

PLANNER_ONLY = {
    "total": 84,
    "by_node": {"hypothesis_gen_tools": 84},
    "by_phase_and_node": {"diagnosis": {"hypothesis_gen_tools": 74}, "mitigation": {"hypothesis_gen_tools": 10}},
    "by_phase_and_tool": {"diagnosis": {"exec_kubectl_cmd_safely": 74}, "mitigation": {"exec_kubectl_cmd_safely": 10}},
}

HEADER = [
    "Diagnosis.submission",
    "Diagnosis.success",
    "Mitigation.success",
    "attempt",
    "problem_id",
    "tool_calls",
    "tool_calls_diagnosis",
]


def _csv(path, attempts):
    lines = [",".join(HEADER)]
    for row in attempts:
        cells = []
        for key in HEADER:
            value = str(row.get(key, ""))
            needs_quotes = any(ch in value for ch in ',"\n')
            cells.append('"' + value.replace('"', '""') + '"' if needs_quotes else value)
        lines.append(",".join(cells))
    path.write_text("\n".join(lines) + "\n")


def _run(tmp_path, attempts, tool_calls=None, runtime_dupes=None, stray_dupes=None, resumed=None):
    """Write a run directory in the shape the harness archives.

    tool_calls entries are (problem, attempt, payload) and land on the
    canonical results path. runtime_dupes land under the hidden .runtime
    staging directory an unfinalized attempt leaves behind; stray_dupes land
    under a sibling of run_N on a fully visible path. Neither must be counted.
    resumed is a second, later-stamped aggregate CSV of the kind a resumed run
    writes while the first one stays on disk.
    """
    slot = tmp_path / "shard-0" / "slots" / "0"
    results = slot / "results" / "0908_0744" / "brain_go"
    results.mkdir(parents=True)
    _csv(results / "brain_go_ALL_results.csv", attempts)

    if resumed is not None:
        later = slot / "results" / "0908_0930" / "brain_go"
        later.mkdir(parents=True)
        _csv(later / "brain_go_ALL_results.csv", resumed)

    for problem, attempt, payload in tool_calls or []:
        run_dir = results / problem / f"run_{attempt}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "tool-calls.json").write_text(json.dumps(payload))

    for index, payload in enumerate(runtime_dupes or []):
        anon = slot / ".runtime" / "brain_go" / f"anon_{index}"
        anon.mkdir(parents=True)
        (anon / "tool-calls.json").write_text(json.dumps(payload))

    for index, payload in enumerate(stray_dupes or []):
        stray = results / "some_problem" / f"aborted_{index}"
        stray.mkdir(parents=True)
        (stray / "tool-calls.json").write_text(json.dumps(payload))

    return tmp_path


def _attempt(**over):
    row = {
        "Diagnosis.submission": REASONED,
        "Diagnosis.success": "False",
        "Mitigation.success": "False",
        "attempt": "1",
        "problem_id": "some_problem",
        "tool_calls": "100",
        "tool_calls_diagnosis": "60",
    }
    row.update(over)

    return row


def _classify(run):
    """classify() over a run directory, with the tool-call half of the criterion."""
    calls = classify_run.load_tool_calls(str(run))

    return classify_run.classify(classify_run.load_attempts(str(run)), calls["per_attempt"])


# --- the criterion ----------------------------------------------------------


@pytest.mark.parametrize("template", [ZERO_EVIDENCE_OLD, ZERO_EVIDENCE_NEW], ids=["old-wording", "current-wording"])
def test_a_zero_evidence_report_is_not_counted_as_reasoning(tmp_path, template):
    """The defect this script exists for, in both wordings brain-go has shipped.

    The current wording is the case that matters: it shares no clause with the
    old one, so the prose marker this replaced scores it as reasoned-but-wrong
    and reports a plumbing failure as a reasoning failure.
    """
    run = _run(
        tmp_path,
        [
            _attempt(**{"Diagnosis.submission": template}),
            _attempt(**{"attempt": "2"}),
        ],
        tool_calls=[
            ("some_problem", "1", PLANNER_ONLY),
            ("some_problem", "2", WORKERS_RAN),
        ],
    )
    summary = _classify(run)

    assert summary["boilerplate"] == 1
    assert summary["wrong"] == 1
    assert summary["correct"] == 0


def test_an_inconclusive_report_carrying_evidence_is_reasoned_not_zero_evidence(tmp_path):
    """16 of the 2026-09-15 control run's 63 attempts read like this: the
    summary says nothing was confirmed and the report has no Findings and no
    Theories, but the Evidence section is populated and the signal workers ran.
    Counting those as plumbing failures would be the mirror of the bug."""
    run = _run(
        tmp_path,
        [_attempt(**{"Diagnosis.submission": INCONCLUSIVE})],
        tool_calls=[("some_problem", "1", WORKERS_RAN)],
    )
    summary = _classify(run)

    assert summary["boilerplate"] == 0
    assert summary["wrong"] == 1


def test_an_evidenceless_report_is_reasoned_when_the_signal_workers_ran(tmp_path):
    """One half of the criterion is not enough. run-20260903-1259 archived two
    summary-only reports with no Evidence section that named the real cause and
    were graded correct, so an empty report alone does not make the class."""
    run = _run(
        tmp_path,
        [_attempt(**{"Diagnosis.submission": "## Summary\n\nA blocking init container stalls every pod.\n"})],
        tool_calls=[("some_problem", "1", WORKERS_RAN)],
    )
    summary = _classify(run)

    assert summary["boilerplate"] == 0
    assert summary["wrong"] == 1


def test_a_planner_only_attempt_that_reported_evidence_is_reasoned(tmp_path):
    """The other half. The 2026-09-15 control and treatment runs have one
    attempt each whose every diagnosis-phase call was issued by the planner,
    both carrying full evidence: by_node says who issued the call, not whether
    anything was read."""
    run = _run(
        tmp_path,
        [_attempt(**{"Diagnosis.submission": INCONCLUSIVE})],
        tool_calls=[("some_problem", "1", PLANNER_ONLY)],
    )
    summary = _classify(run)

    assert summary["boilerplate"] == 0
    assert summary["wrong"] == 1


def test_an_attempt_with_no_tool_calls_file_is_judged_on_its_report_alone(tmp_path):
    """Older archives carry no tool-calls.json. Falling back to the report
    over-counts the class rather than under-counting it, which is the direction
    that gets looked at."""
    run = _run(tmp_path, [_attempt(**{"Diagnosis.submission": ZERO_EVIDENCE_NEW})])
    summary = _classify(run)

    assert summary["boilerplate"] == 1


def test_evidence_items_counts_bullets_and_not_the_heading():
    """An Evidence heading with nothing under it is zero evidence, the same as
    no heading at all, and a bullet in another section does not count."""
    assert classify_run.evidence_items(INCONCLUSIVE) == 3
    assert classify_run.evidence_items(ZERO_EVIDENCE_OLD) == 0
    assert classify_run.evidence_items("## Summary\n\n- a finding\n\n## Evidence\n\nNone collected.\n") == 0
    assert classify_run.evidence_items("## Evidence\n\n- [e1] one\n\n## Recommended actions\n\n- do a thing\n") == 1


def test_evidence_items_reads_the_heading_and_list_shapes_brain_go_emits():
    """brain-go has two evidence renderers, "### Evidence" in diagnosis.go and
    the "## Evidence" every archive here carries, so the level cannot be
    pinned. Numbered items count as items, and the heading match does not
    depend on the case or on the space after the hashes."""
    assert classify_run.evidence_items("### Evidence\n\n- [e1] one\n") == 1
    assert classify_run.evidence_items("## Evidence\n\n1. [e1] one\n2. [e2] two\n") == 2
    assert classify_run.evidence_items("## evidence\n\n- [e1] one\n") == 1
    assert classify_run.evidence_items("##Evidence\n\n- [e1] one\n") == 1


def test_signal_worker_calls_ignores_the_planner_and_the_mitigation_phase():
    assert classify_run.signal_worker_calls(WORKERS_RAN) == 27
    assert classify_run.signal_worker_calls(PLANNER_ONLY) == 0
    assert classify_run.signal_worker_calls({}) == 0


def test_a_correct_diagnosis_wins_over_the_zero_evidence_match(tmp_path):
    """Order matters: a graded-correct attempt is correct even when its report
    carries no evidence, so the classes cannot double count."""
    run = _run(
        tmp_path,
        [_attempt(**{"Diagnosis.success": "True", "Diagnosis.submission": ZERO_EVIDENCE_NEW})],
        tool_calls=[("some_problem", "1", PLANNER_ONLY)],
    )
    summary = _classify(run)

    assert summary["correct"] == 1
    assert summary["boilerplate"] == 0
    assert summary["attempts"] == summary["correct"] + summary["wrong"] + summary["boilerplate"]


def test_the_reasoned_rate_excludes_zero_evidence_attempts(tmp_path):
    """Two of four graded correct, one of the rest zero-evidence: 2/4 overall
    and 2/3 among attempts that answered. The gap is what the plumbing costs."""
    run = _run(
        tmp_path,
        [
            _attempt(**{"Diagnosis.success": "True"}),
            _attempt(**{"Diagnosis.success": "True", "attempt": "2"}),
            _attempt(**{"attempt": "3"}),
            _attempt(**{"attempt": "4", "Diagnosis.submission": ZERO_EVIDENCE_NEW}),
        ],
        tool_calls=[("some_problem", "4", PLANNER_ONLY)],
    )
    summary = _classify(run)

    assert summary["diagnosis_rate"] == 0.5
    assert summary["diagnosis_rate_reasoned"] == 2 / 3


def test_an_attempt_that_mitigated_then_reported_no_evidence_is_counted(tmp_path):
    """The sharpest case, and 11 of run-20260908-0739's 18: the agent fixed the
    fault and then submitted a report saying it gathered nothing. Evidence that
    the reasoning worked and the reporting threw it away."""
    run = _run(
        tmp_path,
        [
            _attempt(**{"Diagnosis.submission": ZERO_EVIDENCE_NEW, "Mitigation.success": "True"}),
            _attempt(**{"attempt": "2", "Diagnosis.submission": ZERO_EVIDENCE_NEW, "Mitigation.success": "False"}),
        ],
        tool_calls=[("some_problem", "1", PLANNER_ONLY), ("some_problem", "2", PLANNER_ONLY)],
    )
    summary = _classify(run)

    assert summary["boilerplate"] == 2
    assert summary["boilerplate_mitigated"] == 1


def test_truthiness_survives_the_csv_round_trip(tmp_path):
    """The verdicts arrive as text, and "true"/"True"/"1" all mean the same
    thing. A bare bool() on the string "False" would count every attempt."""
    run = _run(
        tmp_path,
        [
            _attempt(**{"Diagnosis.success": "true"}),
            _attempt(**{"attempt": "2", "Diagnosis.success": "1"}),
            _attempt(**{"attempt": "3", "Diagnosis.success": "False"}),
            _attempt(**{"attempt": "4", "Diagnosis.success": ""}),
        ],
    )
    summary = _classify(run)

    assert summary["correct"] == 2


# --- loading ----------------------------------------------------------------


def test_tool_calls_are_attributed_by_node(tmp_path):
    """The planner share is the run's own answer to where the calls went, and it
    comes from the driver's count of its attempt rather than from Prometheus,
    which is process-wide across a box's three slots."""
    run = _run(tmp_path, [_attempt()], tool_calls=[("some_problem", "1", WORKERS_RAN)])
    calls = classify_run.load_tool_calls(str(run))

    assert calls["total"] == 88
    assert calls["by_node"]["hypothesis_gen_tools"] == 52
    assert calls["by_phase_node"]["diagnosis"]["logs"] == 18
    assert calls["by_phase_tool"]["diagnosis"]["get_logs"] == 20
    assert ("some_problem", "1") in calls["per_attempt"]


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
    calls = classify_run.load_tool_calls(str(run))

    assert calls["total"] == 10
    assert calls["node_total"] == 10
    assert calls["by_node"]["state"] == 5
    assert calls["by_node"]["logs"] == 5
    assert len(calls["per_attempt"]) == 2


def test_the_staging_copy_of_the_same_file_is_not_counted(tmp_path):
    """An attempt that never finalizes leaves its staging directory under
    .runtime, keyed by an opaque id. The 2026-09-15 control archive has 52 of
    those beside its 63 finalized attempts, so counting both would inflate
    every share."""
    run = _run(
        tmp_path,
        [_attempt()],
        tool_calls=[("some_problem", "1", {"total": 10, "by_node": {"state": 10}})],
        runtime_dupes=[{"total": 10, "by_node": {"state": 10}}],
    )
    calls = classify_run.load_tool_calls(str(run))

    assert calls["total"] == 10, "the .runtime staging copy must not be added to the total"
    assert calls["by_node"]["state"] == 10
    assert len(calls["per_attempt"]) == 1


def test_a_tool_calls_file_beside_run_n_on_a_visible_path_is_not_counted(tmp_path):
    """The path constraint has to do the work, not glob's dot-directory
    handling. This copy sits under results/<stamp>/brain_go/<problem>/ with
    nothing hidden anywhere in its path, so a pattern matching the file name
    anywhere counts it and the run_N pattern does not."""
    run = _run(
        tmp_path,
        [_attempt()],
        tool_calls=[("some_problem", "1", {"total": 10, "by_node": {"state": 10}})],
        stray_dupes=[{"total": 7, "by_node": {"state": 7}}],
    )
    calls = classify_run.load_tool_calls(str(run))

    assert calls["total"] == 10
    assert calls["by_node"]["state"] == 10
    assert len(calls["per_attempt"]) == 1


def test_tool_calls_are_found_when_the_tool_is_pointed_below_results(tmp_path):
    """argparse offers "a run directory, or its parent", and a run archive
    nests four levels deeper than that. Requiring a results/ segment below
    run_dir emptied the tool half of the criterion whenever someone pointed the
    tool at one timestamped directory instead of the shard root, and said "not
    recorded" with the files sitting right there. run_N is the segment that
    discriminates; results/ never added anything."""
    _run(tmp_path, [_attempt()], tool_calls=[("some_problem", "1", WORKERS_RAN)])
    inner = tmp_path / "shard-0" / "slots" / "0" / "results" / "0908_0744"
    calls = classify_run.load_tool_calls(str(inner))

    assert calls["files"] == 1
    assert calls["total"] == 88


def test_attempt_key_reads_problem_and_attempt_from_the_path(tmp_path):
    """The file holds counts and no identifier, so the path is the only place
    the attempt's identity lives."""
    key = classify_run.attempt_key(
        "/x/shard-0/slots/0/results/0908_0744/brain_go/network_policy_block/run_3/tool-calls.json"
    )

    assert key == ("network_policy_block", "3")
    assert classify_run.attempt_key("/x/.runtime/brain_go/anon_ab/tool-calls.json") is None
    assert classify_run.attempt_key("/x/results/0908_0744/brain_go/np/aborted_0/tool-calls.json") is None


def test_a_resumed_run_counts_each_attempt_once(tmp_path):
    """main.py seeds a resumed run with the rows it read from the previous CSV
    and then writes a fresh aggregate into a new timestamped directory while
    the old one stays on disk. The recursive glob sees both, so without a
    deduplication the resumed attempts are counted twice and every rate below
    them moves."""
    done = _attempt(**{"Diagnosis.success": "True"})
    run = _run(
        tmp_path,
        [done],
        resumed=[done, _attempt(**{"attempt": "2"})],
    )
    summary = _classify(run)

    assert summary["attempts"] == 2
    assert summary["correct"] == 1
    assert summary["diagnosis_rate"] == 0.5


def test_a_resumed_row_takes_its_later_copy(tmp_path):
    """Both copies are the same attempt, and the later directory is the one the
    run finished with, so a verdict that changed between them reads as the
    later verdict."""
    run = _run(
        tmp_path,
        [_attempt(**{"Diagnosis.success": "False"})],
        resumed=[_attempt(**{"Diagnosis.success": "True"})],
    )
    summary = _classify(run)

    assert summary["attempts"] == 1
    assert summary["correct"] == 1


def test_a_submission_that_is_not_valid_utf_8_still_loads(tmp_path):
    """The archives are not clean text. run-20260908-0739 stores its template's
    trailing ellipsis double-encoded, and a byte that decodes as nothing at all
    is the same class of damage. Reading without errors="replace", as
    score_run.py already does, turns one bad byte into a dead run."""
    run = _run(tmp_path, [_attempt()])
    csv_path = next(Path(run).rglob("brain_go_ALL_results.csv"))
    raw = csv_path.read_bytes()
    assert b"NetworkPolicy" in raw
    csv_path.write_bytes(raw.replace(b"NetworkPolicy", b"NetworkPolic\xe9"))

    rows = classify_run.load_attempts(str(run))

    assert len(rows) == 1
    assert "## Evidence" in rows[0]["Diagnosis.submission"]


def test_a_row_with_no_identity_is_kept_rather_than_collapsed(tmp_path):
    """Deduplication keys on (problem_id, attempt). A CSV missing either would
    otherwise collapse to a single row and report an attempt count of 1."""
    run = _run(
        tmp_path,
        [_attempt(**{"problem_id": "", "attempt": ""}), _attempt(**{"problem_id": "", "attempt": ""})],
    )

    assert len(classify_run.load_attempts(str(run))) == 2


def test_a_run_with_no_tool_call_files_still_classifies(tmp_path):
    """An older run, or one whose results were partly archived, must still give
    its failure classes rather than failing the whole report."""
    run = _run(tmp_path, [_attempt(**{"Diagnosis.submission": ZERO_EVIDENCE_OLD})])
    summary = _classify(run)
    calls = classify_run.load_tool_calls(str(run))

    assert summary["boilerplate"] == 1
    assert calls["total"] == 0
    assert not calls["by_node"] and not calls["by_phase_node"] and not calls["per_attempt"]


# --- the print layer: report(), compare(), main() ---------------------------
#
# The reconciliation checks above are only real if report() itself is exercised.
# A test that recomputes both totals by hand and compares them proves nothing
# about the code that decides what to print: hardcoding report()'s status to
# "ok" left every prior test green. These call report()/compare()/main() and
# assert on what they actually print, the way tests/benchmark/test_score_run.py
# asserts on render()'s output rather than on score()'s dict alone.


def test_report_prints_ok_when_both_reconciliations_hold(tmp_path, capsys):
    run = _run(
        tmp_path,
        [_attempt(**{"tool_calls": "10"})],
        tool_calls=[("some_problem", "1", {"total": 10, "by_node": {"state": 10}})],
    )
    classify_run.report("run-x", str(run))
    out = capsys.readouterr().out

    assert "tool-call files, csv vs json:       ok" in out
    assert "node attribution, by_node vs total: ok" in out


def test_report_says_tool_calls_are_unrecorded_rather_than_mismatched(tmp_path, capsys):
    """No tool-calls.json anywhere is unmeasured, not broken: every agent but
    brain_go writes none, and so does every archive older than the driver's
    counting. Calling that a mismatch would make the check cry wolf on the runs
    it has nothing to say about."""
    run = _run(tmp_path, [_attempt(**{"tool_calls": "100"})])
    rc = classify_run.main(["classify_run.py", str(run)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "tool calls: not recorded (no tool-calls.json under this run)" in out
    assert "MISMATCH" not in out


def test_report_prints_a_mismatch_naming_both_totals_when_a_file_is_missing(tmp_path, capsys):
    """The scenario the file check exists for: one attempt's tool-calls.json
    never made it into the archive, so the json total falls short of the csv
    total the driver recorded per attempt."""
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

    assert "tool-call files, csv vs json:       MISMATCH (json 100 vs csv 200)" in out


def test_report_flags_a_by_node_shortfall_the_csv_check_cannot_see(tmp_path, capsys):
    """An archive written before by_node existed: the total is right and the
    node table is absent or short. The csv column is a copy of that same total,
    so the file check still says ok while every share printed below is computed
    over 60 of 100 calls. Not a second independent count of the same calls -
    the driver increments both counters in one branch, so a live archive always
    reconciles."""
    run = _run(
        tmp_path,
        [_attempt(**{"tool_calls": "100"})],
        tool_calls=[("some_problem", "1", {"total": 100, "by_node": {"state": 40, "logs": 20}})],
    )
    classify_run.report("run-x", str(run))
    out = capsys.readouterr().out

    assert "tool-call files, csv vs json:       ok" in out
    assert "node attribution, by_node vs total: SHORT (by_node 60 of 100; shares below understate)" in out


def test_report_prints_the_three_way_split_and_the_reasoned_only_rate(tmp_path, capsys):
    run = _run(
        tmp_path,
        [
            _attempt(**{"Diagnosis.success": "True"}),
            _attempt(**{"attempt": "2", "Diagnosis.success": "True"}),
            _attempt(**{"attempt": "3"}),
            _attempt(**{"attempt": "4", "Diagnosis.submission": ZERO_EVIDENCE_NEW}),
        ],
        tool_calls=[("some_problem", "4", PLANNER_ONLY)],
    )
    classify_run.report("run-x", str(run))
    out = capsys.readouterr().out

    assert "attempts                4" in out
    assert "diagnosed correctly     2  (50.0%)" in out
    assert "reasoned but wrong      1" in out
    assert "zero-evidence template  1" in out
    assert "diagnosis rate, reasoned attempts only  66.7%" in out


def test_report_warns_when_the_class_covers_every_attempt(tmp_path, capsys):
    """A whole run in the class is possible and rare, so the report says to go
    and look. It is not a rename tripwire and does not claim to be: rewriting
    the evidence heading across the 2026-09-15 control run moves the count from
    0 to 1 of 63, and across the treatment run and run-20260914-1312 from 0 to
    0, none of which this condition sees."""
    run = _run(
        tmp_path,
        [
            _attempt(**{"Diagnosis.submission": ZERO_EVIDENCE_NEW}),
            _attempt(**{"attempt": "2", "Diagnosis.submission": ZERO_EVIDENCE_NEW}),
        ],
        tool_calls=[("some_problem", "1", PLANNER_ONLY), ("some_problem", "2", PLANNER_ONLY)],
    )
    classify_run.report("run-x", str(run))
    out = capsys.readouterr().out

    assert "zero-evidence template  2" in out
    assert "WARNING: every attempt classified zero-evidence" in out


def test_report_does_not_warn_when_some_attempt_reasoned(tmp_path, capsys):
    run = _run(
        tmp_path,
        [
            _attempt(**{"Diagnosis.submission": ZERO_EVIDENCE_NEW}),
            _attempt(**{"attempt": "2"}),
        ],
        tool_calls=[("some_problem", "1", PLANNER_ONLY), ("some_problem", "2", WORKERS_RAN)],
    )
    classify_run.report("run-x", str(run))

    assert "WARNING" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "by_node",
    [
        {"hypothesis_gen_tools_v2": 7, "metrics": 3},
        {"unknown": 10},
        {"hypothesis_gen_tools": 7, "unknown": 3},
    ],
    ids=["planner-node-renamed", "attribution-dropped", "attribution-partial"],
)
def test_report_warns_when_node_attribution_is_not_the_expected_shape(tmp_path, capsys, by_node):
    """Two ways brain-go can silently kill the tool half of the criterion, both
    reproduced on run-20260908-0739: renaming nodeGenTools out from under
    PLANNER_NODE, and dropping node attribution so the driver's omitempty
    fallback writes "unknown". Either takes that run's 18 zero-evidence
    attempts to 0 with no report-format change and nothing else moving. The
    third case is the partial one, where the planner is still named and only
    some calls lost their node, and it is the only case that pins the "unknown"
    half of the condition: the other two happen to have no planner in by_node
    either."""
    run = _run(
        tmp_path,
        [_attempt()],
        tool_calls=[("some_problem", "1", {"total": 10, "by_node": by_node})],
    )
    classify_run.report("run-x", str(run))
    out = capsys.readouterr().out

    assert "WARNING: node attribution is not the shape" in out
    assert "nodeGenTools" in out


def test_report_does_not_warn_on_ordinary_node_attribution(tmp_path, capsys):
    """The guard is one condition on by_node, so it has to stay quiet on every
    archive on file. WORKERS_RAN is run-20260908-0739's first attempt."""
    run = _run(tmp_path, [_attempt()], tool_calls=[("some_problem", "1", WORKERS_RAN)])
    classify_run.report("run-x", str(run))

    assert "node attribution is not the shape" not in capsys.readouterr().out


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
    assert "planner share of calls issued, diagnosis phase only: 62.5%" in out
    assert "which node issued the call, not what the call read" in out


def test_report_prints_the_diagnosis_phase_tool_breakdown(tmp_path, capsys):
    """The node table says which node issued a call, so a planner-issued
    get_logs lands under the planner and the planner share cannot be read as
    "the workers gathered nothing". The per-tool table answers that directly
    and is in the same file, so it is printed beside it."""
    run = _run(tmp_path, [_attempt()], tool_calls=[("some_problem", "1", WORKERS_RAN)])
    classify_run.report("run-x", str(run))
    out = capsys.readouterr().out

    assert "diagnosis-phase tool calls by tool:" in out
    assert "exec_kubectl_cmd_safely      47   67.1%" in out
    assert "get_logs                     20   28.6%" in out
    assert "get_alerts                    3    4.3%" in out


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
                    _attempt(**{"attempt": "2", "Diagnosis.submission": ZERO_EVIDENCE_NEW}),
                ],
                tool_calls=[("some_problem", "2", PLANNER_ONLY)],
            )
        )
    )
    after = classify_run.analyze(
        str(
            _run(
                tmp_path / "after",
                [
                    _attempt(**{"Diagnosis.success": "True"}),
                    _attempt(**{"attempt": "2", "Diagnosis.success": "True"}),
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


def test_main_returns_nonzero_when_a_reconciliation_fails(tmp_path, capsys):
    """A caller scripting this must not read an unreconciled run as clean. The
    report still prints: the failure says the shares are untrustworthy, not
    that they are missing."""
    run = _run(
        tmp_path,
        [_attempt(**{"tool_calls": "100"}), _attempt(**{"attempt": "2", "tool_calls": "100"})],
        tool_calls=[("some_problem", "1", {"total": 100, "by_node": {"state": 100}})],
    )
    rc = classify_run.main(["classify_run.py", str(run)])

    assert rc == 1
    assert "MISMATCH (json 100 vs csv 200)" in capsys.readouterr().out


def test_main_returns_nonzero_when_the_baseline_is_the_one_that_fails(tmp_path, capsys):
    """Both runs are reconciled, not just the one named first."""
    clean = _run(
        tmp_path / "after",
        [_attempt(**{"tool_calls": "10"})],
        tool_calls=[("some_problem", "1", {"total": 10, "by_node": {"state": 10}})],
    )
    broken = _run(
        tmp_path / "before",
        [_attempt(**{"tool_calls": "100"})],
        tool_calls=[("some_problem", "1", {"total": 100, "by_node": {"state": 60}})],
    )
    rc = classify_run.main(["classify_run.py", str(clean), str(broken)])

    assert rc == 1
    assert "SHORT (by_node 60 of 100" in capsys.readouterr().out


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
