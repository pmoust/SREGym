"""Whether terminate() honours keep_alive_on_failure, and when.

The flag is named for failures and terminate() is also the success path:
run_shard.sh calls it with "shard complete" when a shard finishes, and ten
other sites call it because something broke. Before the split, a debug flag
meant to preserve a broken box for inspection also held a finished one up until
the runtime cap, which is an hour of idle m8i.8xlarge. It happened on
run-20260902-2008 and again on run-20260903-0558.

The function lives in a terraform template with %{ if } conditionals, so it is
rendered here for both flag values and run as shell. bash -n cannot see any of
this: every variant is valid shell, just not the one anyone meant.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "terraform" / "templates" / "bootstrap.sh.tftpl"


def _render_terminate(keep_alive: bool) -> str:
    """The terminate() function with the template's one conditional resolved."""
    body = TEMPLATE.read_text().split("terminate() {", 1)[1].split("\n}\n", 1)[0]
    body = "terminate() {" + body + "\n}\n"

    # %{ if keep_alive_on_failure } ... %{ else } ... %{ endif }, or no else.
    def resolve(match):
        inner = match.group(1)
        if "%{ else }" in inner:
            taken, other = inner.split("%{ else }", 1)
        else:
            taken, other = inner, ""

        return taken if keep_alive else other

    rendered = re.sub(r"%\{ if keep_alive_on_failure \}(.*?)%\{ endif \}", resolve, body, flags=re.S)
    assert "%{" not in rendered, f"unresolved template directive: {rendered}"

    return rendered


def _run(keep_alive: bool, outcome: str | None, tmp_path):
    """Run terminate() with the AWS call and shutdown stubbed to markers."""
    called = tmp_path / "terminated"
    script = tmp_path / "t.sh"
    args = f'"a reason"{" " + outcome if outcome else ""}'
    script.write_text(
        "REGION=us-east-2\nIID=i-test\n"
        "sync_results() { :; }\nsync_logs() { :; }\n"
        f'aws() {{ echo "$@" > {called}; }}\n'
        f"shutdown() {{ echo shutdown > {called}; }}\n" + _render_terminate(keep_alive) + f"terminate {args}\n"
    )
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    return proc.stdout, called.exists()


def test_a_finished_shard_terminates_even_with_the_flag_set(tmp_path):
    """The regression. run_shard.sh's completion path passes "success"."""
    out, terminated = _run(keep_alive=True, outcome="success", tmp_path=tmp_path)

    assert terminated, "a successful run must not be left billing"
    assert "succeeded, so terminating anyway" in out


def test_a_failure_stays_up_with_the_flag_set(tmp_path):
    """What the flag is for: keep the broken box so it can be inspected."""
    out, terminated = _run(keep_alive=True, outcome=None, tmp_path=tmp_path)

    assert not terminated, "a failure with the flag set must survive for inspection"
    assert "staying up" in out


@pytest.mark.parametrize("outcome", ["success", None])
def test_without_the_flag_everything_terminates(outcome, tmp_path):
    """The default, and what every scored run uses: run.tfvars never sets it."""
    _, terminated = _run(keep_alive=False, outcome=outcome, tmp_path=tmp_path)

    assert terminated


def test_the_shard_complete_path_declares_success():
    """The template's half is useless if the caller never says it succeeded."""
    shard = (ROOT / "scripts" / "benchmark" / "run_shard.sh").read_text()

    assert 'terminate "shard complete" success' in shard, (
        "run_shard.sh's completion exit must pass success, or the flag holds the box up"
    )


def test_the_runtime_cap_still_ignores_the_flag():
    """hard_stop is the one backstop against a forgotten box billing forever.

    A debug flag that silently disables it would defeat its purpose, so it must
    not grow the same conditional.
    """
    hard_stop = TEMPLATE.read_text().split("hard_stop() {", 1)[1].split("\n}\n", 1)[0]

    assert "keep_alive_on_failure" not in hard_stop
    assert "terminate-instances" in hard_stop
