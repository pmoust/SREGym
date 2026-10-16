"""native_arm.sh's default edge agent image has to be in the registry cache.

scripts/benchmark/native_arm.sh's EDGE_IMAGE defaults to
"nofireai/edge:v3.6.0" and SREGYM_EDGE_IMAGE can override it per run.
run_shard.sh's preflight checks whichever image is actually effective at
run time, but that check cannot catch
a *default* that drifted out of image_cache.list ahead of time, since it
only runs on a real box with a real registry mirror. This test is the
ahead-of-time half: a plain text check that the hardcoded default is still
in the list, so a bump to native_arm.sh's own default without a matching
image_cache.list line fails here, in CI, rather than on the first box that
boots with no override set.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NATIVE_ARM = ROOT / "scripts" / "benchmark" / "native_arm.sh"
IMAGE_LIST = ROOT / "scripts" / "benchmark" / "image_cache.list"

_DEFAULT_RE = re.compile(r'EDGE_IMAGE="\$\{SREGYM_EDGE_IMAGE:-([^}]+)\}"')


def _default_edge_image() -> str:
    text = NATIVE_ARM.read_text()
    m = _DEFAULT_RE.search(text)
    assert m, "could not find native_arm.sh's EDGE_IMAGE default; did the variable get renamed?"
    return m.group(1)


def test_native_arm_default_edge_image_is_in_the_cache_list():
    default = _default_edge_image()
    listed = {line.strip() for line in IMAGE_LIST.read_text().splitlines() if line.strip() and not line.startswith("#")}
    assert f"docker.io/{default}" in listed, (
        f"native_arm.sh's default edge image ({default!r}) is not in {IMAGE_LIST.relative_to(ROOT)}"
    )
