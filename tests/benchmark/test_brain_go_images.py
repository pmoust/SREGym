"""brain_go_images.py: the pure derivation behind setup_brain_go.sh's
host-image gate and seed_host_images.sh's --brain-go-ref seeding.

No docker or git here: load_compose_config() is the only function that
shells out, and it is not exercised by these tests. Every other function
takes plain dicts/strings and is verified against fixture compose JSON and
Dockerfile text shaped like brain-go's real ones (confirmed separately,
read-only, against brain-go's own pinned refs and origin/main).
"""

from __future__ import annotations

import pytest

from scripts.benchmark import brain_go_images as bgi

DOCKERFILE = """# comment
ARG GO_VERSION=1.26.6
ARG BUILD_IMAGE=golang:${GO_VERSION}
ARG RUNTIME_IMAGE=alpine:3.20

FROM ${BUILD_IMAGE} AS build
ARG BINARY=brain
RUN echo building

FROM ${RUNTIME_IMAGE} AS runtime
COPY --from=build /out/app /app
"""


def test_resolve_dockerfile_from_images_substitutes_one_level_of_arg_reference():
    assert bgi.resolve_dockerfile_from_images(DOCKERFILE) == ["golang:1.26.6", "alpine:3.20"]


def test_resolve_dockerfile_from_images_honors_a_build_arg_override():
    images = bgi.resolve_dockerfile_from_images(DOCKERFILE, {"GO_VERSION": "1.27.0"})
    assert images == ["golang:1.27.0", "alpine:3.20"]


def test_resolve_dockerfile_from_images_with_a_literal_from_and_no_args():
    text = "FROM node:22.9.0-alpine\n"
    assert bgi.resolve_dockerfile_from_images(text) == ["node:22.9.0-alpine"]


def test_resolve_dockerfile_from_images_raises_on_an_unresolvable_reference():
    text = "ARG BUILD_IMAGE\nFROM ${BUILD_IMAGE}\n"
    with pytest.raises(bgi.DerivationError, match="BUILD_IMAGE"):
        bgi.resolve_dockerfile_from_images(text)


def test_resolve_dockerfile_from_images_skips_a_from_of_an_earlier_stage():
    text = "FROM golang:1.21 AS build\nFROM build AS test\nFROM alpine:3.20 AS runtime\n"
    assert bgi.resolve_dockerfile_from_images(text) == ["golang:1.21", "alpine:3.20"]


def test_resolve_dockerfile_from_images_with_a_platform_flag():
    text = "FROM --platform=linux/amd64 golang:1.21\n"
    assert bgi.resolve_dockerfile_from_images(text) == ["golang:1.21"]


def test_resolve_dockerfile_from_images_skips_scratch():
    text = "FROM scratch AS base\nFROM golang:1.21\n"
    assert bgi.resolve_dockerfile_from_images(text) == ["golang:1.21"]


def test_resolve_dockerfile_from_images_honors_a_bash_style_default():
    text = "FROM ${BUILD_IMAGE:-golang:1.21}\n"
    assert bgi.resolve_dockerfile_from_images(text) == ["golang:1.21"]
    assert bgi.resolve_dockerfile_from_images(text, {"BUILD_IMAGE": "golang:1.27.0"}) == ["golang:1.27.0"]


def test_resolve_dockerfile_from_images_raises_not_typeerror_on_a_null_build_arg():
    text = "ARG GO_VERSION\nFROM golang:${GO_VERSION}\n"
    with pytest.raises(bgi.DerivationError, match="GO_VERSION"):
        bgi.resolve_dockerfile_from_images(text, {"GO_VERSION": None})


def test_slugify_keeps_repository_and_tag_but_drops_a_digest():
    assert bgi.slugify("pgvector/pgvector:pg16") == "pgvector-pgvector__pg16"
    assert bgi.slugify("golang:1.26.6") == "golang__1.26.6"
    assert bgi.slugify("library/busybox@sha256:" + "a" * 64) == "library-busybox"


def test_slugify_distinguishes_two_tags_of_the_same_repository():
    assert bgi.slugify("golang:1.26.6") != bgi.slugify("golang:1.27.0")


def test_active_services_drops_a_profiled_service():
    config = {
        "services": {
            "api": {"image": "a:1"},
            "e2e": {"image": "golang:1.26.4", "profiles": ["e2e"]},
            "tailscale": {"image": "tailscale:1", "profiles": ["share"]},
        }
    }
    assert set(bgi.active_services(config)) == {"api"}


def test_required_host_images_combines_no_build_images_and_dockerfile_froms():
    config = {
        "services": {
            "postgres": {"image": "pgvector/pgvector:pg16"},
            "dashboard": {"image": "node:22.9.0-alpine"},
            "e2e": {"image": "golang:1.26.4", "profiles": ["e2e"]},
            "api": {
                "image": "brain-go/brain:local",
                "build": {"context": "/repo", "dockerfile": "docker/Dockerfile"},
            },
            "tworker": {
                "image": "brain-go/brain:local",
                "build": {"context": "/repo", "dockerfile": "docker/Dockerfile"},
            },
        }
    }
    reads = {"/repo/docker/Dockerfile": DOCKERFILE}
    result = bgi.required_host_images(config, lambda p: reads[p])

    # api and tworker share one (context, dockerfile) pair, so the
    # Dockerfile is "read" once; the build-keyed services' own "image"
    # (brain-go/brain:local, the tag `make up` assigns to what it just
    # built) never appears.
    assert result == [
        ("alpine__3.20", "alpine:3.20"),
        ("golang__1.26.6", "golang:1.26.6"),
        ("node__22.9.0-alpine", "node:22.9.0-alpine"),
        ("pgvector-pgvector__pg16", "pgvector/pgvector:pg16"),
    ]


def test_required_host_images_with_only_no_build_services():
    config = {"services": {"temporal": {"image": "temporalio/auto-setup:1.26.2"}}}
    result = bgi.required_host_images(config, lambda p: pytest.fail("should not read a Dockerfile"))
    assert result == [("temporalio-auto-setup__1.26.2", "temporalio/auto-setup:1.26.2")]


def test_required_host_images_ignores_a_service_with_neither_image_nor_build():
    config = {"services": {"weird": {}}}
    assert bgi.required_host_images(config, lambda p: "") == []
