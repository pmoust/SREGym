"""The MCP pod runs this checkout's code, and the run says which image it ran.

Every benchmark run before this one deployed ghcr.io/sregym/sregym-mcp:latest
with imagePullPolicy: Always. That image was built 2026-09-06, so it has
neither the /healthz route nor the anyio.to_thread.run_sync offload this repo
added afterwards, and no artifact recorded that. The consequence was
measurable: a blocking kubectl call held the server's single event loop, the
supervisor's probe went unanswered, and run-20260921-1250 restarted healthy
port-forwards 43 times.

Three things have to hold together for that not to come back, and each is one
edit away from breaking silently:

  - the image is built from the checkout and tagged with its commit,
  - it is loaded into a slot's kind node before the deploy applies anything,
  - the deploy overrides the image and the pull policy at apply time, leaving
    upstream's committed manifest byte for byte diffable.
"""

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from unittest import mock

import pytest
import yaml

from sregym.paths import MCP_SERVER_K8S
from sregym.service.mcp_server import MCP_IMAGE_ENV, MCPServer, kustomize_root, split_image_reference

ROOT = Path(__file__).resolve().parents[2]
RUN_SHARD = ROOT / "scripts" / "benchmark" / "run_shard.sh"
BUILD_SCRIPT = ROOT / "scripts" / "benchmark" / "build_mcp_image.sh"
DOCKERFILE = ROOT / "mcp_server" / "Dockerfile"

# The invocation, not the bare filename: the filename also appears in a
# comment further up the file, and an ordering assertion against that
# comment would pass or fail on where the comment sits.
DEPLOY_CALL = 'python "$WORKDIR/scripts/benchmark/deploy_mcp.py"'


def _head_sha() -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _run_shard_body() -> str:
    return RUN_SHARD.read_text()


def test_the_image_is_tagged_with_the_commit_it_was_built_from():
    """A moving tag is what made the old image undiagnosable from a pod.

    "latest" says nothing about which code answered a tool call. The commit
    says exactly that, and it is the same value the manifest records.
    """
    assert BUILD_SCRIPT.stat().st_mode & 0o111, "run_shard.sh calls this directly"

    proc = subprocess.run([str(BUILD_SCRIPT), "--print-name"], capture_output=True, text=True)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"sregym-mcp:{_head_sha()}"


def test_the_dockerfile_copies_every_package_the_build_declares():
    """A declared package missing from the context fails the build, not a COPY.

    atif_converter has been in pyproject.toml's [tool.setuptools] packages
    since 2026-07-11 and mcp_server/Dockerfile never copied it, so
    `uv sync --frozen` ended at "error: package directory 'atif_converter'
    does not exist" and no image could be built from this tree at all. The
    message names setuptools rather than the Dockerfile, so pin the class here
    instead of the one directory.
    """
    copied = set(re.findall(r"^COPY (\S+)/ \./", DOCKERFILE.read_text(), re.M))
    declared = {
        package.split(".")[0]
        for package in tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["packages"]
    }

    assert not declared - copied, f"declared but never copied into the image: {sorted(declared - copied)}"


def test_printing_the_name_reaches_no_docker(tmp_path, monkeypatch):
    """write_run_manifest.sh asks for the name seconds into the bootstrap.

    That is before anything wants an image built, and the manifest call is
    best-effort, so a --print-name that shelled out to docker would turn a
    slow or absent daemon into an empty manifest field. Run against a docker
    that fails on sight: a build would take it, a name lookup never looks.
    """
    stub = tmp_path / "docker"
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    proc = subprocess.run([str(BUILD_SCRIPT), "--print-name"], capture_output=True, text=True)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"sregym-mcp:{_head_sha()}"


