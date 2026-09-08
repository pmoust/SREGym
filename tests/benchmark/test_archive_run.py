"""archive_run.sh is the only route a run's results take out of S3.

It spent its first weeks untracked in a sibling brain-go checkout, so these
tests keep it in version control with something asserting the behaviours that
would silently regress.

Split deliberately into two kinds. Everything reachable without AWS is tested
by running the script: root resolution, root validation, and the refusal when
credentials are unusable all happen before the first API call, so they are real
behavioural tests that work in CI. The bucket-resolution logic needs live S3 and
CI has no credentials, so those are source-level assertions, and each says so
rather than implying it exercised anything.
"""

import os
import re
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
    """The other half: a good override must get past validation rather than
    being refused for an unrelated reason."""
    with tempfile.TemporaryDirectory() as tmp:
        done = _run(["run-anything"], {"SREGYM_ARCHIVE_ROOT": tmp})

        assert "not a writable directory" not in done.stderr


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


def test_both_bucket_layouts_refuse_an_ambiguous_prefix():
    """The shared-bucket branch always refused an ambiguous prefix; the
    per-run-bucket branch took head -1 of however many matched, so a prefix
    matching two per-run buckets silently archived whichever sorted first and
    printed an ordinary success line. "run-2026" matches two real buckets.

    Source-level: reproducing it needs live S3 with several runs present.
    """
    src = _source()
    assert src.count("matches more than one") == 2, (
        "both the per-run-bucket and shared-bucket branches must refuse an ambiguous prefix"
    )
    assert "pass a longer prefix" in src

    # The per-run branch must not silently collapse its matches before counting.
    resolve = src.split("BUCKET_MATCHES=", 1)
    assert len(resolve) == 2, "per-run bucket lookup should collect every match, not head -1"


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
