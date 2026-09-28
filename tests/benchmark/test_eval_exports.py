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

The bootstrap template had the same flaw on its ssh-agent start. Its two
lines are extracted and run the same way, and a static guard scans every
benchmark script and terraform template for the idiom.
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


# `eval "$(cmd)"`, `eval $(cmd)`, `eval -- "$(cmd)"` and the backtick forms all
# return eval's status, never cmd's.
EVAL_OF_SUBSTITUTION = re.compile(r'\beval\s+(?:--\s+)?"?(?:\$\(|`)')

# NOFire's shell: the benchmark scripts and the terraform bootstrap template.
# Upstream test tooling under tests/e2e-testing-scripts is out of scope.
GUARDED_SHELL = sorted((ROOT / "scripts" / "benchmark").glob("*.sh")) + sorted(
    (ROOT / "terraform" / "templates").glob("*.tftpl")
)
BOOTSTRAP = ROOT / "terraform" / "templates" / "bootstrap.sh.tftpl"


def _eval_offenders(text: str) -> list[str]:
    # Drops comment lines and trailing comments, so documentation of the bug
    # does not count as the bug.
    return [
        f"{n}: {line.strip()}"
        for n, line in enumerate(text.splitlines(), 1)
        if EVAL_OF_SUBSTITUTION.search(re.sub(r"(^|\s)#.*", "", line))
    ]


@pytest.mark.parametrize(
    "line",
    [
        'eval "$(cmd)"',
        "eval $(cmd)",
        'eval -- "$(cmd)"',
        "eval -- $(cmd)",
        "eval `cmd`",
        'eval "`cmd`"',
        'x=1; eval "$(ssh-agent -s)" >/dev/null || exit 1',
        '  if ! eval "$(cmd)"; then',
    ],
)
def test_guard_catches_every_form(line):
    assert _eval_offenders(line) != []


@pytest.mark.parametrize(
    "line",
    [
        '# eval "$(cmd)" would hide the status',
        '    #   eval "$(scripts/benchmark/seed_slot.sh s 9955)"',
        'eval "$out"  # not eval "$(cmd)"',
        'eval "$eval_exports_out"',
        'eval "pid=\\$$var"',
        'eval_exports "$(cmd)"',
        "retrieval $(cmd)",
    ],
)
def test_guard_ignores_comments_and_evals_of_a_variable(line):
    assert _eval_offenders(line) == []


def test_guard_covers_the_bootstrap_and_run_shard():
    assert BOOTSTRAP in GUARDED_SHELL
    assert RUN_SHARD in GUARDED_SHELL


@pytest.mark.parametrize("path", GUARDED_SHELL, ids=lambda p: p.name)
def test_no_eval_of_a_command_substitution(path):
    """A new `eval "$(...)"` or one of its variants would reintroduce the bug."""
    assert _eval_offenders(path.read_text()) == []


def _bootstrap_ssh_agent_lines() -> str:
    lines = [line for line in BOOTSTRAP.read_text().splitlines() if "SSH_AGENT_ENV" in line]
    assert len(lines) == 2, lines
    return "\n".join(lines) + "\n"


# Stand-ins for the bootstrap's own terminate() and for ssh-agent.
FAKE_BOOTSTRAP = """
terminate() { echo "terminated: $1"; }
"""


def test_bootstrap_ssh_agent_failure_reaches_terminate():
    r = subprocess.run(
        [
            "bash",
            "-c",
            BOOTSTRAP_OPTS
            + FAKE_BOOTSTRAP
            + 'ssh-agent() { echo "fake: no agent" >&2; return 2; }\n'
            + _bootstrap_ssh_agent_lines()
            + "echo reached",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.stdout == "terminated: could not start an ssh-agent\n"
    assert r.returncode == 1


def test_bootstrap_ssh_agent_success_sets_the_socket():
    r = subprocess.run(
        [
            "bash",
            "-c",
            BOOTSTRAP_OPTS
            + FAKE_BOOTSTRAP
            + "ssh-agent() { echo 'SSH_AUTH_SOCK=/tmp/fake.sock; export SSH_AUTH_SOCK;'; echo 'echo Agent pid 7;'; }\n"
            + _bootstrap_ssh_agent_lines()
            + "bash -c 'echo \"sock=$SSH_AUTH_SOCK\"'",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout == "sock=/tmp/fake.sock\n"
