"""archive_run.sh is the only route a run's results take out of S3.

It spent its first weeks untracked in a sibling brain-go checkout, so these
tests keep it in version control with something asserting the behaviours that
would silently regress.

Almost everything here runs the script. Root resolution, root validation and
the refusal when credentials are unusable all happen before the first API call.
Bucket resolution needs an AWS reply rather than real S3, so those tests stub
the `aws` binary on PATH and assert on exit status and output; they work in CI
with no credentials. Only the sync filters stay source-level, because the
filtering happens inside the CLI rather than in this script.
"""

import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "benchmark" / "archive_run.sh"

# Enough PATH for the script's own tools, and deliberately keeping the real
# `aws` binary reachable: an earlier version of this file set PATH to
# /usr/bin:/bin, which on a Homebrew machine removed `aws` entirely, so the
# credential test passed because the binary was missing rather than because the
# credential check worked.
_AWS_DIR = str(Path(shutil.which("aws")).parent) if shutil.which("aws") else ""
_PATH = ":".join(p for p in (_AWS_DIR, "/usr/bin", "/bin", "/usr/local/bin") if p)


def _run(args, env_extra=None, cwd=None):
    env = {"PATH": _PATH, "HOME": os.environ.get("HOME", "/tmp")}
    env.update(env_extra or {})
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, cwd=cwd or str(ROOT))


def _fake_aws(bindir, per_run=(), shared="", prefixes=()):
    """Stand in for the aws CLI so bucket resolution is testable without S3.

    Bucket resolution used to be checked by counting strings in the source,
    which cannot see whether a refusal actually stops. Dropping the `exit 1`
    while keeping the warning above it left those assertions green, and that is
    exactly the silent wrong-run archival the refusal exists to prevent. This
    answers the three calls the script makes during resolution, so the test can
    assert on exit status instead.
    """
    bindir = Path(bindir)
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "aws"
    stub.write_text(
        "#!/bin/bash\n"
        # Credentials are checked before resolution, so this has to pass for
        # resolution to be reached at all.
        'if [ "$1 $2" = "sts get-caller-identity" ]; then exit 0; fi\n'
        'if [ "$1 $2" = "s3api list-buckets" ]; then\n'
        # The two list-buckets calls differ only by their --query, so the
        # shared-bucket wrapper in the query string is what tells them apart.
        '  for a in "$@"; do\n'
        "    case \"$a\" in *sregym-bench-results-*) printf '%s\\n' " + shlex.quote(shared) + "; exit 0;; esac\n"
        "  done\n"
        "  printf '%s\\n' " + shlex.quote("\n".join(per_run)) + "\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1 $2" = "s3api list-objects-v2" ]; then\n'
        "  printf '%s\\n' " + shlex.quote("\n".join(f"{x}/" for x in prefixes)) + "\n"
        "  exit 0\n"
        "fi\n"
        # s3 sync and anything else: succeed silently.
        "exit 0\n"
    )
    stub.chmod(0o755)

    return str(bindir)


def _run_stubbed(args, bindir, root):
    env = {"PATH": f"{bindir}:{_PATH}", "HOME": os.environ.get("HOME", "/tmp"), "SREGYM_ARCHIVE_ROOT": str(root)}

    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, cwd=str(ROOT))


def _sandbox(tmp):
    root = Path(tmp) / "root"
    root.mkdir()

    return root, Path(tmp) / "bin"


def _source() -> str:
    return SCRIPT.read_text()


def test_the_script_is_tracked_and_executable():
    assert SCRIPT.exists(), "archive_run.sh must live in this repo, not in an untracked sibling checkout"

    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(SCRIPT.relative_to(ROOT))], cwd=ROOT, capture_output=True
    )
    assert tracked.returncode == 0, "archive_run.sh must be tracked by git"

    # Actually check the bit this test's name claims to check.
    assert SCRIPT.stat().st_mode & stat.S_IXUSR, "archive_run.sh must be executable"


