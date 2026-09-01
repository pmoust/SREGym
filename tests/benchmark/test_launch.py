"""launch.sh.

Every bug this file would have caught was found by a reviewer building scratch
repos by hand: a stale SHA pinned after a failed fetch, `launch.sh run` dying
before the plan because a grep matched nothing, and `read` aborting at EOF. All
three were in this one script and none was covered, so a future edit that
reintroduces any of them would only be caught by someone repeating that work.

These tests stop before `terraform`. They exercise argument parsing, path
resolution, scenario lookup and ref pinning, which is where the bugs were, and
they run against throwaway git repos under tmp_path rather than the checkout.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCH = ROOT / "scripts" / "benchmark" / "launch.sh"


def _git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def _repo(path: Path, with_launch: bool = True) -> Path:
    """A git repo shaped enough like the real one for launch.sh to run in."""
    (path / "scripts" / "benchmark").mkdir(parents=True)
    (path / "terraform").mkdir()
    if with_launch:
        shutil.copy(LAUNCH, path / "scripts" / "benchmark" / "launch.sh")
        os.chmod(path / "scripts" / "benchmark" / "launch.sh", 0o755)
    (path / "terraform" / "smoke.tfvars").write_text(
        '# Prove the harness runs.\nproblems = [\n  "network_policy_block",\n]\ninstance_count = 1\n'
    )
    (path / "terraform" / "run.tfvars").write_text("# The scored run.\ninstance_count = 3\n")
    # Provider-free, so `terraform plan` works offline and the run reaches the
    # apply prompt. Only the variables launch.sh and the scenarios actually pass
    # need declaring.
    (path / "terraform" / "main.tf").write_text(
        'variable "problems" {\n  type    = list(string)\n  default = []\n}\n'
        'variable "instance_count" {\n  default = 1\n}\n'
        'variable "sregym_ref" {\n  default = "main"\n}\n'
        'variable "brain_go_ref" {\n  default = "main"\n}\n'
        'output "seen" {\n  value = "${var.sregym_ref}"\n}\n'
    )

    _git(path, "init", "-q", ".")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    _git(path, "branch", "-M", "main")

    return path


def _remote(path: Path) -> Path:
    """A bare origin, so `fetch origin main` has something real to talk to."""
    bare = path.parent / f"{path.name}-origin.git"
    _git(path, "clone", "-q", "--bare", str(path), str(bare))
    _git(path, "remote", "add", "origin", str(bare))
    _git(path, "fetch", "-q", "origin", "main")

    return bare


def _run(repo: Path, *args, stdin: str | None = None, brain_go: Path | None = None):
    env = {**os.environ}
    env["BRAIN_GO_DIR"] = str(brain_go) if brain_go else str(repo)
    return subprocess.run(
        ["bash", str(repo / "scripts" / "benchmark" / "launch.sh"), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        input=stdin if stdin is not None else "",
        env=env,
    )


def test_a_missing_scenario_lists_what_is_available(tmp_path):
    repo = _repo(tmp_path / "r")
    _remote(repo)
    r = _run(repo, "nosuch")

    assert r.returncode == 1
    assert "no such scenario: nosuch" in r.stderr
    # The listing itself used to print nothing, because ROOT resolved to
    # scripts/ and the glob matched no files.
    assert "smoke" in r.stderr and "run" in r.stderr
    assert "Prove the harness runs." in r.stderr


def test_plan_only_parses_in_either_position(tmp_path):
    repo = _repo(tmp_path / "r")
    _remote(repo)
    for args in (("smoke", "--plan-only"), ("--plan-only", "smoke")):
        r = _run(repo, *args)
        assert "scenario   smoke" in r.stdout, f"{args}: {r.stdout}{r.stderr}"


def test_two_scenarios_is_an_error_rather_than_a_silent_choice(tmp_path):
    repo = _repo(tmp_path / "r")
    _remote(repo)
    r = _run(repo, "smoke", "run")

    assert r.returncode == 2
    assert "more than one scenario" in r.stderr


def test_an_unknown_flag_is_rejected(tmp_path):
    repo = _repo(tmp_path / "r")
    _remote(repo)
    r = _run(repo, "smoke", "--dry-run")

    assert r.returncode == 2
    assert "unknown option" in r.stderr


def test_a_failed_fetch_refuses_rather_than_pinning_a_stale_sha(tmp_path):
    """The bug that defeated the script's whole purpose.

    errexit does not propagate out of a multi-statement command substitution on
    bash 3.2, so the failed fetch was ignored, rev-parse succeeded against the
    cached ref, and the 40-character check passed because a stale SHA is still
    40 characters.
    """
    repo = _repo(tmp_path / "r")
    _remote(repo)
    stale = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "origin/main"], capture_output=True, text=True
    ).stdout.strip()
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

    r = _run(repo, "smoke", "--plan-only")

    assert r.returncode == 1
    assert "refusing to pin a possibly stale ref" in r.stderr
    assert stale not in r.stdout, "pinned the stale SHA anyway"


def test_a_scenario_with_no_problems_list_still_reaches_the_plan(tmp_path):
    """run.tfvars leaves problems unset, so a grep matches nothing and exits 1.

    Under pipefail that killed the script after printing its settings and before
    planning, every time, while the smoke scenario worked.
    """
    repo = _repo(tmp_path / "r")
    _remote(repo)
    r = _run(repo, "run", "--plan-only")

    assert "problems             (unset" in r.stdout, r.stdout + r.stderr
    # It gets as far as terraform, which is not installed in every environment;
    # what matters is that it did not die in the metadata block.
    assert "scenario   run" in r.stdout


def test_a_non_interactive_invocation_declines_rather_than_aborting(tmp_path):
    """read fails at EOF. That used to abort with no message at all."""
    repo = _repo(tmp_path / "r")
    _remote(repo)
    if shutil.which("terraform") is None:
        pytest.skip("terraform is needed to reach the prompt")

    r = _run(repo, "smoke", stdin="")

    assert "not applied" in r.stdout or "not applied" in r.stderr


def test_brain_go_is_found_beside_the_main_checkout_from_a_worktree(tmp_path):
    """A worktree's parent is not the main checkout's parent, so the obvious
    single candidate misses."""
    main = _repo(tmp_path / "nest" / "sregym")
    _remote(main)
    brain = _repo(tmp_path / "nest" / "brain-go", with_launch=False)
    _remote(brain)
    wt = tmp_path / "nest" / "worktrees" / "wt"
    _git(main, "worktree", "add", "-q", "--detach", str(wt))

    env = {**os.environ}
    env.pop("BRAIN_GO_DIR", None)
    r = subprocess.run(
        ["bash", str(wt / "scripts" / "benchmark" / "launch.sh"), "smoke", "--plan-only"],
        cwd=wt,
        capture_output=True,
        text=True,
        input="",
        env=env,
    )

    assert "cannot find the brain-go checkout" not in r.stderr, r.stderr
    assert str(brain.resolve()) in r.stdout or "brain-go " in r.stdout


def test_invoked_through_a_symlink(tmp_path):
    repo = _repo(tmp_path / "r")
    _remote(repo)
    link = tmp_path / "launch-link.sh"
    link.symlink_to(repo / "scripts" / "benchmark" / "launch.sh")

    r = subprocess.run(
        ["bash", str(link), "nosuch"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "BRAIN_GO_DIR": str(repo)},
    )

    # Resolving $0 wrongly makes the scenario listing empty rather than failing.
    assert "smoke" in r.stderr, r.stderr


@pytest.mark.parametrize("reply", ["y", "Y", "yes", "YES", "Yes"])
def test_the_apply_prompt_accepts_what_people_actually_type(tmp_path, reply):
    """The prompt reads [y/N] and "yes" is what gets typed at it.

    Matching only the single letter turned a deliberate yes into "not applied",
    which reads as though the script refused rather than as though the answer
    was not understood. The scratch config declares no providers, so the apply
    this drives touches nothing outside tmp_path.
    """
    if shutil.which("terraform") is None:
        pytest.skip("terraform is needed to reach the prompt")
    repo = _repo(tmp_path / "r")
    _remote(repo)

    r = _run(repo, "smoke", stdin=reply + "\n")

    assert "not applied" not in r.stdout, f"{reply!r} was read as a no"
    assert "watch it with" in r.stdout, r.stdout + r.stderr


@pytest.mark.parametrize("reply", ["n", "N", "", "nonsense"])
def test_the_apply_prompt_declines_on_anything_else(tmp_path, reply):
    if shutil.which("terraform") is None:
        pytest.skip("terraform is needed to reach the prompt")
    repo = _repo(tmp_path / "r")
    _remote(repo)

    r = _run(repo, "smoke", stdin=reply + "\n")

    assert "not applied" in r.stdout, f"{reply!r} was read as a yes"
