"""sregym.conductor.conductor._harness_cache_images: every spelling a pod
spec might actually write for a cached image.

_harness_cache_images is lru_cache'd on zero arguments, so each test clears
that cache first and monkeypatches the module's own _IMAGE_CACHE_LIST path
rather than depending on the real scripts/benchmark/image_cache.list, whose
contents this test must not need to track.
"""

from pathlib import Path

from sregym.conductor import conductor


def _images_for(tmp_path: Path, monkeypatch, lines: list[str]) -> frozenset[str]:
    list_path = tmp_path / "image_cache.list"
    list_path.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(conductor, "_IMAGE_CACHE_LIST", list_path)
    conductor._harness_cache_images.cache_clear()
    return conductor._harness_cache_images()


def test_docker_io_library_image_adds_every_short_form(tmp_path, monkeypatch):
    images = _images_for(tmp_path, monkeypatch, ["docker.io/library/busybox:1.36"])
    assert "docker.io/library/busybox:1.36" in images
    assert "library/busybox:1.36" in images
    assert "busybox:1.36" in images


def test_docker_io_namespaced_image_adds_the_short_form(tmp_path, monkeypatch):
    images = _images_for(tmp_path, monkeypatch, ["docker.io/nofireai/edge:v3.6.0"])
    assert "docker.io/nofireai/edge:v3.6.0" in images
    assert "nofireai/edge:v3.6.0" in images
    # No bare "edge:v3.6.0" form: unlike library/, a namespaced path's first
    # segment is not droppable.
    assert "edge:v3.6.0" not in images


def test_latest_tagged_image_also_adds_the_untagged_form(tmp_path, monkeypatch):
    """Docker's own implicit-tag rule: a reference with no tag at all means
    ":latest". A pod spec that writes the bare, untagged form must still
    match a ":latest"-tagged cache entry."""
    images = _images_for(tmp_path, monkeypatch, ["docker.io/library/busybox:latest"])
    assert "docker.io/library/busybox:latest" in images
    assert "library/busybox:latest" in images
    assert "busybox:latest" in images
    assert "busybox" in images
    assert "library/busybox" in images
    assert "docker.io/library/busybox" in images


def test_non_latest_image_gets_no_untagged_form(tmp_path, monkeypatch):
    images = _images_for(tmp_path, monkeypatch, ["docker.io/library/busybox:1.36"])
    assert "busybox" not in images


def test_non_docker_io_image_is_unaffected(tmp_path, monkeypatch):
    images = _images_for(tmp_path, monkeypatch, ["ghcr.io/open-feature/flagd:latest"])
    assert "ghcr.io/open-feature/flagd:latest" in images
    assert "ghcr.io/open-feature/flagd" in images
    assert "open-feature/flagd:latest" not in images
