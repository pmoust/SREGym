"""archive_run.sh is the only route a run's results take out of S3.

It spent its first weeks untracked in a sibling brain-go checkout, so these
tests exist partly to keep it in version control with something asserting its
two load-bearing behaviours: what it fetches, and that a credential failure is
loud.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "benchmark" / "archive_run.sh"


def _source() -> str:
    return SCRIPT.read_text()


def test_the_script_is_tracked_and_executable():
    assert SCRIPT.exists(), "archive_run.sh must live in this repo, not in an untracked sibling checkout"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(SCRIPT.relative_to(ROOT))],
        cwd=ROOT,
        capture_output=True,
    )
    assert tracked.returncode == 0, "archive_run.sh must be tracked by git"


def test_the_results_sync_fetches_the_per_node_artifacts_not_only_csv():
    """The gap this closed: --include "*.csv" alone left every
    token-usage-by-node.json and tool-calls.json in S3, so a local archive had
    the scores but not the per-node evidence behind them. run-20260908-0739
    archived zero .json files despite writing 63 of each."""
    src = _source()
    sync = re.search(r'aws s3 sync "\$SRC" "\$RESULTS/".*?--only-show-errors', src, re.S)
    assert sync, "could not find the results sync"

    body = sync.group(0)
    assert '--include "*.csv"' in body
    assert '--include "*.json"' in body, "per-attempt node breakdowns would be left in S3"
    assert '--include "*.prom"' in body, "the scrapes a token delta was computed from would be left in S3"


def test_a_credential_failure_exits_nonzero_rather_than_looking_like_a_no_op():
    """Every aws call is wrapped so a failure cannot make `set -e` exit before
    a single line is printed. An expired SSO token used to produce no output
    and no files, which is indistinguishable from "nothing to fetch" for a tool
    whose whole job is rescuing data before it is deleted."""
    src = _source()
    assert "get-caller-identity" in src, "must check credentials before claiming there is no matching run"

    env = {"PATH": "/usr/bin:/bin", "AWS_CONFIG_FILE": "/dev/null", "AWS_SHARED_CREDENTIALS_FILE": "/dev/null"}
    done = subprocess.run(["bash", str(SCRIPT), "run-does-not-exist"], capture_output=True, text=True, env=env)

    assert done.returncode != 0, "a credential or lookup failure must not exit 0"
    assert done.stdout.strip() == "", "must not print archive progress when it fetched nothing"
    assert "archive_run:" in done.stderr, "must say on stderr why it did nothing"


def test_the_archive_root_survives_being_run_from_a_git_worktree():
    """Deriving the sibling brain-go checkout from this script's own path finds
    worktrees/brain-go, which does not exist, so a run archived from a linked
    worktree landed somewhere different from one archived from the primary
    checkout. Asks git for the primary worktree instead."""
    src = _source()
    assert "worktree list" in src, "sibling lookup must resolve the primary worktree, not the script's own path"
    assert "SREGYM_ARCHIVE_ROOT" in src, "the archive root must stay overridable"
