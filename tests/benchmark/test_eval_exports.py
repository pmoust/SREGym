"""run_shard.sh aborts a slot when the script that prints its environment fails.

run_slot used to load its environment with `eval "$(cmd)"`. eval returns the
status of the string it evaluates, not of the substitution, and both producers
(run_slot_env.sh and seed_slot.sh) print errors to stderr and nothing to stdout
when they fail, so a failed run evaluated "" and returned 0. In
run-20260928-0801-f50f50db tenant seeding failed, the `if ! eval` abort branch
did not run, and the slot died later in the edge install on an unbound
BRAIN_EDGE_API_KEY.

eval_exports() is extracted from run_shard.sh and run as shell, the technique
test_mcp_probe_gate.py uses: run_shard.sh is sourced by the bootstrap rather
than executed, so it cannot be run whole here.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUN_SHARD = ROOT / "scripts" / "benchmark" / "run_shard.sh"
RUN_SLOT_ENV = ROOT / "scripts" / "benchmark" / "run_slot_env.sh"

# What the bootstrap that sources run_shard.sh sets: -u and pipefail, no -e.
BOOTSTRAP_OPTS = "set -uo pipefail\n"

# A producer shaped like seed_slot.sh: diagnostics on stderr, exports on stdout
# only when it succeeds.
FAKE_PRODUCERS = """
fails() { echo "fake: seeding failed" >&2; return 3; }
succeeds() { printf 'export SEEDED_KEY=%q\\n' 'k e y'; }
"""


def _helper() -> str:
    text = RUN_SHARD.read_text()
    opener = "eval_exports() {"
    assert text.count(opener) == 1, "eval_exports is not defined exactly once in run_shard.sh"
    body = text.split(opener, 1)[1].split("\n}\n", 1)[0]
    return f"{opener}{body}\n}}\n"


def _bash(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", BOOTSTRAP_OPTS + _helper() + FAKE_PRODUCERS + script],
        capture_output=True,
        text=True,
        check=False,
    )


def test_old_idiom_misses_the_failure():
    """Pins the bug the helper exists for, so the harness itself is trusted."""
    r = _bash('if ! eval "$(fails)"; then echo caught; else echo missed; fi')
    assert r.stdout.strip() == "missed"


def test_failure_takes_the_abort_branch_with_the_command_status():
    r = _bash(
        "if ! eval_exports fails; then echo caught; else echo missed; fi\n"
        'eval_exports fails; echo "$?"\n'
        'echo "key=${SEEDED_KEY:-unset}"'
    )
    assert r.stdout.split() == ["caught", "3", "key=unset"]
    assert "fake: seeding failed" in r.stderr


def test_success_exports_to_the_caller_and_its_children():
    r = _bash("eval_exports succeeds || exit 9\nbash -c 'echo \"$SEEDED_KEY\"'")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "k e y\n"


def test_env_prefix_reaches_the_command():
    r = _bash('eval_exports env GREETING=hi bash -c \'echo "export GOT=$GREETING"\' || exit 9\necho "$GOT"')
    assert r.returncode == 0, r.stderr
    assert r.stdout == "hi\n"


@pytest.mark.parametrize("slot", ["", "x1", "501"])
def test_real_run_slot_env_failure_is_caught(tmp_path, slot):
    r = _bash(
        f'if ! eval_exports env SREGYM_SLOT_ROOT="{tmp_path}" "{RUN_SLOT_ENV}" "{slot}"; '
        "then echo caught; else echo missed; fi\n"
        'echo "port=${MCP_SERVER_PORT:-unset}"'
    )
    assert r.stdout.split() == ["caught", "port=unset"], r.stderr


def test_real_run_slot_env_success_sets_the_slot(tmp_path):
    r = _bash(
        f'eval_exports env SREGYM_SLOT_ROOT="{tmp_path}" "{RUN_SLOT_ENV}" 2 || exit 9\n'
        'echo "$KIND_CLUSTER_NAME $MCP_SERVER_PORT"'
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout == "sregym-2 9956\n"


def test_run_shard_has_no_eval_of_a_command_substitution():
    """A new `eval "$(...)"` or `eval $(...)` would reintroduce the bug."""
    offenders = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(RUN_SHARD.read_text().splitlines(), 1)
        if not line.lstrip().startswith("#") and re.search(r'\beval\s+"?\$\(', line)
    ]
    assert offenders == []
