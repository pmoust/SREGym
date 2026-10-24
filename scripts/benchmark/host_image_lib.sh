#!/usr/bin/env bash
# Shared "load one host-level image from the S3 host-image cache" step.
#
# A host-level image is one a box's own docker daemon pulls directly rather
# than one a kind node's containerd pulls through registry_mirror.sh:
# registry:3 and the kind node image (both `docker run` by the host itself),
# and brain-go's compose-declared images and build-base images (both pulled
# or built by the host's own docker compose / docker build, before kind or
# the registry mirror are involved at all). None of these go through the
# per-upstream mirror registries, so none of them belong in
# scripts/benchmark/image_cache.list; scripts/benchmark/seed_host_images.sh
# is the one list for all of them.
#
# Sourced, not executed: both callers (run_shard.sh, already running in the
# bootstrap's shell, and setup_brain_go.sh, its own process) need the
# `load_host_image` function in their own shell so a failure can reach their
# own fail-loud path (`terminate` in run_shard.sh, `die` in
# setup_brain_go.sh) rather than this file's.
#
# A box never calls `docker pull` for any of these: this function reads the
# pointer, downloads that exact tarball, `docker load`s it, and checks the
# loaded image's own ID against the pointer before returning success. Prints
# progress to stderr and returns 1 with an error on stderr for any failure;
# never exits the caller's shell itself, which is what lets a caller still
# run its own cleanup before dying.

load_host_image() {
  local name="$1" ref="$2" bucket="$3" region="$4" tmp_dir="$5"
  local digest hex tar loaded

  digest=$(aws s3 cp "s3://$bucket/host-images/${name}.digest" - --region "$region" 2>/dev/null) || {
    echo "[host-image] s3://$bucket/host-images/${name}.digest is missing; run scripts/benchmark/seed_host_images.sh --bucket $bucket (add --brain-go-ref <ref> if $name is one of brain-go's own host images) first" >&2
    return 1
  }
  hex="${digest#sha256:}"
  tar="$tmp_dir/${name}.tar"
  aws s3 cp "s3://$bucket/host-images/${name}-${hex}.tar" "$tar" --region "$region" --only-show-errors || {
    echo "[host-image] s3://$bucket/host-images/${name}-${hex}.tar is missing for $ref; re-run scripts/benchmark/seed_host_images.sh (add --brain-go-ref <ref> if $name is one of brain-go's own host images)" >&2
    return 1
  }
  docker load -i "$tar" >/dev/null || {
    echo "[host-image] docker load failed for $name ($tar)" >&2
    return 1
  }
  loaded=$(docker inspect --format '{{.Id}}' "$ref") || {
    echo "[host-image] docker inspect failed for $ref after loading $name" >&2
    return 1
  }
  if [ "$loaded" != "$digest" ]; then
    echo "[host-image] loaded $name image id $loaded does not match s3://$bucket/host-images/${name}.digest ($digest); re-run scripts/benchmark/seed_host_images.sh (add --brain-go-ref <ref> if $name is one of brain-go's own host images)" >&2
    return 1
  fi
  rm -f "$tar"
  echo "[host-image] loaded $name ($ref) from the cache bucket, verified $loaded" >&2
  return 0
}