def test_a_bad_archive_root_is_refused_before_anything_is_fetched():
    """Root validation runs before the first AWS call, so this needs no
    credentials. Both routes into ROOT used to reach mkdir before failing, and
    the failure named a path nobody had chosen."""
    done = _run(["run-anything"], {"SREGYM_ARCHIVE_ROOT": "/nope/does/not/exist"})

    assert done.returncode != 0
    assert "not a writable directory" in done.stderr
    assert "SREGYM_ARCHIVE_ROOT" in done.stderr, "must name the knob to turn"
    assert done.stdout.strip() == "", "must not print archive progress"


def test_a_writable_archive_root_is_accepted():
    """The other half of validation, and it has to assert something positive.

    Checking only that the rejection message is absent passes just as well when
    validation never ran at all, or when the script died earlier for some
    unrelated reason. Reaching bucket resolution is the evidence that a good
    root was accepted and execution carried on past the check.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root, bindir = _sandbox(tmp)
        done = _run_stubbed(["run-anything"], _fake_aws(bindir), root)

        assert "not a writable directory" not in done.stderr
        assert "no run matching" in done.stderr, "a good root should reach bucket resolution"


def test_the_root_resolves_to_the_primary_worktree_not_the_script_path():
    """Deriving the sibling checkout from the script's own path finds
    worktrees/brain-go, which does not exist, so a run archived from a linked
    worktree landed somewhere different from one archived from the primary
    checkout. Runs the real resolution instead of grepping for it."""
    done = subprocess.run(
        ["bash", "-x", str(SCRIPT), "run-anything"],
        capture_output=True,
        text=True,
        env={"PATH": _PATH, "HOME": os.environ.get("HOME", "/tmp"), "SREGYM_ARCHIVE_ROOT": ""},
        cwd=str(ROOT),
    )
    trace = done.stderr

    primary = re.search(r"^\+ PRIMARY=(.+)$", trace, re.M)
    assert primary, "the trace should show PRIMARY being resolved"

    resolved = primary.group(1).strip()
    assert resolved not in (".", "/", ""), f"PRIMARY resolved to a nonsense root: {resolved!r}"
    assert (Path(resolved) / ".git").exists(), f"PRIMARY should be a real checkout, got {resolved!r}"


def test_the_results_sync_fetches_the_per_node_artifacts_not_only_csv():
    """The gap this closed: --include "*.csv" alone left every
    token-usage-by-node.json and tool-calls.json in S3, so a local archive had
    the scores but not the per-node evidence behind them. run-20260908-0739
    archived zero .json files despite writing 63 of each.

    Source-level: the sync itself needs live S3.
    """
    sync = re.search(r'aws s3 sync "\$SRC" "\$RESULTS/".*?--only-show-errors', _source(), re.S)
    assert sync, "could not find the results sync"

    body = sync.group(0)
    for want in ('--include "*.csv"', '--include "*.json"', '--include "*.prom"'):
        assert want in body, f"results sync must carry {want}"

    # AWS CLI's "*" spans path separators, so the bare includes above also swept
    # kubectl's API-discovery cache (300 of 478 objects on run-20260908-0739) and
    # the shard scrapes the logs sync already takes. Later filters win.
    assert '--exclude "*/.kube/*"' in body, "would sweep kubectl's discovery cache into the results tree"
    assert '--exclude "*/logs/*"' in body, "would fetch the shard scrapes into both trees"


def test_the_per_run_bucket_layout_refuses_an_ambiguous_prefix():
    """The per-run branch took head -1 of however many buckets matched, so a
    prefix matching two runs archived whichever sorted first and printed an
    ordinary success line. The two bucket names below are the real ones that
    made "run-2026" ambiguous.

    Invoked rather than grepped, because a source-level count cannot tell a
    refusal from a warning followed by carrying on anyway, and the second is
    the same wrong-run archival with extra output.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root, bindir = _sandbox(tmp)
        stub = _fake_aws(
            bindir,
            per_run=[
                "sregym-bench-run-20260829-1325-a4d231c1-results",
                "sregym-bench-run-20260831-1752-2ac5a6ce-results",
            ],
        )
        done = _run_stubbed(["run-2026"], stub, root)

        assert done.returncode == 1, "an ambiguous prefix must stop the script, not just warn"
        assert "matches more than one per-run bucket" in done.stderr
        assert "run-20260829-1325-a4d231c1" in done.stderr, "must name every candidate"
        assert "run-20260831-1752-2ac5a6ce" in done.stderr, "must name every candidate"
        assert "pass a longer prefix" in done.stderr
        assert "[archive]" not in done.stdout, "must not begin archiving a run it could not identify"


