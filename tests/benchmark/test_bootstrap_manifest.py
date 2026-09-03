"""The bootstrap's manifest invocation, which fails silently when it breaks.

The manifest is a multi-line environment prefix: nine lines of VAR=value each
ending in a backslash, then the script. Drop one backslash and the statement
splits in two. The first half becomes bare assignments, which set shell
variables without exporting them, so the script runs as a child that sees none
of them and writes a manifest with empty values.

`bash -n` accepts that happily, because it is valid shell. It is just not the
shell anyone meant. This file exists because that exact edit was made and only
caught by running it.
"""

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "terraform" / "templates" / "bootstrap.sh.tftpl"

_VALUES = {
    "problem_set": "sregym-lite",
    "n_attempts": "3",
    "noise": "false",
    "problems": "network_policy_block wrong_dns_policy_astronomy_shop",
    "agent_timeout_seconds": "1800",
    "runs_per_instance": "3",
    "instance_type": "test-instance-type",
    "bucket": "sregym-bench-results-123456789012",
    "agent_model": "test-agent-model",
    "judge_model": "test-judge-model",
    "sregym_ref": "a" * 40,
    "brain_go_ref": "b" * 40,
}


def _manifest_block() -> str:
    """The manifest invocation, with template interpolations filled in."""
    tpl = TEMPLATE.read_text()
    after = tpl.split("# for why values and not just key names.\n", 1)[1]
    block = after.split("|| true\n", 1)[0] + "|| true"
    for key, value in _VALUES.items():
        block = block.replace(f"${{{key}}}", value)

    return block


def test_every_continuation_line_ends_in_a_backslash():
    """The invariant that broke. Cheap, and it names the line when it fails."""
    lines = [ln for ln in _manifest_block().split("\n") if ln.strip()]
    # The last line is the script call itself; everything before it continues.
    for i, line in enumerate(lines[:-1]):
        assert line.rstrip().endswith("\\"), f"line {i} does not continue: {line!r}"


def test_the_manifest_is_written_with_every_value_populated(tmp_path):
    """Runs it. An unexported assignment reads as empty, not as an error."""
    out = tmp_path / "manifest.txt"
    script = tmp_path / "run.sh"
    script.write_text(
        "set -u\n"
        "RUN_ID=run-test\n"
        "SHARD_INDEX=1\n"
        "SHARD_COUNT=3\n"
        f"WORKDIR={ROOT}\n"
        "BRAIN_GO_SHA=deadbeef\n"
        'BRAIN_ENV_EXTRA="CHAT_SERVICE_TURN_TIMEOUT=3600s"\n'
        f"MANIFEST={out}\n" + _manifest_block() + "\n"
    )
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert out.exists(), proc.stderr

    fields = dict(line.split("=", 1) for line in out.read_text().strip().split("\n") if "=" in line)
    for key in (
        "run_id",
        "shard_index",
        "shard_count",
        "problem_set",
        "n_attempts",
        "noise",
        "agent_timeout_seconds",
        "runs_per_instance",
        "instance_type",
        "results_bucket",
        "agent_model",
        "judge_model",
        "sregym_ref",
        "brain_go_ref",
        "stages",
    ):
        assert fields.get(key), f"{key} is empty; an assignment is not reaching the script"

    assert fields["problems"] == _VALUES["problems"]
    assert fields["stages"] == "diagnosis,mitigation"
    assert fields["problem_set"] == "sregym-lite"


def test_the_shard_handoff_is_declared_once():
    """Two handoff blocks shipped on main once, from a bad rebase resolution.

    Harmless at runtime, since export is additive and only one source line
    exists, but it wasted 30 lines of a capped user_data budget and made the
    file read as though the runner were invoked twice.
    """
    tpl = TEMPLATE.read_text()
    assert tpl.count("# --- shard runner") == 1
    assert tpl.count('. "$WORKDIR/scripts/benchmark/run_shard.sh"') == 1
    assert len(re.findall(r"^export PROBLEM_SET", tpl, re.M)) == 1


