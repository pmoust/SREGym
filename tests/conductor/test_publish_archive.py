"""The agent's transcripts are folded in after publishing, not staged."""

import ast
import inspect
import json
import os
import types

import main
from sregym.run_artifacts import RunArtifacts


def test_transcripts_move_into_the_published_directory(tmp_path):
    archive = tmp_path / "archive"
    published = tmp_path / "published"
    archive.mkdir()
    published.mkdir()
    (archive / "rci_history.json").write_text(json.dumps({"events": []}))
    (archive / "turn_history.json").write_text(json.dumps({"events": []}))

    main._publish_archive(types.SimpleNamespace(archive_dir=archive), published)

    assert sorted(p.name for p in published.iterdir()) == ["rci_history.json", "turn_history.json"]
    assert not archive.exists(), "the emptied archive directory is removed"


def test_a_missing_archive_directory_is_not_an_error(tmp_path):
    published = tmp_path / "published"
    published.mkdir()

    main._publish_archive(types.SimpleNamespace(archive_dir=tmp_path / "gone"), published)
    main._publish_archive(types.SimpleNamespace(), published)

    assert list(published.iterdir()) == []


def test_the_timed_out_attempt_reaps_with_the_named_grace():
    """A structural check, because the timeout path needs a whole attempt to run.

    The 60s window the driver's export was sized against only exists on the
    normal path. Dropping the timeout kwarg here silently puts the timed-out
    attempt back on cleanup_agent's 5s default, which is the attempt most worth
    archiving.
    """
    tree = ast.parse(inspect.getsource(main))

    graced = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "cleanup_agent"
        and any(
            kw.arg == "timeout" and isinstance(kw.value, ast.Name) and kw.value.id == "AGENT_REAP_GRACE_SECONDS"
            for kw in node.keywords
        )
    ]

    assert len(graced) == 1, "the timeout path must reap with AGENT_REAP_GRACE_SECONDS, not cleanup_agent's default"


def test_the_agent_environment_exports_the_archive_directory(tmp_path):
    """Behavioural, not a source grep.

    The constant's name appears twice in _artifact_environment, once in the
    bookkeeping that saves the previous value and once in the export that
    matters. A substring check is satisfied by the first, so deleting the export
    leaves it green while every agent launches with the variable unset and
    archives nothing.
    """
    active = tmp_path / "active"
    archive = tmp_path / "archive"
    active.mkdir()
    archive.mkdir()
    run = types.SimpleNamespace(active_dir=active, archive_dir=archive, artifact_id="anon_x")

    before = os.environ.get("AGENT_ARCHIVE_DIR")

    with main._artifact_environment(run):
        assert os.environ["AGENT_ARCHIVE_DIR"] == str(archive.resolve())
        assert os.environ["AGENT_LOGS_DIR"] == str(active.resolve())

    assert os.environ.get("AGENT_ARCHIVE_DIR") == before, "the previous value is restored"


def test_the_archive_sits_outside_the_scanned_tree(tmp_path):
    """The one invariant the archive split exists to establish.

    Everything else about the split can stay green while the archive is nested
    back under the directory finalization scans, and the next model-authored
    slug matching a problem id would again cost the attempt its published tree.
    """
    run = RunArtifacts.create(
        staging_root=tmp_path / "staging",
        results_root=tmp_path / "results",
        problem_id="incorrect_image",
        agent="brain_go",
        attempt=1,
    )

    assert not run.archive_dir.is_relative_to(run.active_dir), (
        "the archive must sit outside the tree finalization scans for the real problem id"
    )
    assert run.archive_dir.is_dir()
