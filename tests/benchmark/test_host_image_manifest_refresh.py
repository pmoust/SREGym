"""registry:3 and the kind node image must reach the manifest's
brain_go_host_images field, not just setup_brain_go.sh's brain-go rows.

run_shard.sh appends those two images to SREGYM_HOST_IMAGE_MANIFEST after
the bootstrap's one call to write_run_manifest.sh has already read that file
(terraform/templates/bootstrap.sh.tftpl runs setup_brain_go.sh, then
write_run_manifest.sh, then sources run_shard.sh). Without a refresh, a real
run's manifest names brain-go's own images only, which is the gap this file
pins against both at the unit level (write_run_manifest.sh --host-images
directly) and by running the real block out of run_shard.sh.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN_SHARD = ROOT / "scripts" / "benchmark" / "run_shard.sh"
WRITE_MANIFEST = ROOT / "scripts" / "benchmark" / "write_run_manifest.sh"

_DIGEST = "sha256:" + "0" * 39 + "1"

# load_host_image's two reads: the digest pointer (stdout) and the tarball
# (a file `aws s3 cp <key> <dest>` must create for `docker load` to find).
# Lifted from test_setup_brain_go_host_image_gate.py's FAKE_AWS, which uses
# the same load_host_image function for brain-go's own host images.
FAKE_AWS = f"""#!/bin/sh
case "$*" in
  *".digest"*)
    echo "{_DIGEST}"
    ;;
  *)
    for a in "$@"; do
      last="$a"
    done
    touch "$last" 2>/dev/null || true
    ;;
esac
exit 0
"""

FAKE_DOCKER = f"""#!/bin/sh
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  echo "{_DIGEST}"
  exit 0
fi
if [ "$1" = "inspect" ]; then
  echo "{_DIGEST}"
  exit 0
fi
if [ "$1" = "load" ]; then
  exit 0