def _is_scenario(path: Path) -> bool:
    """A tfvars that defines a run, as opposed to a local or generated one.

    <something>.local.tfvars is gitignored, and experiment.sh will write one
    per ad-hoc run. Subjecting a throwaway file to the scenario checks below
    would fail the suite on a file nobody committed, so the exclusion lives
    here once rather than at each call site where the two could drift.
    """
    return not path.name.endswith(".local.tfvars")


def _scenarios() -> list[str]:
    """Every scenario file, so adding one does not quietly opt out of these.

    The list used to be ["smoke", "run"] spelled out here. A third scenario
    would then have been unchecked on both counts below, which is the failure
    these tests exist to prevent.
    """
    return sorted(p.stem for p in (ROOT / "terraform").glob("*.tfvars") if _is_scenario(p))


@pytest.mark.parametrize("name", _scenarios())
def test_each_scenario_names_only_real_problems(name):
    """A typo in a tfvars list is a hard stop on the instance, which is correct
    but expensive: it costs a boot to find out. Check it here instead."""
    src = (ROOT / "sregym" / "conductor" / "problem_sets.py").read_text()
    known = set(re.findall(r'"([a-z0-9_]+)"', src.split("SREGYM_LITE_PROBLEMS = (")[1].split(")")[0]))

    tfvars = (ROOT / "terraform" / f"{name}.tfvars").read_text()
    block = re.search(r"^problems\s*=\s*\[(.*?)\]", tfvars, re.S | re.M)
    if block is None:  # the scored run leaves it unset on purpose
        return

    named = re.findall(r'"([a-z0-9_]+)"', block.group(1))
    assert named, "problems is present but empty; omit it instead"
    unknown = [p for p in named if p not in known]
    assert not unknown, f"{name}.tfvars names ids that are not in sregym-lite: {unknown}"


def _tfvar(scenario: str, name: str) -> str | None:
    """The scalar a scenario assigns, or None when it leaves the default."""
    src = (ROOT / "terraform" / f"{scenario}.tfvars").read_text()
    found = re.search(rf"^{name}\s*=\s*(\S+)", src, re.M)

    return found.group(1) if found else None


def _lite_problem_count() -> int:
    src = (ROOT / "sregym" / "conductor" / "problem_sets.py").read_text()

    return len(re.findall(r'"([a-z0-9_]+)"', src.split("SREGYM_LITE_PROBLEMS = (")[1].split(")")[0]))


def test_the_scored_run_is_exactly_lite_width_and_splits_evenly():
    """Both splits are static modulo, so a width that does not divide is silent.

    instance_count x runs_per_instance above the problem count leaves slots
    that draw nothing and log "drew no problems, skipping", which reads like a
    clean short run. Below it, slots go two problems deep and the run takes as
    long as the deepest slot. Neither fails, and neither is visible in the
    results.
    """
    problems = _lite_problem_count()
    instances = int(_tfvar("run", "instance_count"))
    slots = int(_tfvar("run", "runs_per_instance"))

    assert instances * slots == problems, (
        f"{instances} x {slots} = {instances * slots} slots for {problems} problems: "
        "over-provisioned slots skip, under-provisioned slots go deep"
    )
    assert problems % instances == 0, (
        f"{problems} problems across {instances} instances is uneven, so the run is as slow as the largest shard"
    )


