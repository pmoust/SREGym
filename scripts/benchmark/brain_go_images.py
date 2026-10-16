#!/usr/bin/env python3
"""Derive the exact set of host-level images brain-go's own `make up` pulls
or builds from, for whichever brain-go ref is checked out at the path this
runs from.

`make up` runs `docker compose --env-file deploy/.env up -d` with no
`--profile` flag, against the root docker-compose.yml (which `include:`s
deploy/docker-compose.yml and deploy/docker-compose.clickstack.yml) and no
override file. Two kinds of image this box's own docker daemon needs,
outside kind and outside the registry mirror entirely:

  - A service with no `build:` key pulls its `image:` tag directly
    (postgres, localstack, temporal, temporal-ui, dashboard, entry today).
  - A service with a `build:` key builds FROM an image named by that
    Dockerfile's own ARG defaults (docker/Dockerfile's GO_VERSION-derived
    BUILD_IMAGE for the build stage, RUNTIME_IMAGE for the final stage
    today), which still has to be pulled once before the build can start.
    The service's own `image:` (brain-go/brain:local, brain-go/brainctl:
    local) is the tag `make up` assigns to what it just built, not
    something to pull, so a build-keyed service never contributes its own
    `image:` value here.

A service with a non-empty `profiles:` list is not part of this (e2e,
share, observability): `make up` activates no profile, so compose never
starts it and it needs no host image cached. In practice `docker compose
config` itself already drops a service whose profile is not enabled, but
the check below is kept in case a future compose version changes that.

This module is read two ways: setup_brain_go.sh runs it live, with cwd
inside the exact brain-go checkout a benchmark box is about to run, as the
gate that must pass before `make up`; seed_host_images.sh derives the same
set against a chosen ref read-only, from a developer machine, to decide
what to cache (see that script's --brain-go-ref). Both share the pure
functions below so the two can never drift into checking a different set
than they seed; main() is the thin CLI that shells out to `docker compose
config` against whatever brain-go checkout is the current working
directory.

Usage (run with cwd at a brain-go checkout, after `./scripts/dev-env.sh
ensure` or any deploy/.env has been created):
    brain_go_images.py
Prints one "<name>\t<image ref>" line per required image, sorted by name.
<name> is a filesystem/S3-key-safe slug of the image's repository and tag
(both, not the repository alone: two brain_go_refs pinning different tags of
the same repository, e.g. a GO_VERSION bump, must not collapse onto the same
cache key), used as the host-image cache's key: see seed_host_images.sh and
host_image_lib.sh.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ARG_RE = re.compile(r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:=(.*))?\s*$")
FROM_RE = re.compile(
    r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+[Aa][Ss]\s+(\S+))?\s*$",
    re.IGNORECASE,
)
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")


class DerivationError(Exception):
    """The compose config or a Dockerfile it points at could not be resolved
    into a concrete image set. str(e) names what and why."""


def _substitute(value: str, known: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1) or m.group(3)
        default = m.group(2)
        if name in known:
            return known[name]
        if default is not None:
            return _substitute(default, known)
        raise DerivationError(f"'{value}' references ${{{name}}} with no ARG default and no build-arg override")

    return VAR_RE.sub(repl, value)


def resolve_dockerfile_from_images(text: str, build_args: dict[str, str] | None = None) -> list[str]:
    """Every FROM image reference in a Dockerfile, in file order, with ARG
    defaults (or a build.args override, which wins) substituted in. Handles
    an ARG with a literal, ${VAR:-default}, or already-known default,
    declared before the FROM that references it; a FROM's own
    --platform=<...> flag; "FROM scratch", which names no image to pull; and
    a FROM naming an earlier build stage by its "AS <name>" rather than a
    real image, which is a reference to this same Dockerfile, not something
    to pull. An ARG declared with no default and never overridden, then
    referenced by a FROM, raises rather than guessing; so does a
    build.args value of null, which a compose file's YAML can express but
    which no FROM can resolve against."""
    known: dict[str, str] = {}
    for name, value in (build_args or {}).items():
        if value is None:
            raise DerivationError(f"build.args sets '{name}' to null, which no FROM reference can be resolved against")
        known[name] = str(value)

    stage_names: set[str] = set()
    froms: list[str] = []
    for line in text.splitlines():
        m = ARG_RE.match(line)
        if m:
            name, default = m.group(1), m.group(2)
            if name not in known and default is not None:
                known[name] = _substitute(default.strip(), known)
            continue
        m = FROM_RE.match(line)
        if m:
            image_ref, stage_name = m.group(1), m.group(2)
            resolved = _substitute(image_ref, known)
            if resolved.lower() != "scratch" and resolved not in stage_names:
                froms.append(resolved)
            if stage_name:
                stage_names.add(stage_name)
    return froms


def active_services(compose_config: dict) -> dict:
    """Services `docker compose ... up -d` (no --profile) actually starts: a
    service with a non-empty "profiles" list needs that profile enabled
    first, which make up's plain invocation never does."""
    return {name: svc for name, svc in (compose_config.get("services") or {}).items() if not svc.get("profiles")}


