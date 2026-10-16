"""scripts/benchmark/image_cache.list cannot silently drift behind the code
that actually pulls images.

image_cache.list is hand-built (see its own header comment), so a new fault
injector, problem, or raw manifest that introduces an image literal is easy
to miss: that is exactly how otel-collector.yaml's own collector image and
two python:3.12 tags used by lite-reachable problems were absent from the
list despite being pulled on every run that uses them.

This scans every `.py` and `.yaml`/`.yml` file under `sregym/` and `kind/`
for an image literal and fails if one is not in image_cache.list, after
subtracting the deliberate-fault images (never meant to resolve) and the
list's own documented out-of-scope gaps (the kind node image, pulled by the
host rather than the in-cluster containerd this list covers).

This also covers the vendored third-party manifests under
sregym/resources/vendored/ and kind/manifests/: those are plain .yaml files
with no Chart.yaml of their own, so the chart-tree exclusion below does not
apply to them, and they scan like any
other raw manifest in this tree. The OpenEBS operator manifest sets its
helper image through two env vars (OPENEBS_IO_HELPER_IMAGE,
CLEANUP_JOB_IMAGE), not an "image:" key, so a second pattern below
(_YAML_ENV_IMAGE_RE) catches a "name: ...IMAGE...\\n  value: <ref>" pair the
same way the plain "image:" pattern catches a normal container spec.

Deliberately NOT scanned: any directory tree rooted at a vendored Helm
chart (one containing its own Chart.yaml). image_cache.list's own images
for those charts (prometheus, grafana, the astronomy-shop chart, ...) come
from a human running `helm template`, not a literal grep, because a chart's
own values can disable one of its defaults (astronomy_shop.py drops two of
astronomy-shop's bundled images this way) in a manner a flat grep cannot
see. Scanning those trees produced only noise: images from subcharts that
are never actually installed (filebeat, logstash) or a Helm test hook that
only runs under `helm test` (alertmanager's test-connection busybox). That
means this check cannot catch a Helm-chart-only gap; it catches the raw
manifest and Python-literal class of gap, which is what every miss found so
far turned out to be.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = [ROOT / "sregym", ROOT / "kind"]
IMAGE_LIST = ROOT / "scripts" / "benchmark" / "image_cache.list"

# A plain `image:` key, the raw-manifest shape. Used for .yaml/.yml files
# AND for a .py file's own embedded YAML text (a Deployment manifest built
# as a multi-line string, e.g. mutating_webhook_resource_limits.py's webhook
# server Deployment): a `.py` file is not just scanned by the three
# Python-syntax patterns below, since an "image:" key that never appears as
# real Python dict/kwarg/constant syntax, only inside a triple-quoted or
# f-string block, is invisible to all three of them. "image: str" (and
# similar bare type annotations with no default) would match this same
# shape, so _looks_like_image below stops a handful of common built-in type
# names from being mistaken for a real image reference.
_YAML_IMAGE_RE = re.compile(r'^\s*image:\s*"?([^"\s#]+)"?\s*$', re.MULTILINE)

# A Kubernetes env var entry naming an image through "value:" rather than an
# "image:" key, the shape the OpenEBS operator manifest's own
# OPENEBS_IO_HELPER_IMAGE and CLEANUP_JOB_IMAGE env vars take
# (sregym/resources/vendored/openebs-operator-v3.5.0.yaml). "IMAGE" anywhere
# in the env var's own name, matching the same naming convention this
# codebase's Python IMAGE-named constants already use, so a future vendored
# manifest's own helper-image env var is caught the same way without a new
# pattern.
_YAML_ENV_IMAGE_RE = re.compile(r'name:\s*(?:\S*IMAGE\S*)\s*\n\s*value:\s*"?([^"\s]+)"?', re.MULTILINE)

# The Python shapes an image literal actually takes in this tree: a dict
# literal's "image" key, an "image=" keyword argument, a module-level
# constant whose name contains IMAGE (the pattern this codebase already uses
# for a fault's own named image, e.g. INCORRECT_IMAGE_DEFAULT,
# WRONG_OPERATOR_IMAGE, including one with a leading underscore for a
# module-private constant, e.g. _CONTROLLER_IMAGE, _PROBE_IMAGE), and a
# `--image=<ref>` shell flag inside an f-string building a kubectl command
# (tidb_cluster_operator.py's "kubectl run mysql-client --image=mysql:8").
# The last one is not Python "image=" keyword-argument syntax at all (the
# value is a bare, unquoted token inside a larger quoted string), so it needs
# its own pattern rather than being folded into _PY_IMAGE_KW_RE.
_PY_IMAGE_DICT_RE = re.compile(r"""["']image["']\s*:\s*["']([^"']+)["']""")
_PY_IMAGE_KW_RE = re.compile(r"""\bimage\s*=\s*["']([^"']+)["']""")
_PY_IMAGE_CONST_RE = re.compile(r"""\b_*[A-Z][A-Z0-9_]*IMAGE[A-Z0-9_]*\s*=\s*["']([^"']+)["']""")
_PY_IMAGE_FLAG_RE = re.compile(r"""--image[= ]([A-Za-z0-9][\w./:@-]*)""")

# Two fixed fault images that must NEVER be seeded: the whole point of each
# is that it resolves on no registry. Keyed by the exact literal as it
# appears in source, normalized the same way a real image is below.
_NEVER_SEED = {"app-image:latest", "pingcap/tidb-operatorr:v1.6.3"}

# image_cache.list's own documented, deliberate gaps (see its header): the
# kind node image is pulled by the HOST's own `docker pull` when `kind
# create cluster` runs, not by the in-cluster containerd this list covers.
_KNOWN_GAPS = {
    "jacksonarthurclark/aiopslab-kind-x86:latest",
    "jacksonarthurclark/aiopslab-kind-arm:latest",
}


# _YAML_IMAGE_RE applied to a .py file can match a bare dataclass/field type
# annotation with no default value ("image: str"), the exact shape the old
# .yaml/.yml-only restriction existed to avoid (see that pattern's own
# comment). None of these appear today (a Python annotation on its own line
# is unusual outside a dataclass, and every current "image:" field in this
# tree has a value, not a bare type), but a bare one would read as a real
# image for every one of these names, so they are rejected on top of the
# general shape check below rather than relied on never to occur.
_PY_TYPE_ANNOTATION_NAMES = {
    "str",
    "int",
    "float",
    "bool",
    "bytes",
    "None",
    "Any",
    "list",
    "dict",
    "set",
    "tuple",
    "Path",
}


def _looks_like_image(value: str) -> bool:
    """Reject a regex match that is not actually an image reference.

    The IMAGE-named-constant pattern in particular over-matches: a loopback
    file path (DEFAULT_LOOP_IMAGE) or an env-var name string assigned to a
    constant whose own name contains IMAGE (MCP_IMAGE_ENV = "SREGYM_MCP_
    IMAGE") both match the same regex shape as a real image literal.
    """
    if not value or value.startswith("/") or " " in value:
        return False
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
        return False  # an identifier/env-var name, not an image reference
    if value in _PY_TYPE_ANNOTATION_NAMES:
        return False  # a bare Python type annotation, not an image reference
    return bool(re.fullmatch(r"[a-zA-Z0-9][\w.-]*(?:/[a-zA-Z0-9][\w.-]*)*(?::[\w][\w.-]*)?", value))


def _normalize(ref: str) -> str:
    """Docker's own implicit-registry rule: a bare name is docker.io/library/
    <name>, a name with one namespace segment and no dotted/ported/localhost
    host is docker.io/<name>, and anything else is already host-qualified.
    Matches how image_cache.list spells every docker.io path out in full
    (NOFIRE.md's registry mirror section has the path-layout story)."""
    if "/" not in ref:
        return f"docker.io/library/{ref}"
    first = ref.split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        return ref
    return f"docker.io/{ref}"


def _chart_roots() -> set[Path]:
    roots: set[Path] = set()
    for d in SCAN_DIRS:
        if not d.is_dir():
            continue
        for chart_yaml in d.rglob("Chart.yaml"):
            roots.add(chart_yaml.parent)
    return roots


def _in_chart_tree(path: Path, chart_roots: set[Path]) -> bool:
    return any(root in path.parents for root in chart_roots)


def _scan_images() -> dict[str, set[str]]:
    """Every normalized image literal found under SCAN_DIRS, mapped to the
    file(s) it came from (for a useful failure message)."""
    chart_roots = _chart_roots()
    found: dict[str, set[str]] = {}
    for scan_dir in SCAN_DIRS:
        if not scan_dir.is_dir():
            continue
        for path in scan_dir.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if _in_chart_tree(path, chart_roots):
                continue
            text = path.read_text(errors="ignore")
            if path.suffix in (".yaml", ".yml"):
                matches = (m for rx in (_YAML_IMAGE_RE, _YAML_ENV_IMAGE_RE) for m in rx.finditer(text))
            elif path.suffix == ".py":
                # _YAML_IMAGE_RE and _YAML_ENV_IMAGE_RE too, not just the
                # three Python-syntax patterns: a raw manifest built as a
                # Python string (a triple-quoted block, an f-string) carries
                # its "image:" key as plain YAML text, which none of the
                # Python-syntax patterns below ever match (mutating_webhook_
                # resource_limits.py's webhook Deployment is one such case).
                matches = (
                    m
                    for rx in (
                        _YAML_IMAGE_RE,
                        _YAML_ENV_IMAGE_RE,
                        _PY_IMAGE_DICT_RE,
                        _PY_IMAGE_KW_RE,
                        _PY_IMAGE_CONST_RE,
                        _PY_IMAGE_FLAG_RE,
                    )
                    for m in rx.finditer(text)
                )
            else:
                continue
            for m in matches:
                value = m.group(1)
                if _looks_like_image(value):
                    found.setdefault(_normalize(value), set()).add(str(path.relative_to(ROOT)))
    return found


def _listed_images() -> set[str]:
    listed = set()
    for line in IMAGE_LIST.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        listed.add(line)
    return listed


def test_image_cache_list_has_every_scanned_image():
    found = _scan_images()
    listed = _listed_images()
    excluded = {_normalize(ref) for ref in _NEVER_SEED | _KNOWN_GAPS}

    missing = sorted(set(found) - listed - excluded)
    assert not missing, (
        "image(s) referenced in sregym/ or kind/ but missing from "
        + str(IMAGE_LIST.relative_to(ROOT))
        + ":\n"
        + "\n".join(f"  {image}  (from {', '.join(sorted(found[image]))})" for image in missing)
    )


def test_never_seed_images_are_not_in_the_list():
    """app-image:latest and the operator typo tag must never be seeded: a
    container stuck pulling either is the fault under test, and the whole
    point of both is that they resolve on no registry."""
    listed = _listed_images()
    for ref in _NEVER_SEED:
        assert _normalize(ref) not in listed, f"{ref} must never be in image_cache.list"


def test_scan_finds_a_known_real_image():
    """A control for the scanner itself: if this starts failing, the regexes
    above stopped matching real source, and the completeness test above
    would pass for the wrong reason (finding nothing)."""
    found = _scan_images()
    assert "docker.io/jaegertracing/all-in-one:1.57" in found


def test_scan_finds_a_leading_underscore_constant_image():
    """_PY_IMAGE_CONST_RE used to anchor on a bare "[A-Z]" as the first
    character of the constant name. A word boundary is satisfied right
    before a leading underscore too, but "[A-Z]" itself never matches one,
    so finalizer_deadlock_controller.py's "_CONTROLLER_IMAGE = ..." was
    invisible to the scanner before this pattern allowed leading underscores.
    Removing that allowance and rerunning this test is how the regression
    was confirmed to fail."""
    found = _scan_images()
    assert any(
        "finalizer_deadlock_controller.py" in f for f in found.get("docker.io/library/python:3.12-alpine", set())
    )


def test_scan_finds_fstring_image_flag():
    """tidb_cluster_operator.py builds `kubectl run mysql-client
    --image=mysql:8` as an f-string: the value is a bare, unquoted token
    inside a larger quoted string, not Python "image=" keyword-argument
    syntax, so _PY_IMAGE_KW_RE never matched it."""
    found = _scan_images()
    assert "docker.io/library/mysql:8" in found
    assert any("tidb_cluster_operator.py" in f for f in found["docker.io/library/mysql:8"])


def test_scan_finds_embedded_yaml_in_py_string():
    """mutating_webhook_resource_limits.py builds a Deployment manifest as a
    Python string with its own "image:" line, plain YAML text that none of
    the three Python-syntax patterns ever match: _YAML_IMAGE_RE has to run
    against .py files too, not just .yaml/.yml."""
    found = _scan_images()
    assert any(
        "mutating_webhook_resource_limits.py" in f for f in found.get("docker.io/library/python:3.12-alpine", set())
    )


def test_bare_type_annotation_is_not_an_image():
    """A guard for applying _YAML_IMAGE_RE to .py files: a bare dataclass
    field annotation with no default ("image: str") matches the same regex
    shape as a real "image: <ref>" line and must not be mistaken for one."""
    assert not _looks_like_image("str")
    assert not _looks_like_image("Path")


def test_scan_finds_vendored_calico_and_metrics_server_images():
    """The vendored manifests under kind/manifests/ and sregym/resources/
    vendored/ are plain .yaml files with no Chart.yaml, so they scan like any
    other raw manifest, not like a Helm chart tree."""
    found = _scan_images()
    assert any("calico-v3.27.0.yaml" in f for f in found.get("docker.io/calico/node:v3.27.0", set()))
    assert any(
        "metrics-server-components-v0.9.0.yaml" in f
        for f in found.get("registry.k8s.io/metrics-server/metrics-server:v0.9.0", set())
    )


def test_scan_finds_openebs_helper_image_in_env_value():
    """The vendored OpenEBS operator manifest sets its helper image through
    OPENEBS_IO_HELPER_IMAGE/CLEANUP_JOB_IMAGE env vars, not an "image:" key:
    _YAML_ENV_IMAGE_RE has to exist and run against .yaml files, or removing
    this image from image_cache.list would go undetected. Confirmed by
    actually deleting the
    docker.io/openebs/linux-utils:3.5.0 line from image_cache.list and
    re-running test_image_cache_list_has_every_scanned_image, which failed
    naming that image, then restoring the line."""
    found = _scan_images()
    assert any("openebs-operator-v3.5.0.yaml" in f for f in found.get("docker.io/openebs/linux-utils:3.5.0", set()))
