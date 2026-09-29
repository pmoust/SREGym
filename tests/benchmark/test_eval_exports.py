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


# `eval "$(cmd)"`, `eval $(cmd)`, `eval -- "$(cmd)"`, the backtick forms and a
# substitution anywhere in eval's arguments (`eval "export X=$(cmd)"`, which
# returns export's 0) all return eval's status, never cmd's. It is matched
# against the output of _code_lines, where eval must be in command position
# and `$((` arithmetic is not a substitution. Command position includes a case
# arm after its `)`, and eval may follow assignment words and the `command`,
# `builtin` and `time` prefixes.
EVAL_OF_SUBSTITUTION = re.compile(
    r"(?:(?:^|[;&|()!{])\s*|\b(?:if|then|else|elif|do|while|until)\s+)"
    r"(?:\w+=\S*\s+|(?:command|builtin|time)\s+)*"
    r"eval\s+[^;&|]*?(?:\$\((?!\()|`)"
)

# NOFire's shell: the benchmark scripts and the terraform bootstrap template.
# Upstream test tooling under tests/e2e-testing-scripts is out of scope.
GUARDED_SHELL = sorted((ROOT / "scripts" / "benchmark").glob("*.sh")) + sorted(
    (ROOT / "terraform" / "templates").glob("*.tftpl")
)
BOOTSTRAP = ROOT / "terraform" / "templates" / "bootstrap.sh.tftpl"

# Characters that end a word, so a `#` after one of them starts a comment.
WORD_BREAKS = " \t;&|()<>"


def _code_lines(text: str):
    """Yield (first line number, code) for each logical line of shell.

    A backslash-newline joins a line to the next. Comments are dropped, and a
    `#` inside quotes is not a comment. Single-quoted text, escaped characters
    and double-quoted text other than `$`, `(` and backticks become `_`, so an
    eval or a separator inside a string is not read as code.

    Quote state does not carry past the end of a logical line, and heredoc
    bodies are scanned as code, so a quoted string spanning lines or a heredoc
    body is misread. test_no_eval_of_a_command_substitution shows that yields
    no false positive in any guarded file today.
    """
    buf: list[str] = []
    start = 0
    quote = ""
    for n, line in enumerate(text.splitlines(), 1):
        if not buf:
            start = n
        joined = False
        i = 0
        while i < len(line):
            c = line[i]
            if quote == "'":
                buf.append(c if c == "'" else "_")
                quote = "" if c == "'" else quote
            elif c == "\\":
                if i + 1 == len(line):
                    joined = True
                    break
                buf.append("__")
                i += 1
            elif quote == '"':
                buf.append(c if c in '"$(`' else "_")
                quote = "" if c == '"' else quote
            elif c == "#" and (not buf or buf[-1] in WORD_BREAKS):
                break
            else:
                quote = c if c in "'\"" else ""
                buf.append(c)
            i += 1
        if joined:
            buf.append(" ")
            continue
        yield start, "".join(buf)
        buf, quote = [], ""
    if buf:
        yield start, "".join(buf)


def _eval_offenders(text: str) -> list[str]:
    lines = text.splitlines()
    return [f"{n}: {lines[n - 1].strip()}" for n, code in _code_lines(text) if EVAL_OF_SUBSTITUTION.search(code)]


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
        'eval "export X=$(cmd)"',
        'x="a #b"; eval "$(cmd)"',
        'eval \\\n  "$(cmd)"',
        'eval "x=1; y=$(cmd)"',
        'case x in y) eval "$(cmd)";; esac',
        'X=1 eval "$(cmd)"',
        'X="a b" Y= eval "$(cmd)"',
        'command eval "$(cmd)"',
        'builtin eval "$(cmd)"',
        'time eval "$(cmd)"',
        'if ! X=1 command eval "$(cmd)"; then',
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
        'echo "do not eval $(cmd)"',
        "eval 'x=1' # $(cmd)",
        'eval "n=$((1 + 2))"',
        '# a comment ending in a backslash \\\neval "$out"',
        'eval "$out" \\\n  # not eval "$(cmd)"',
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


SSH_AGENT_CAPTURE = "SSH_AGENT_ENV=$(ssh-agent -s) || "
SSH_AGENT_EVAL = 'eval "$SSH_AGENT_ENV" >/dev/null'


def _bootstrap_ssh_agent_lines() -> str:
    """The capture line and the eval line right after it, both unindented.

    Unindented stands in for top level: the template indents the body of
    every if, loop and function, so a code line at column 0 is outside all of
    them. Heredoc bodies also sit at column 0, which this does not tell apart.
    That is a convention of the file, not a parse. The lines run as raw
    template text, which equals the rendered text only while they hold no
    `%{` or `$${`, so that is asserted too.
    """
    lines = BOOTSTRAP.read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(SSH_AGENT_CAPTURE)]
    assert len(starts) == 1, starts
    pair = lines[starts[0] : starts[0] + 2]
    assert pair[1] == SSH_AGENT_EVAL, pair
    assert not any("%{" in line or "$${" in line for line in pair), pair
    return "\n".join(pair) + "\n"


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