def test_the_manifest_records_the_image_the_build_script_names(tmp_path):
    """One value, two files, and they must not be able to disagree.

    write_run_manifest.sh asks build_mcp_image.sh for the name rather than
    deriving it again, so a manifest cannot record a tag nothing was built
    under.
    """
    out = tmp_path / "manifest.txt"
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts" / "benchmark" / "write_run_manifest.sh"), str(out)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PROBLEM_SET": "sregym-lite", "PROBLEMS_OVERRIDE": ""},
    )
    assert proc.returncode == 0, proc.stderr

    fields = dict(line.split("=", 1) for line in out.read_text().strip().split("\n") if "=" in line)

    assert fields["mcp_image"] == f"sregym-mcp:{_head_sha()}"


def _mcp_load_offset(text: str) -> int:
    """Where run_shard.sh actually loads the MCP image.

    Matched on the command as it is run, not on any mention of it: a comment
    naming `kind load docker-image` sits earlier in the file and would make
    both ordering assertions below read the wrong position.
    """
    needle = 'kind load docker-image "$SREGYM_MCP_IMAGE"'
    assert needle in text, "run_shard.sh no longer loads the MCP image this way"

    return text.index(needle)


def test_the_build_runs_once_per_box_and_the_load_runs_per_slot():
    """Two different frequencies, and putting either at the other's place is
    silently wrong rather than an error.

    The build is per box: docker's cache and the image are shared by every
    slot on the instance. The load is per slot: a kind node keeps its own
    image store and each slot creates its own cluster, so an image loaded
    once reaches one cluster.
    """
    text = _run_shard_body()

    build = text.index("build_mcp_image.sh")
    run_slot = text.index("run_slot() {")
    load = _mcp_load_offset(text)

    assert build < run_slot, "the build is inside run_slot, so it would run once per slot"
    assert load > run_slot, "the load is outside run_slot, so it would reach one cluster only"


def test_the_load_happens_after_the_cluster_exists_and_before_the_deploy():
    """Ordering is the whole mechanism. Loading before `kind create cluster`
    has no node to load into; loading after the deploy leaves the pod in
    ErrImageNeverPull, because the override sets imagePullPolicy: Never."""
    text = _run_shard_body()

    create = text.index("setup_kind_cluster.sh")
    load = _mcp_load_offset(text)
    deploy = text.index(DEPLOY_CALL)

    assert create < load < deploy


def test_the_slot_records_the_image_its_pod_actually_started():
    """What was asked for and what ran are different facts, and only the
    second one would have caught the stale image.

    Written as .json under the slot directory because that is what already
    reaches an archive: sync_results syncs SLOTS_DIR, and archive_run.sh's
    results sync includes *.json.
    """
    text = _run_shard_body()
    record = text.split(DEPLOY_CALL, 1)[1].split("PF_PID_FILE=", 1)[0]

    assert "containerStatuses[*].image}" in record
    assert "containerStatuses[*].imageID}" in record
    assert "app.kubernetes.io/name=mcp-server" in record
    assert '>"$slot_dir/mcp_pod_image.json"' in record, "must land where sync_results already syncs"


def test_an_unset_image_applies_the_committed_kustomization(monkeypatch):
    """Upstream's path, untouched. Nothing in mcp_server/k8s is edited to make
    the override work, so a standalone `main.py` run behaves as it always
    did."""
    monkeypatch.delenv(MCP_IMAGE_ENV, raising=False)

    with kustomize_root() as root:
        assert Path(root) == MCP_SERVER_K8S


def test_the_override_names_the_commit_tag_and_never_pulls(monkeypatch):
    """The two halves of the override, both of which the pod needs.

    The tag is resolved at run time and so cannot live in a committed file,
    and imagePullPolicy is not something kustomize's images transform can set
    at all, which is why this is a generated overlay rather than an `images:`
    entry alone.
    """
    monkeypatch.setenv(MCP_IMAGE_ENV, f"sregym-mcp:{_head_sha()}")

    with kustomize_root() as root:
        spec = yaml.safe_load((Path(root) / "kustomization.yaml").read_text())

        assert {entry.get("newTag") for entry in spec["images"]} == {_head_sha()}
        assert {entry.get("newName") for entry in spec["images"]} == {"sregym-mcp"}
        assert "imagePullPolicy: Never" in spec["patches"][0]["patch"]

        # The base is copied in rather than referenced where it lives, so the
        # committed directory is never written to and kustomize reaches it by
        # a relative path from a root the overlay owns.
        assert spec["resources"] == ["../base"]
        assert (Path(root).parent / "base" / "kustomization.yaml").exists()

    assert not Path(root).exists(), "the overlay outlived the deploy that used it"


