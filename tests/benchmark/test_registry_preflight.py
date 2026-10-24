"""registry_preflight.py: the HTTP-only registry cache preflight that
replaced `docker pull`-into-the-host-store for run_shard.sh's ~108-image
check.

Exercised two ways: against a real local `registry:3` seeded with `crane`
(the two tests marked with the docker/crane skip conditions, which prove
"one present image passes, one with a deleted blob fails naming the blob")
and, for every other shape, against a
mocked urllib so the full matrix (manifest list resolution, a missing
manifest, a missing blob, no linux/amd64 platform) runs without Docker.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

from scripts.benchmark import registry_preflight as rp

IMAGE_CACHE_LIST = Path(__file__).resolve().parents[2] / "scripts" / "benchmark" / "image_cache.list"

CONFIG_DIGEST = "sha256:" + "a" * 64
LAYER_DIGEST = "sha256:" + "b" * 64
AMD64_CHILD_DIGEST = "sha256:" + "c" * 64
ARM64_CHILD_DIGEST = "sha256:" + "d" * 64


def _manifest_response(body: dict) -> bytes:
    return json.dumps(body).encode()


def _single_platform_manifest() -> dict:
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"digest": CONFIG_DIGEST},
        "layers": [{"digest": LAYER_DIGEST}],
    }


def _manifest_index() -> dict:
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {"digest": ARM64_CHILD_DIGEST, "platform": {"os": "linux", "architecture": "arm64"}},
            {"digest": AMD64_CHILD_DIGEST, "platform": {"os": "linux", "architecture": "amd64"}},
        ],
    }


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen_sequence(responses):
    """responses: list of (bytes | Exception) consumed in call order, one
    per urlopen() call regardless of whether it is the manifest GET or a
    blob HEAD."""
    it = iter(responses)

    def _fake(req, timeout=None):
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)

    return _fake


def test_single_platform_manifest_checks_config_and_every_layer():
    responses = [
        _manifest_response(_single_platform_manifest()),  # manifest GET
        b"",  # config HEAD
        b"",  # layer HEAD
    ]
    with mock.patch("urllib.request.urlopen", side_effect=_urlopen_sequence(responses)) as m:
        rp.check_image("5000", "library/busybox:1.36")
    assert m.call_count == 3
    urls = [c.args[0].full_url for c in m.call_args_list]
    assert urls[0] == "http://127.0.0.1:5000/v2/library/busybox/manifests/1.36"
    assert urls[1] == f"http://127.0.0.1:5000/v2/library/busybox/blobs/{CONFIG_DIGEST}"
    assert urls[2] == f"http://127.0.0.1:5000/v2/library/busybox/blobs/{LAYER_DIGEST}"


def test_manifest_list_resolves_the_linux_amd64_child_before_checking_blobs():
    responses = [
        _manifest_response(_manifest_index()),  # top-level index GET
        _manifest_response(_single_platform_manifest()),  # resolved child manifest GET
        b"",  # config HEAD
        b"",  # layer HEAD
    ]
    with mock.patch("urllib.request.urlopen", side_effect=_urlopen_sequence(responses)) as m:
        rp.check_image("5000", "library/busybox:1.36")
    urls = [c.args[0].full_url for c in m.call_args_list]
    assert urls[1] == f"http://127.0.0.1:5000/v2/library/busybox/manifests/{AMD64_CHILD_DIGEST}"


def test_manifest_list_with_no_linux_amd64_entry_raises():
    index = _manifest_index()
    index["manifests"] = [{"digest": ARM64_CHILD_DIGEST, "platform": {"os": "linux", "architecture": "arm64"}}]
    responses = [_manifest_response(index)]
    with (
        mock.patch("urllib.request.urlopen", side_effect=_urlopen_sequence(responses)),
        pytest.raises(rp.PreflightError, match="no linux/amd64 entry"),
    ):
        rp.check_image("5000", "library/busybox:1.36")


def test_missing_manifest_names_the_image_and_the_url():
    err = urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)
    with (
        mock.patch("urllib.request.urlopen", side_effect=_urlopen_sequence([err])),
        pytest.raises(rp.PreflightError, match="HTTP 404 fetching manifest"),
    ):
        rp.check_image("5000", "library/does-not-exist:1.0")


def test_missing_blob_names_the_digest_and_the_url():
    err = urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)
    responses = [
        _manifest_response(_single_platform_manifest()),
        b"",  # config HEAD ok
        err,  # layer HEAD missing
    ]
    with (
        mock.patch("urllib.request.urlopen", side_effect=_urlopen_sequence(responses)),
        pytest.raises(rp.PreflightError, match=f"HTTP 404 for blob .*{LAYER_DIGEST}"),
    ):
        rp.check_image("5000", "library/busybox:1.36")


def test_main_exits_zero_on_success(capsys):
    responses = [_manifest_response(_single_platform_manifest()), b"", b""]
    with mock.patch("urllib.request.urlopen", side_effect=_urlopen_sequence(responses)):
        code = rp.main(["registry_preflight.py", "5000", "library/busybox:1.36"])
    assert code == 0


def test_main_exits_one_and_prints_the_image_on_failure(capsys):
    err = urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)
    with mock.patch("urllib.request.urlopen", side_effect=_urlopen_sequence([err])):
        code = rp.main(["registry_preflight.py", "5000", "library/does-not-exist:1.0"])
    assert code == 1
    out = capsys.readouterr().err
    assert "library/does-not-exist:1.0" in out


def test_main_rejects_wrong_argument_count():
    assert rp.main(["registry_preflight.py", "5000"]) == 2


# --- split_ref / parse_cache_line -------------------------------------
# docker.io/polinux/stress has no tag at all, which the old
# `ref.rpartition(":")` parse raised a bare ValueError-shaped PreflightError
# on ("'polinux/stress' is not a <path>:<tag> reference"), aborting every
# box's preflight before any slot started.


def test_split_ref_with_a_tag():
    assert rp.split_ref("library/busybox:1.36") == ("library/busybox", "1.36")


def test_split_ref_with_no_tag_defaults_to_latest():
    assert rp.split_ref("polinux/stress") == ("polinux/stress", "latest")


def test_split_ref_with_a_multi_segment_path_and_no_tag():
    # The path itself can contain more "/"s; only the final segment decides
    # whether a tag is present.
    assert rp.split_ref("open-telemetry/demo") == ("open-telemetry/demo", "latest")


def test_split_ref_with_a_digest():
    digest = "sha256:" + "a" * 64
    assert rp.split_ref(f"library/busybox@{digest}") == ("library/busybox", digest)


def test_split_ref_rejects_a_bare_colon():
    with pytest.raises(rp.PreflightError):
        rp.split_ref(":1.36")


def test_split_ref_rejects_a_bare_at():
    with pytest.raises(rp.PreflightError):
        rp.split_ref("@sha256:" + "a" * 64)


def test_parse_cache_line_splits_host_path_and_reference():
    assert rp.parse_cache_line("docker.io/library/busybox:1.36") == ("docker.io", "library/busybox", "1.36")


def test_parse_cache_line_with_the_untagged_polinux_stress_line():
    assert rp.parse_cache_line("docker.io/polinux/stress") == ("docker.io", "polinux/stress", "latest")


def test_parse_cache_line_rejects_a_line_with_no_slash():
    with pytest.raises(rp.PreflightError):
        rp.parse_cache_line("docker.io")


def test_check_image_with_no_tag_requests_latest():
    responses = [_manifest_response(_single_platform_manifest()), b"", b""]
    with mock.patch("urllib.request.urlopen", side_effect=_urlopen_sequence(responses)) as m:
        rp.check_image("5000", "polinux/stress")
    urls = [c.args[0].full_url for c in m.call_args_list]
    assert urls[0] == "http://127.0.0.1:5000/v2/polinux/stress/manifests/latest"


def test_every_real_image_cache_list_line_parses():
    """Run the parser over every non-comment line of the real file, not
    just the handful of shapes above,
    so a future tagless or digest-pinned addition is caught by this test
    before it reaches a benchmark box's preflight."""
    assert IMAGE_CACHE_LIST.is_file(), f"{IMAGE_CACHE_LIST} not found"
    lines = [
        raw.strip()
        for raw in IMAGE_CACHE_LIST.read_text().splitlines()
        if raw.strip() and not raw.strip().startswith("#")
    ]
    assert lines, "image_cache.list has no image lines to check"
    for line in lines:
        host, path, reference = rp.parse_cache_line(line)
        assert host and path and reference, f"{line!r} parsed to an empty component"