@pytest.mark.parametrize("scenario", _scenarios())
def test_no_scenario_asks_for_a_turn_timeout_brain_go_will_not_honour(scenario):
    """The bootstrap derives CHAT_SERVICE_TURN_TIMEOUT as twice this value.

    brain-go's HTTP write timeout is 90 minutes and severs the connection
    whatever the turn timeout says, so anything above 2700s here configures a
    turn timeout that is quietly capped. The symptom is a turn dying at 90
    minutes with a client-side disconnect rather than at the timeout anyone
    set.
    """
    ceiling = 90 * 60 // 2
    configured = _tfvar(scenario, "agent_timeout_seconds")
    if configured is None:  # inherits the variables.tf default
        src = (ROOT / "terraform" / "variables.tf").read_text()
        block = src.split('variable "agent_timeout_seconds"', 1)[1]
        configured = re.search(r"default\s*=\s*(\d+)", block).group(1)

    assert int(configured) <= ceiling, (
        f"{scenario}: agent_timeout_seconds={configured} derives a "
        f"{int(configured) * 2}s turn timeout, past brain-go's 5400s write timeout"
    )


def test_every_template_variable_is_modelled_for_the_user_data_measurement():
    """measure_user_data models the template's inputs and drifts from it silently.

    Adding a variable to the templatefile block in compute.tf without adding it
    here makes launch.sh exit before it plans, with "template needs values not
    modelled here". That is the right failure, but it fires on the operator at
    launch time rather than here, and it already cost one launch attempt.
    """
    spec = importlib.util.spec_from_file_location(
        "measure_user_data",
        ROOT / "scripts" / "benchmark" / "measure_user_data.py",
    )
    measure_user_data = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(measure_user_data)

    referenced = set(re.findall(r"\$\{(\w+)\}", TEMPLATE.read_text()))
    modelled = set(measure_user_data.FIXED) | set(measure_user_data.SCENARIOS["typical"])

    assert not referenced - modelled, (
        f"bootstrap.sh.tftpl uses {sorted(referenced - modelled)}, which measure_user_data "
        "does not model, so launch.sh will refuse to plan"
    )


def test_a_narrowed_scenario_has_a_slot_for_every_problem_it_names():
    """A scenario that asks for more slots than problems wastes the difference.

    Slots are handed problems by index modulo runs_per_instance, so a slot that
    draws none logs "drew no problems, skipping" and exits. That reads like a
    clean short run. It matters most for smoke7, whose entire purpose is to put
    a named number of slots under load: if its problem list and its slot count
    ever drift apart, it silently tests fewer slots than it claims to.
    """
    for name in _scenarios():
        src = (ROOT / "terraform" / f"{name}.tfvars").read_text()
        block = re.search(r"^problems\s*=\s*\[(.*?)\]", src, re.S | re.M)
        if block is None:  # the scored run runs the whole set
            continue

        named = len(re.findall(r'"([a-z0-9_]+)"', block.group(1)))
        instances = int(_tfvar(name, "instance_count") or 1)
        slots = int(_tfvar(name, "runs_per_instance") or 1)

        assert instances * slots <= named, (
            f"{name}.tfvars: {instances} x {slots} = {instances * slots} slots for "
            f"{named} problem(s), so {instances * slots - named} slot(s) draw nothing"
        )


