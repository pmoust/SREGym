"""The generated conductor tasklist, and what the manifest then records of it.

The conductor grades both diagnosis and mitigation for any problem
sregym/conductor/tasklist.yml does not list, and says so only in a log line.
So the interesting cases here are the silent ones: a problem missing from the
file, a stage value that never reaches the file at all, and a manifest that
keeps saying both stages ran after the file has narrowed them.
"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WRITE_TASKLIST = ROOT / "scripts" / "benchmark" / "write_tasklist.py"
WRITE_MANIFEST = ROOT / "scripts" / "benchmark" / "write_run_manifest.sh"
RUN_SHARD = ROOT / "scripts" / "benchmark" / "run_shard.sh"

PROBLEMS = "network_policy_block kafka_producer_leak wrong_dns_policy_astronomy_shop"


def _generate(tmp_path, problems: str = PROBLEMS, stages: str | None = None):
    """Run write_tasklist.py the way run_shard.sh does, into a temp WORKDIR."""
    target = tmp_path / "sregym" / "conductor" / "tasklist.yml"
    env = dict(os.environ)
    env["TASKLIST_PATH"] = str(target)
    env["PROBLEMS"] = problems
    if stages is None:
        env.pop("SREGYM_STAGES", None)
    else:
        env["SREGYM_STAGES"] = stages

    proc = subprocess.run(
        ["uv", "run", "--quiet", "python", str(WRITE_TASKLIST)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    return proc, target


def test_the_default_is_diagnosis_only(tmp_path):
    """Nothing set must mean diagnosis. The whole change is that default."""
    proc, target = _generate(tmp_path)

    assert proc.returncode == 0, proc.stderr
    problems = yaml.safe_load(target.read_text())["all"]["problems"]
    assert problems
    assert all(stages == ["diagnosis"] for stages in problems.values()), problems


def test_every_problem_the_shard_runs_is_listed(tmp_path):
    # A problem absent from the file is not an error anywhere downstream: the
    # conductor grades both its stages and logs a warning. This is the only
    # place that can catch it.
    _, target = _generate(tmp_path)

    problems = yaml.safe_load(target.read_text())["all"]["problems"]
    assert set(problems) == set(PROBLEMS.split())


def test_both_stages_can_still_be_asked_for(tmp_path):
    proc, target = _generate(tmp_path, stages="diagnosis,mitigation")

    assert proc.returncode == 0, proc.stderr
    problems = yaml.safe_load(target.read_text())["all"]["problems"]
    assert all(stages == ["diagnosis", "mitigation"] for stages in problems.values()), problems


@pytest.mark.parametrize(
    "stages",
    ["mitigation", "diagnosis, mitigation", "mitigation,diagnosis", "", "both", "diagnosis,mitigation,teardown"],
)
def test_an_unaccepted_stage_value_writes_nothing_and_fails(tmp_path, stages):
    """Reject rather than fall through.

    A value the generator half-accepted, or accepted into a file the conductor
    then rejects, costs a whole run: no tasklist means the conductor's
    both-stage default, at twice the token cost, looking like a normal run.
    """
    proc, target = _generate(tmp_path, stages=stages)

    assert proc.returncode != 0
    assert "SREGYM_STAGES" in proc.stderr
    assert not target.exists()


def test_an_empty_problem_list_fails(tmp_path):
    proc, target = _generate(tmp_path, problems="   ")

    assert proc.returncode != 0
    assert not target.exists()


def test_the_stages_written_are_an_ordered_subset_the_conductor_accepts(tmp_path):
    """The conductor raises on an out-of-order or unknown stage name.

    Checked against the conductor's own predicate rather than a copy of the
    rule, so a change there fails here instead of at run time on a box.
    """
    from sregym.conductor.utils import is_ordered_subset

    for stages in ("diagnosis", "diagnosis,mitigation"):
        _, target = _generate(tmp_path, stages=stages)
        problems = yaml.safe_load(target.read_text())["all"]["problems"]
        for listed in problems.values():
            assert is_ordered_subset(listed, ["diagnosis", "mitigation"]), listed


def test_the_shard_hard_stops_on_a_rejected_stage_value(tmp_path):
    """run_shard.sh must terminate, not carry on without a tasklist.

    Runs the real block out of run_shard.sh against a stub terminate, the
    technique test_mcp_probe_gate.py uses: `bash -n` accepts a missing
    hard stop happily, and the failure it would cause is a full-price run
    that reads as a normal one.
    """
    text = RUN_SHARD.read_text()
    opener = "# --- stages ---"
    assert text.count(opener) == 1, "the stages block is not defined exactly once in run_shard.sh"
    block = text.split(opener, 1)[1].split("# --- run ---", 1)[0]

    script = tmp_path / "stages.sh"
    script.write_text(
        "set -uo pipefail\n"
        'terminate() { echo "TERMINATED: $1"; }\n'
        f"WORKDIR={ROOT}\n"
        'PROBLEMS="network_policy_block"\n'
        "SREGYM_STAGES=mitigation\n" + block
    )
    proc = subprocess.run(["bash", str(script)], cwd=ROOT, capture_output=True, text=True)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "TERMINATED" in proc.stdout, proc.stdout + proc.stderr


def _manifest(tmp_path, workdir: Path, *, refresh: bool = False) -> dict[str, str]:
    out = tmp_path / "manifest.txt"
    env = dict(os.environ)
    env["PROBLEM_SET"] = "sregym-lite"
    env["PROBLEMS_OVERRIDE"] = ""
    env["WORKDIR"] = str(workdir)
    env.pop("SREGYM_TASKLIST", None)
    args = ["bash", str(WRITE_MANIFEST)]
    if refresh:
        args.append("--stages")
    proc = subprocess.run([*args, str(out)], cwd=ROOT, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    return dict(line.split("=", 1) for line in out.read_text().strip().split("\n") if "=" in line)


def test_the_manifest_records_the_generated_tasklist(tmp_path):
    """stages=diagnosis and a hash of the file that made it so."""
    _generate(tmp_path)
    fields = _manifest(tmp_path, tmp_path)

    assert fields["stages_source"] == "tasklist.yml"
    assert fields["stages"] == "diagnosis"
    assert len(fields["tasklist_sha256"]) == 64


def test_the_generated_header_is_not_read_as_a_stage(tmp_path):
    """The file carries a comment header saying where it came from.

    The manifest derives stages by grepping the file for stage names, so a
    header that mentions one would be read as that stage having run.
    """
    _, target = _generate(tmp_path)
    target.write_text("# diagnosis and mitigation are both stage names\n" + target.read_text())

    assert _manifest(tmp_path, tmp_path)["stages"] == "diagnosis"


def test_the_refresh_replaces_the_stage_lines_it_does_not_append_them(tmp_path):
    """The bootstrap writes the manifest before the tasklist exists.

    So the first write records the conductor's both-stage default, and
    run_shard.sh rewrites those lines once it has generated the file. A
    second copy of the lines rather than a replacement would leave the
    manifest saying both things at once.
    """
    absent = tmp_path / "no-workdir-here"
    before = _manifest(tmp_path, absent)
    assert before["stages"] == "diagnosis,mitigation"
    assert before["stages_source"] == "conductor-default-no-tasklist"
    assert "tasklist_sha256" not in before

    _generate(tmp_path)
    after = _manifest(tmp_path, tmp_path, refresh=True)

    assert after["stages"] == "diagnosis"
    assert after["stages_source"] == "tasklist.yml"
    assert len(after["tasklist_sha256"]) == 64
    # Every other field survives the rewrite: the refresh is not a second
    # manifest, and losing provenance to gain one line is not a trade.
    for key, value in before.items():
        if key in ("stages", "stages_source"):
            continue
        assert after[key] == value, key

    text = (tmp_path / "manifest.txt").read_text()
    assert text.count("\nstages=") == 1
    assert text.count("\nstages_source=") == 1


def test_the_refresh_fails_loudly_on_a_manifest_that_is_not_there(tmp_path):
    proc = subprocess.run(
        ["bash", str(WRITE_MANIFEST), "--stages", str(tmp_path / "nothing.txt")],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert proc.returncode != 0
    assert "does not exist" in proc.stderr
