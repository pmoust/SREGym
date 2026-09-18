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


def _scenario_with_ref(repo: Path, name: str, key: str, value: str) -> None:
    """A scenario tfvars, shaped like smoke.tfvars, with sregym_ref or
    brain_go_ref set to something other than main.

    Committed immediately, not left untracked: an untracked file makes `git
    status --porcelain` non-empty on its own, which would trip the dirty-
    worktree check regardless of what the test is actually trying to exercise.
    """
    (repo / "terraform" / f"{name}.tfvars").write_text(
        f'# Ref-resolution test scenario.\nproblems = [\n  "network_policy_block",\n]\n'
        f'instance_count = 1\n{key} = "{value}"\n'
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"add {name} scenario")


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


def test_an_unresolvable_ref_is_rejected_before_any_terraform_call(tmp_path):
    """An engineer's own ref goes through `resolve()`'s custom-ref path once a
    scenario's tfvars sets sregym_ref/brain_go_ref to anything but main. A
    typo or a deleted branch has to fail here, named, rather than surface as
    an opaque three-instances-terminated-at-boot failure."""
    repo = _repo(tmp_path / "r")
    _remote(repo)
    _scenario_with_ref(repo, "custom", "sregym_ref", "no/such/branch")

    r = _run(repo, "custom", "--plan-only")

    assert r.returncode == 1
    assert "does not resolve to a commit" in r.stderr


def test_an_unpushed_ref_is_rejected_with_a_named_error(tmp_path):
    """The fleet clones from the remote, so a commit that only exists in this
    worktree is invisible to it. That has to be a named error here, not a
    boot failure three minutes into a launch."""
    repo = _repo(tmp_path / "r")
    _remote(repo)
    _git(repo, "checkout", "-qb", "unpushed-feature")
    (repo / "unpushed.txt").write_text("local only\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "local-only commit")
    _scenario_with_ref(repo, "custom", "sregym_ref", "unpushed-feature")

    r = _run(repo, "custom", "--plan-only")

    assert r.returncode == 1
    assert "is not on origin" in r.stderr
    assert "push it first" in r.stderr


def test_a_dirty_worktree_is_refused_on_a_non_default_ref(tmp_path):
    """A run whose code exists nowhere, neither pinned nor on disk in the form
    it ran, is unreproducible. The default origin/main path never checks this
    (it always re-resolves from the remote), so this only applies once a
    scenario asks for a specific ref.

    The scenario file is added on main and carried onto the branch before the
    push, not after: committing it afterwards would leave the branch's tip one
    commit ahead of what got pushed, which is exactly the unpushed-commit
    failure this test is not trying to exercise.
    """
    repo = _repo(tmp_path / "r")
    _remote(repo)
    _scenario_with_ref(repo, "custom", "sregym_ref", "pushed-feature")
    _git(repo, "checkout", "-qb", "pushed-feature")
    (repo / "feature.txt").write_text("committed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feature commit")
    _git(repo, "push", "-q", "origin", "pushed-feature")
    # Dirty only now, after the push, so the pushed tip itself stays clean.
    (repo / "feature.txt").write_text("uncommitted change\n")

    r = _run(repo, "custom", "--plan-only")

    assert r.returncode == 1
    assert "worktree" in r.stderr and "dirty" in r.stderr


def test_an_untracked_file_does_not_count_as_dirty(tmp_path):
    """The counterpart to the test above: an untracked file cannot be part of
    the commit the on-origin check just cleared, so it cannot reach the fleet
    or change what runs, and refusing the launch over one names a
    reproducibility problem the run does not have.

    This is not hypothetical. Both real checkouts carry untracked scratch
    notes, an .idea/ and stray .DS_Store files, which made every pinned launch
    from them fail with a message about unreproducible code.
    """
    repo = _repo(tmp_path / "r")
    _remote(repo)
    _scenario_with_ref(repo, "custom", "sregym_ref", "pushed-feature")
    _git(repo, "checkout", "-qb", "pushed-feature")
    (repo / "feature.txt").write_text("committed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feature commit")
    _git(repo, "push", "-q", "origin", "pushed-feature")
    # Untracked only: never added, so no commit can carry it.
    (repo / "scratch-notes.md").write_text("notes nobody committed\n")

    r = _run(repo, "custom", "--plan-only")

    assert "dirty" not in r.stderr


def test_a_pushed_branch_resolves_to_its_correct_sha(tmp_path):
    """The success path: a branch name in the tfvars resolves to the exact
    SHA it was pushed at, and that is what gets pinned.

    The scenario file is added on main before branching, same as the dirty-
    worktree test above, so the pushed tip is exactly the SHA under test."""
    if shutil.which("terraform") is None:
        pytest.skip("terraform is needed to reach the plan")
    repo = _repo(tmp_path / "r")
    _remote(repo)
    _scenario_with_ref(repo, "custom", "sregym_ref", "clean-feature")
    _git(repo, "checkout", "-qb", "clean-feature")
    (repo / "feature.txt").write_text("committed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feature commit")
    _git(repo, "push", "-q", "origin", "clean-feature")
    want_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "clean-feature"], capture_output=True, text=True
    ).stdout.strip()

    r = _run(repo, "custom", "--plan-only")

    assert r.returncode == 0, r.stdout + r.stderr
    assert f"sregym     {want_sha}" in r.stdout
    assert "(ref: clean-feature)" in r.stdout