fi
exit 0
"""


def _extract_host_images_block() -> str:
    """The real "host images: registry:3, the kind node image" section.

    Same technique as test_tasklist_generation.py's
    test_the_shard_hard_stops_on_a_rejected_stage_value: slice the live
    script by its own section markers rather than copy its logic, so an edit
    to the real block is what this test exercises.
    """
    text = RUN_SHARD.read_text()
    opener = "# --- host images: registry:3, the kind node image ---"
    closer = "# --- registry mirror ---"
    assert text.count(opener) == 1, "the host-images block is not defined exactly once in run_shard.sh"
    after = text.split(opener, 1)[1]
    assert closer in after, "no registry-mirror section follows the host-images block"
    return after.split(closer, 1)[0]


def _run_host_images_block(tmp_path, *, manifest_text: str, host_image_rows: str = "") -> tuple[Path, Path]:
    """Run the real block with a stubbed aws/docker, in an isolated WORKDIR.

    WORKDIR's scripts/ is a symlink to the real checkout so the block's own
    `. "$WORKDIR/scripts/benchmark/host_image_lib.sh"` and the final
    `write_run_manifest.sh --host-images` call both run the real files, while
    HOST_IMAGE_TMP ("$WORKDIR/host-images") and everything else stay under
    tmp_path rather than writing into the checkout.
    """
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "scripts").symlink_to(ROOT / "scripts")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "aws").write_text(FAKE_AWS)
    (bindir / "aws").chmod(0o755)
    (bindir / "docker").write_text(FAKE_DOCKER)
    (bindir / "docker").chmod(0o755)

    host_images = tmp_path / "host-images.tsv"
    host_images.write_text(host_image_rows)

    manifest = tmp_path / "manifest.txt"
    manifest.write_text(manifest_text)

    script = tmp_path / "host-images.sh"
    script.write_text(
        "set -uo pipefail\n"
        'terminate() { echo "TERMINATED: $1" >&2; exit 1; }\n'
        f"WORKDIR={workdir}\n"
        f"MANIFEST={manifest}\n"
        # Exported: the refresh runs write_run_manifest.sh as its own child
        # process (the real block's "bash ... write_run_manifest.sh" call),
        # which a bare assignment would not reach -- the same gap
        # test_bootstrap_manifest.py notes for this same variable.
        f"export SREGYM_HOST_IMAGE_MANIFEST={host_images}\n"
        'REGISTRY_CACHE_BUCKET="fake-bucket"\n'
        'REGION="us-east-1"\n' + _extract_host_images_block()
    )

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    return host_images, manifest


def test_the_block_appends_both_images_and_refreshes_the_manifest(tmp_path):
    """The end-to-end case the review finding describes.

    Before the fix, this ran clean (registry:3 and the kind node image both
    load and both land in the TSV) but the manifest's brain_go_host_images
    kept saying only the brain-go row that was there before the append,
    because nothing re-read the TSV after it changed.
    """
    host_images, manifest = _run_host_images_block(
        tmp_path,
        manifest_text=(
            "run_id=run-test\n"
            "stages=diagnosis\n"
            "brain_go_host_images=brain-brain-go:brain-go/brain:local@sha256:" + "a" * 64 + "\n"
        ),
        host_image_rows="brain-brain-go\tbrain-go/brain:local\tsha256:" + "a" * 64 + "\n",
    )

    rows = [line.split("\t") for line in host_images.read_text().splitlines() if line]
    assert [r[:2] for r in rows] == [
        ["brain-brain-go", "brain-go/brain:local"],
        ["registry3", "registry:3.1.2"],
        ["kindnode-x86", "jacksonarthurclark/aiopslab-kind-x86:latest"],
    ]

    fields = dict(line.split("=", 1) for line in manifest.read_text().strip().split("\n") if "=" in line)
    images = fields["brain_go_host_images"]
    assert f"registry3:registry:3.1.2@{_DIGEST}" in images, images
    assert f"kindnode-x86:jacksonarthurclark/aiopslab-kind-x86:latest@{_DIGEST}" in images, images
    # The pre-existing brain-go row survives the refresh; this is a
    # replacement of the one line, not a loss of what the first write knew.
    assert images.startswith("brain-brain-go:brain-go/brain:local@sha256:" + "a" * 64), images
    # Every other field in the manifest the bootstrap already wrote survives
    # the refresh untouched.
    assert fields["run_id"] == "run-test"
    assert fields["stages"] == "diagnosis"
    # Exactly one brain_go_host_images line: a refresh, not a second copy.
    assert manifest.read_text().count("\nbrain_go_host_images=") == 1


def test_the_block_is_best_effort_when_the_manifest_is_missing(tmp_path):
    """MANIFEST unset or absent must not fail the shard over provenance.

    The append and the load still have to succeed; only the refresh is
    skipped, the same `|| true` shape as the --stages refresh just above it
    in run_shard.sh.
    """
    host_images, manifest = _run_host_images_block(
        tmp_path,
        manifest_text="",
        host_image_rows="",
    )
    manifest.unlink()  # the block must tolerate MANIFEST naming nothing

    rows = [line.split("\t") for line in host_images.read_text().splitlines() if line]
    assert [r[:2] for r in rows] == [
        ["registry3", "registry:3.1.2"],
        ["kindnode-x86", "jacksonarthurclark/aiopslab-kind-x86:latest"],
    ]


def _write_manifest_host_images(tmp_path, manifest_text: str, host_image_rows: str | None) -> dict[str, str]:
    out = tmp_path / "manifest.txt"
    out.write_text(manifest_text)
    env = dict(os.environ)
    if host_image_rows is None:
        env.pop("SREGYM_HOST_IMAGE_MANIFEST", None)
    else:
        host_images = tmp_path / "host-images.tsv"
        host_images.write_text(host_image_rows)
        env["SREGYM_HOST_IMAGE_MANIFEST"] = str(host_images)

    proc = subprocess.run(
        ["bash", str(WRITE_MANIFEST), "--host-images", str(out)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return dict(line.split("=", 1) for line in out.read_text().strip().split("\n") if "=" in line)


def test_the_host_images_refresh_replaces_the_line_it_does_not_append_it(tmp_path):
    fields = _write_manifest_host_images(
        tmp_path,
        manifest_text="run_id=run-test\nbrain_go_host_images=stale\nstages=diagnosis\n",
        host_image_rows="pgvector-pgvector\tpgvector/pgvector:pg16\tsha256:" + "a" * 64 + "\n",
    )

    assert fields["brain_go_host_images"] == "pgvector-pgvector:pgvector/pgvector:pg16@sha256:" + "a" * 64
    assert fields["run_id"] == "run-test"
    assert fields["stages"] == "diagnosis"

    out = tmp_path / "manifest.txt"
    assert out.read_text().count("\nbrain_go_host_images=") == 1


def test_the_host_images_refresh_handles_an_absent_host_image_manifest(tmp_path):
    """SREGYM_HOST_IMAGE_MANIFEST unset, or naming a file that is not there,
    must write an empty field, the same shape a reader always gets, rather
    than fail the refresh."""
    fields = _write_manifest_host_images(
        tmp_path,
        manifest_text="run_id=run-test\nbrain_go_host_images=stale\n",
        host_image_rows=None,
    )
    assert fields["brain_go_host_images"] == ""
    assert fields["run_id"] == "run-test"


def test_the_host_images_refresh_fails_loudly_on_a_manifest_that_is_not_there():
    proc = subprocess.run(
        ["bash", str(WRITE_MANIFEST), "--host-images", "/nonexistent/manifest.txt"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "does not exist" in proc.stderr
