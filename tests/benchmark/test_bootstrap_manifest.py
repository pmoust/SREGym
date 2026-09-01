"""The bootstrap's manifest invocation, which fails silently when it breaks.

The manifest is a multi-line environment prefix: eight lines of VAR=value each
ending in a backslash, then the script. Drop one backslash and the statement
splits in two. The first half becomes bare assignments, which set shell
variables without exporting them, so the script runs as a child that sees none
of them and writes a manifest with empty values.

`bash -n` accepts that happily, because it is valid shell. It is just not the
shell anyone meant. This file exists because that exact edit was made and only
caught by running it.
"""

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