def test_the_committed_manifest_is_left_alone(monkeypatch):
    """The override is applied at deploy time, so mcp_server/k8s is upstream's.

    Pinned because the cheap way to ship this change is to edit those two
    files, and a fork carrying one local tag there cannot then take an
    upstream change to the same lines without a conflict.
    """
    before = (MCP_SERVER_K8S / "kustomization.yaml").read_bytes()
    monkeypatch.setenv(MCP_IMAGE_ENV, "sregym-mcp:deadbeef")

    with kustomize_root():
        pass

    assert (MCP_SERVER_K8S / "kustomization.yaml").read_bytes() == before
    assert b"newTag: latest" in before, "upstream's tag, which the overlay overrides rather than replaces"


def test_deploy_applies_the_overlay_rather_than_the_committed_directory(monkeypatch):
    """The override only matters if the apply points at it.

    deploy() builds the kustomization root and applies that root; an apply
    left on MCP_SERVER_K8S would generate an overlay and throw it away, and
    the pod would go back to the published image with nothing in the log to
    say so.
    """
    monkeypatch.setenv(MCP_IMAGE_ENV, "sregym-mcp:deadbeef")

    server = MCPServer.__new__(MCPServer)
    server.namespace = "sregym"
    server.service_name = "mcp-server"
    server.port = 9954
    server.port_forward_process = None
    server.kubectl = mock.Mock()
    server._ensure_rbac = mock.Mock()
    server._is_running = mock.Mock(return_value=False)
    server.start_port_forward = mock.Mock()

    monkeypatch.setenv("SREGYM_EXTERNAL_PORT_FORWARD", "1")
    server.deploy()

    applies = [call.args[0] for call in server.kubectl.exec_command.call_args_list if "apply -k" in call.args[0]]
    assert len(applies) == 1, applies
    assert str(MCP_SERVER_K8S) not in applies[0]
    assert applies[0].endswith("/overlay")


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs kubectl's embedded kustomize")
def test_the_overlay_renders_to_the_image_the_pod_will_run(monkeypatch):
    """Renders it rather than reading the source of it.

    The overlay transforms the base's *output*, where deployment.yaml's
    `sregym` has already become ghcr.io/sregym/sregym-mcp, so an override
    matching only the pre-transform name would parse fine and change nothing.
    This is the assertion that catches that.
    """
    monkeypatch.setenv(MCP_IMAGE_ENV, "sregym-mcp:cafe1234")

    with kustomize_root() as root:
        rendered = subprocess.run(
            ["kubectl", "kustomize", str(root)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    deployment = next(doc for doc in yaml.safe_load_all(rendered) if doc and doc["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]

    assert container["image"] == "sregym-mcp:cafe1234"
    assert container["imagePullPolicy"] == "Never"


@pytest.mark.parametrize(
    "reference, expected",
    [
        ("sregym-mcp:abc123", ("sregym-mcp", "abc123")),
        ("sregym-mcp", ("sregym-mcp", "")),
        ("ghcr.io/sregym/sregym-mcp:latest", ("ghcr.io/sregym/sregym-mcp", "latest")),
        # A registry port is not a tag. Splitting on the last colon alone
        # would name the image "localhost" and tag it "5000/sregym-mcp".
        ("localhost:5000/sregym-mcp", ("localhost:5000/sregym-mcp", "")),
        ("localhost:5000/sregym-mcp:abc123", ("localhost:5000/sregym-mcp", "abc123")),
    ],
)
def test_split_image_reference(reference, expected):
    assert split_image_reference(reference) == expected