# --- real registry, crane-seeded (skipped unless docker/crane are present) ---

_HAS_DOCKER_AND_CRANE = shutil.which("docker") is not None and shutil.which("crane") is not None


def _docker_daemon_reachable() -> bool:
    if not _HAS_DOCKER_AND_CRANE:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0


@pytest.mark.skipif(not _docker_daemon_reachable(), reason="needs a reachable docker daemon and crane")
def test_real_registry_present_image_passes_and_deleted_blob_fails(tmp_path: Path):
    """Seed a local plain registry with crane, confirm the preflight
    passes, delete one
    blob's data file on disk, restart the registry (distribution caches an
    already-open blob read, so a live container keeps serving a just-deleted
    file until it restarts), and confirm the preflight fails, naming that
    blob."""
    name = "sregym-preflight-pytest-registry"
    data_dir = tmp_path / "registry-data"
    data_dir.mkdir()
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
    subprocess.run(
        ["docker", "run", "-d", "--name", name, "-p", "0:5000", "-v", f"{data_dir}:/var/lib/registry", "registry:3.1.2"],
        check=True,
        capture_output=True,
        timeout=30,
    )

    def _published_port() -> str:
        return subprocess.run(
            ["docker", "inspect", "-f", '{{(index (index .NetworkSettings.Ports "5000/tcp") 0).HostPort}}', name],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()

    try:
        port = _published_port()

        for _ in range(30):
            if (
                subprocess.run(
                    ["curl", "-sf", "-o", "/dev/null", f"http://127.0.0.1:{port}/v2/_catalog"], timeout=5
                ).returncode
                == 0
            ):
                break
            time.sleep(1)
        else:
            pytest.fail("test registry did not become ready")

        subprocess.run(
            ["crane", "copy", "--platform", "linux/amd64", "busybox:1.36", f"localhost:{port}/library/busybox:1.36"],
            check=True,
            capture_output=True,
            timeout=120,
        )

        rp.check_image(port, "library/busybox:1.36")  # present: must not raise

        manifest = json.loads(
            subprocess.run(
                [
                    "curl",
                    "-s",
                    f"http://127.0.0.1:{port}/v2/library/busybox/manifests/1.36",
                    "-H",
                    "Accept: application/vnd.oci.image.manifest.v1+json",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            ).stdout
        )
        layer_digest = manifest["layers"][0]["digest"]
        blob_path = data_dir / "docker" / "registry" / "v2" / "blobs" / "sha256" / layer_digest[7:9] / layer_digest[7:]
        shutil.rmtree(blob_path)

        # `-p 0:5000` (a free host port, so parallel test runs never collide)
        # means a restart can remap to a *different* host port; re-read it
        # rather than reusing the pre-restart value.
        subprocess.run(["docker", "restart", name], check=True, capture_output=True, timeout=30)
        port = _published_port()
        for _ in range(30):
            if (
                subprocess.run(
                    ["curl", "-sf", "-o", "/dev/null", f"http://127.0.0.1:{port}/v2/_catalog"], timeout=5
                ).returncode
                == 0
            ):
                break
            time.sleep(1)
        else:
            pytest.fail("test registry did not become ready after restart")

        with pytest.raises(rp.PreflightError, match=layer_digest):
            rp.check_image(port, "library/busybox:1.36")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