def slugify(image_ref: str) -> str:
    """A stable, S3-key-safe name for an image's repository AND tag, so two
    brain_go_refs pinning different tags of the same repository (a
    GO_VERSION bump changing the build-stage base image, for example) get
    distinct host-image cache keys instead of overwriting each other's
    pointer. A digest reference (no tag) collapses to its repository alone,
    since nothing upstream of this derivation passes one today.
    "pgvector/pgvector:pg16" -> "pgvector-pgvector__pg16".
    "golang:1.26.6" -> "golang__1.26.6"."""
    without_digest = image_ref.split("@", 1)[0]
    repo, sep, tag = without_digest.rpartition(":")
    if not sep:
        repo, tag = without_digest, ""
    slug = repo.replace("/", "-")
    return f"{slug}__{tag}" if tag else slug


def required_host_images(compose_config: dict, read_dockerfile: Callable[[str], str]) -> list[tuple[str, str]]:
    """Returns [(slug, image_ref), ...], deduplicated and sorted by slug.
    read_dockerfile(path) returns that Dockerfile's text; the caller decides
    how to read it (the live filesystem for the runtime gate, `git show
    <ref>:<path>` for a read-only derivation against a ref not checked
    out)."""
    images: set[str] = set()
    build_specs: dict[str, dict[str, str]] = {}
    for svc in active_services(compose_config).values():
        build = svc.get("build")
        if not build:
            image = svc.get("image")
            if image:
                images.add(image)
            continue
        dockerfile = build.get("dockerfile") or "Dockerfile"
        context = build.get("context") or "."
        path = dockerfile if Path(dockerfile).is_absolute() else str(Path(context, dockerfile))
        # First service wins when two share a (context, dockerfile): today
        # none of the differing build.args (seed's BINARY=brainctl) touch
        # GO_VERSION/BUILD_IMAGE/RUNTIME_IMAGE, so this cannot change the
        # result; a future arg that did would need a per-service entry
        # instead of this dedup.
        build_specs.setdefault(path, dict(build.get("args") or {}))
    for path, args in build_specs.items():
        text = read_dockerfile(path)
        images.update(resolve_dockerfile_from_images(text, args))
    return sorted((slugify(ref), ref) for ref in images)


def load_compose_config(cwd: str | None = None) -> dict:
    result = subprocess.run(
        ["docker", "compose", "--env-file", "deploy/.env", "config", "--format", "json"],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=60,
    )
    if result.returncode != 0:
        raise DerivationError(f"docker compose config failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise DerivationError(f"docker compose config did not return valid JSON: {e}") from e


def main(argv: list[str]) -> int:
    try:
        compose_config = load_compose_config()
        images = required_host_images(compose_config, lambda p: Path(p).read_text())
    except DerivationError as e:
        print(f"brain_go_images.py: {e}", file=sys.stderr)
        return 1
    for name, ref in images:
        print(f"{name}\t{ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