def test_every_scenario_file_on_disk_is_tracked_by_git():
    """A scenario that is not committed is a scenario nobody else has.

    This has now happened twice, both times because .gitignore's blanket
    terraform/*.tfvars rule needed an exception per file: once shipping
    launch.sh with no scenarios at all, and again on smoke7, whose tests
    merged while the file they describe sat untracked on one disk. Both passed
    their own gate, because the tests below discover scenarios from the
    filesystem and the author's filesystem had the file.

    So assert the filesystem and the index agree. Discovery is what makes the
    other scenario tests robust and is also what makes them blind to this.
    """
    on_disk = {p.name for p in (ROOT / "terraform").glob("*.tfvars") if _is_scenario(p)}
    tracked = set(
        subprocess.run(
            ["git", "ls-files", "--", "terraform/*.tfvars"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    )
    tracked = {Path(t).name for t in tracked}

    assert not on_disk - tracked, (
        f"untracked scenario file(s): {sorted(on_disk - tracked)}. "
        "Commit them, or name a personal override <something>.local.tfvars"
    )


def test_every_scenario_can_finish_inside_its_runtime_cap():
    """The backstop must not fire before the work it is backing up can finish.

    Attempts are serial inside a slot, so a slot's floor is its problem depth
    times n_attempts times the agent cap, and the cap has to clear that plus
    setup. run-20260902-1556 got this wrong: two problems in one slot at the
    1800s agent cap is 60 minutes of budget alone, against a one-hour backstop.
    It diagnosed its second problem correctly and was killed four minutes later
    before the verdict was graded, scoring 1 of 2 and reading like a harness
    failure rather than a configuration one.

    The setup allowance is measured, not guessed: 13 minutes on that one-slot
    run, and 16 to 58 minutes per slot on the nine-slot run-20260902-0606,
    where the tail is a deploy that failed and retried (#53). 30 minutes is the
    middle of that and is deliberately not the worst case, since a cap sized
    for the worst case stops being a backstop.
    """
    setup_allowance = 30 * 60
    problems_in_set = _lite_problem_count()

    for name in _scenarios():
        cap_hours = _tfvar(name, "max_runtime_hours")
        if cap_hours is None:  # inherits the generous variables.tf default
            continue

        src = (ROOT / "terraform" / f"{name}.tfvars").read_text()
        block = re.search(r"^problems\s*=\s*\[(.*?)\]", src, re.S | re.M)
        problems = len(re.findall(r'"([a-z0-9_]+)"', block.group(1))) if block else problems_in_set

        slots = int(_tfvar(name, "instance_count") or 1) * int(_tfvar(name, "runs_per_instance") or 1)
        attempts = int(_tfvar(name, "n_attempts") or 1)
        agent_cap = _tfvar(name, "agent_timeout_seconds")
        if agent_cap is None:
            variables = (ROOT / "terraform" / "variables.tf").read_text()
            agent_cap = re.search(
                r"default\s*=\s*(\d+)", variables.split('variable "agent_timeout_seconds"', 1)[1]
            ).group(1)

        depth = -(-problems // slots)  # ceiling division: the deepest slot
        needed = depth * attempts * int(agent_cap) + setup_allowance

        assert int(cap_hours) * 3600 >= needed, (
            f"{name}.tfvars: max_runtime_hours={cap_hours} is {int(cap_hours) * 3600}s, but the "
            f"deepest slot needs {depth} x {attempts} x {agent_cap}s plus {setup_allowance}s of "
            f"setup = {needed}s"
        )


def test_brain_go_logs_are_dumped_and_shipped():
    """Both halves, because either alone leaves the archive harness-side only.

    The dump has to run (the logs live in the container runtime and die with
    the box) and the file has to reach the sync list (a file written to
    /var/log and never uploaded is the same as no file). run-20260902-1556 had
    neither, so whether brain-go parsed the incident window it was sent is
    unanswerable from that run's archive (#52).
    """
    tpl = TEMPLATE.read_text()
    sync = tpl.split("sync_logs() {", 1)[1].split("\n}", 1)[0]

    assert "dump_brain_go_logs.sh" in sync, "nothing dumps brain-go's logs"
    assert "/var/log/sregym-brain-go.log" in sync.split("for f in", 1)[1], (
        "the dumped log is not in the list sync_logs uploads"
    )
    assert (ROOT / "scripts" / "benchmark" / "dump_brain_go_logs.sh").stat().st_mode & 0o111, (
        "the dump script is not executable, and sync_logs guards on -x"
    )


def test_the_results_bucket_name_does_not_move_with_the_run():
    """A bucket named from the run id is replaced on every plan.

    That is #48. local.run_id derives from timestamp(), so "${local.name}-results"
    renamed the bucket on each plan, and a rename is a replace: every apply
    tried to delete the previous run's bucket and failed on BucketNotEmpty,
    because versioning leaves object versions behind. It cost a manual paged
    purge before every launch, and once cost a run's logs, purged to unblock the
    next apply before they had been downloaded.

    Checked at the source rather than by planning, because planning needs
    credentials and this needs to fail in CI.
    """
    s3 = (ROOT / "terraform" / "s3.tf").read_text()
    bucket_line = next(
        line for line in s3.splitlines() if re.match(r"\s*bucket\s*=\s*\"", line.strip()) or "bucket  " in line
    )

    assert "local.name" not in bucket_line, (
        f"the bucket name interpolates local.name, which carries the run id: {bucket_line.strip()}"
    )
    assert "run_id" not in bucket_line, f"the bucket name interpolates the run id: {bucket_line.strip()}"


def test_the_bucket_terraform_creates_keeps_its_protections():
    """Every bucket resource is gated on var.results_bucket being empty.

    So handing the job to an existing bucket, which is what #48 originally
    proposed, drops the public-access block, the encryption default, the
    versioning and the lifecycle expiry onto whoever created it by hand. This
    pins that terraform still owns all four, so a later change cannot quietly
    swap ownership without noticing what goes with it.
    """
    s3 = (ROOT / "terraform" / "s3.tf").read_text()

    for resource in (
        "aws_s3_bucket_public_access_block",
        "aws_s3_bucket_server_side_encryption_configuration",
        "aws_s3_bucket_versioning",
        "aws_s3_bucket_lifecycle_configuration",
    ):
        assert f'resource "{resource}" "results"' in s3, f"{resource} is no longer managed here"


def _template_vars_passed_by_terraform() -> set[str]:
    """The names compute.tf actually hands templatefile().

    Read from the config rather than listed here, so adding a variable to the
    templatefile block does not need a matching edit in this file.
    """
    compute = (ROOT / "terraform" / "compute.tf").read_text()
    block = compute.split('bootstrap.sh.tftpl", {', 1)[1].split("\n  }))", 1)[0]

    return set(re.findall(r"^\s*(\w+)\s*=", block, re.M))


def test_every_interpolation_in_the_template_is_a_variable_terraform_passes():
    """A shell brace expansion in this file is a terraform interpolation.

    `local outcome="${2:-failure}"` broke the plan with "Template interpolation
    doesn't expect a colon at this location", after passing bash -n, ruff,
    terraform fmt, terraform validate and a unit test that rendered the file
    with a Python regex. None of those parse the template the way terraform
    does, and the failure surfaced only at apply time on a launch.

    So check it here: every ${...} must name a variable the templatefile call
    supplies. A shell construct needs $${...} to escape, or rewriting to avoid
    braces, which is what that line did.
    """
    known = _template_vars_passed_by_terraform()
    assert known, "could not read the templatefile variable list from compute.tf"

    offenders = []
    for number, line in enumerate(TEMPLATE.read_text().split("\n"), 1):
        # (?<!\$) so an escaped $${...} is not reported: that form is already
        # a literal by the time terraform is done with it.
        for found in re.finditer(r"(?<!\$)\$\{([^}]*)\}", line):
            if found.group(1).strip() not in known:
                offenders.append(f"{TEMPLATE.name}:{number}: {found.group(0)}")

    assert not offenders, "terraform will try to interpolate these:\n  " + "\n  ".join(offenders)


def test_a_generated_local_tfvars_is_not_treated_as_a_scenario(tmp_path, monkeypatch):
    """experiment.sh will write one of these per ad-hoc run.

    They are gitignored and disposable, so the scenario checks must not see
    them: an ad-hoc run at 24 slots for 7 problems, or at an agent timeout
    above the derived-turn-timeout ceiling, is the operator's business and not
    a repo invariant. Without the exclusion the first generated run fails the
    suite on a file nobody committed.
    """
    generated = ROOT / "terraform" / "experiment.local.tfvars"
    assert not generated.exists(), "a real one is in the way; this test would be meaningless"

    generated.write_text('instance_count = 1\nruns_per_instance = 9\nproblems = ["network_policy_block"]\n')
    try:
        assert "experiment.local" not in _scenarios()
        assert not _is_scenario(generated)
    finally:
        generated.unlink()
