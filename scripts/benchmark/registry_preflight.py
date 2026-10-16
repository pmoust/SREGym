#!/usr/bin/env python3
"""Check one image_cache.list entry is fully present in the registry cache,
over plain HTTP, without pulling it into the box's own docker store.

run_shard.sh's preflight used to `docker pull` all ~108 images in
image_cache.list through the local mirror before any slot started. That
genuinely proves the path layout matches (the same request a kind node's
containerd would make), but it also fills the host's docker image store with
every image the benchmark uses, on every box, for images that box's own
docker daemon never otherwise touches.

This does almost the same proof with none of the storage cost: GET the
manifest (resolving a manifest list/index to its linux/amd64 child first,
the same platform selection containerd does), then HEAD the config blob and
every layer blob the resolved manifest names. A HEAD never downloads the
blob's bytes into anything; it only confirms the registry can serve them.
That is the same two-request shape (manifest, then blobs) a real pull makes,
short of actually writing the layer bytes to disk, so a genuinely missing
blob (a corrupted or partial crane copy, an object deleted from the bucket
after seeding) is still caught here, by name, before any slot starts.

Today's registries never actually serve a manifest list: seed_image_cache.sh
copies every image with `crane copy --platform linux/amd64`, which flattens
a multi-arch source straight to the one platform's manifest (confirmed with
a local registry: `crane copy --platform linux/amd64 python:3.12-slim ...`
pushes a single `application/vnd.oci.image.manifest.v1+json`, never an
index). The index-resolution branch below is kept anyway: it is the one
thing standing between "a future seeding change starts preserving the
index" and this preflight silently checking the wrong platform's blobs.

Usage:
    registry_preflight.py <port> <path[:tag|@digest]>

<port> is the local port registry_mirror.sh bound this upstream's mirror to
(127.0.0.1:<port>); the second argument is an image_cache.list line's path
component (everything after the first "/", e.g. "library/busybox:1.36").
split_ref() below accepts a plain tag ("library/busybox:1.36"), a digest
("library/busybox@sha256:..."), or a bare path with neither
("polinux/stress"), which means ":latest", the same default a real pull
applies. Exits 0 and prints nothing on success. Exits 1 and prints one line
per failure, naming the image and the manifest or blob URL that failed, on
any failure.

No third-party dependencies: both curl and python3 are the only tools this
box can be assumed to have (no crane), and python3's stdlib is enough for
two plain HTTP verbs and a JSON body.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 15

# Every manifest shape this registry might serve: Docker's own v2 manifest
# and manifest-list media types, and their OCI equivalents (most images
# built in the last few years are OCI, even on Docker Hub).
_MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
    ]
)

_INDEX_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
}


class PreflightError(Exception):
    """A manifest or blob this image needs is not servable. str(e) names
    exactly what and where, for the caller's own error message."""


def _get(url: str, accept: str) -> dict:
    req = urllib.request.Request(url, method="GET", headers={"Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise PreflightError(f"HTTP {e.code} fetching manifest {url}") from e
    except urllib.error.URLError as e:
        raise PreflightError(f"{e.reason} fetching manifest {url}") from e
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise PreflightError(f"manifest {url} did not return valid JSON: {e}") from e


def _head(url: str) -> None:
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS):
            return
    except urllib.error.HTTPError as e:
        raise PreflightError(f"HTTP {e.code} for blob {url}") from e
    except urllib.error.URLError as e:
        raise PreflightError(f"{e.reason} for blob {url}") from e


def _pick_linux_amd64(index: dict, base: str) -> str:
    for m in index.get("manifests", []):
        platform = m.get("platform") or {}
        if platform.get("os") == "linux" and platform.get("architecture") == "amd64":
            return m["digest"]
    raise PreflightError(f"no linux/amd64 entry in the manifest list at {base}")


def split_ref(ref: str) -> tuple[str, str]:
    """Split an image_cache.list line's path component ("<path>:<tag>",
    "<path>@sha256:<digest>", or a bare "<path>" with no tag or digest at
    all) into (path, reference).

    A bare path with no ":" and no "@" in its final "/"-segment means
    ":latest", the same default a real pull applies (docker.io/polinux/
    stress is one such line: every box aborted at preflight on it because an
    rpartition(":") over the whole ref raises when the path itself contains
    no colon). Checking only the final segment, not the whole ref, matters
    because <path> itself can contain more "/"s (open-telemetry/demo) with
    no ":" of their own.
    """
    last_segment = ref.rpartition("/")[2]
    if "@" in last_segment:
        path, sep, digest = ref.rpartition("@")
        if not sep or not path or not digest:
            raise PreflightError(f"{ref!r} is not a <path>@<digest> reference")
        return path, digest
    if ":" in last_segment:
        path, sep, tag = ref.rpartition(":")
        if not sep or not path:
            raise PreflightError(f"{ref!r} is not a <path>:<tag> reference")
        return path, tag
    return ref, "latest"


def parse_cache_line(line: str) -> tuple[str, str, str]:
    """Split one image_cache.list line ("<host>/<path>:<tag>") into (host,
    path, reference), matching run_shard.sh's own host/path split (the host
    is everything up to the first "/") plus split_ref's tag/digest/bare-ref
    handling above. Used by the completeness test below so every real line
    in the file is proven to parse without error, not just the handful of
    shapes the unit tests construct by hand."""
    host, sep, rest = line.partition("/")
    if not sep or not host:
        raise PreflightError(f"{line!r} has no '/' separating a registry host from its path")
    path, reference = split_ref(rest)
    return host, path, reference


def check_image(port: str, ref: str) -> None:
    """ref is an image_cache.list line's path component: "<path>:<tag>",
    "<path>@sha256:<digest>", or a bare "<path>" (defaulting to ":latest"),
    the same shapes split_ref above accepts. Raises PreflightError naming
    the image and the failing manifest/blob on any gap; returns normally
    when every blob the resolved manifest names is servable."""
    path, reference = split_ref(ref)
    base = f"http://127.0.0.1:{port}/v2/{path}"

    manifest = _get(f"{base}/manifests/{reference}", _MANIFEST_ACCEPT)
    is_index = manifest.get("mediaType") in _INDEX_MEDIA_TYPES or ("manifests" in manifest and "layers" not in manifest)
    if is_index:
        child_digest = _pick_linux_amd64(manifest, f"{base}/manifests/{reference}")
        manifest = _get(f"{base}/manifests/{child_digest}", _MANIFEST_ACCEPT)

    config = manifest.get("config") or {}
    config_digest = config.get("digest")
    if config_digest:
        _head(f"{base}/blobs/{config_digest}")

    for layer in manifest.get("layers", []):
        digest = layer.get("digest")
        if digest:
            _head(f"{base}/blobs/{digest}")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <port> <path:tag>", file=sys.stderr)
        return 2
    port, ref = argv[1], argv[2]
    try:
        check_image(port, ref)
    except PreflightError as e:
        print(f"{ref}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
