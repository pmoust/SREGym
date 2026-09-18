"""The agent's transcripts are folded in after publishing, not staged."""

import ast
import inspect
import json
import types

import main


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


def test_the_agent_environment_exports_the_archive_directory():
    source = inspect.getsource(main._artifact_environment)

    assert "AGENT_ARCHIVE_DIR_ENV" in source, "an agent with no archive directory writes no transcripts at all"
