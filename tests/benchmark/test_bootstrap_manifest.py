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


@pytest.mark.parametrize("name", ["smoke", "run"])
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


@pytest.mark.parametrize("scenario", ["smoke", "run"])
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