def test_the_shared_bucket_layout_refuses_an_ambiguous_prefix():
    """Same refusal, other layout: the run id comes from prefixes inside one
    shared bucket instead of from the bucket name. The two prefixes below are
    real runs that made "run-20260903" ambiguous."""
    with tempfile.TemporaryDirectory() as tmp:
        root, bindir = _sandbox(tmp)
        stub = _fake_aws(
            bindir,
            shared="sregym-bench-results-819709950457",
            prefixes=["run-20260903-0822-4fc0249e", "run-20260903-1144-4fc0249e"],
        )
        done = _run_stubbed(["run-20260903"], stub, root)

        assert done.returncode == 1, "an ambiguous prefix must stop the script, not just warn"
        assert "matches more than one run in" in done.stderr
        assert "run-20260903-0822-4fc0249e" in done.stderr, "must name every candidate"
        assert "run-20260903-1144-4fc0249e" in done.stderr, "must name every candidate"
        assert "pass a longer prefix" in done.stderr
        assert "[archive]" not in done.stdout, "must not begin archiving a run it could not identify"


def test_an_unambiguous_prefix_is_archived_rather_than_refused():
    """The refusals must fire on more than one match and not on one, which is
    the ordinary case. Without this, a comparison tightened into refusing
    everything would pass both tests above."""
    with tempfile.TemporaryDirectory() as tmp:
        root, bindir = _sandbox(tmp)
        stub = _fake_aws(bindir, per_run=["sregym-bench-run-20260829-1325-a4d231c1-results"])
        done = _run_stubbed(["run-20260829"], stub, root)

        assert done.returncode == 0, done.stderr
        assert "matches more than one" not in done.stderr
        assert "[archive] results ->" in done.stdout, "a single match should proceed to the sync"
        # The bucket-name strips either side of the run id are load-bearing.
        assert (root / "sregym-results" / "run-20260829-1325-a4d231c1").is_dir()


def test_lookups_tolerate_failure_but_the_syncs_do_not():
    """Two opposite requirements, and conflating them is how this went wrong.

    The *lookups* that resolve a bucket and run id must tolerate failure: they
    run under set -e, so an unguarded one exits the script before printing a
    line, and an expired SSO token then looks exactly like "no matching run,
    nothing to fetch" for a tool whose job is rescuing data before deletion.

    The *syncs* must NOT tolerate failure. A swallowed sync error would report
    a successful archive having fetched nothing, which is the same silent
    failure wearing different clothes.
    """
    src = _source()
    assert "get-caller-identity" in src, "must check credentials before claiming there is no matching run"

    lines = src.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("aws ") or "get-caller-identity" in stripped:
            continue

        window = "\n".join(lines[i : i + 5])
        if "s3api list-" in stripped:
            assert "|| true" in window, f"unguarded lookup at line {i + 1}: {stripped!r}"
        elif "s3 sync" in stripped:
            assert "|| true" not in window, (
                f"sync at line {i + 1} swallows its own failure, so a fetch that "
                f"got nothing would report success: {stripped!r}"
            )
