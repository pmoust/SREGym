#!/usr/bin/env bash
# Start, stop, and address the per-upstream registry:2 containers behind the
# benchmark's image cache (NOFireAI/sregym#150). Shared by seed_image_cache.sh
# (store mode, run once from a developer machine to fill the S3 bucket via
# `crane copy`) and run_shard.sh (plain mode, run on every benchmark box to
# serve kind nodes read-only from that same bucket, never Docker Hub, ghcr.io,
# quay.io, or registry.k8s.io directly). See NOFIRE.md's registry mirror
# section for the design and scripts/benchmark/image_cache.list for the
# seeded image list.
#
# One registry:2 container per upstream host, not one combined registry: a
# registry's own repository namespace is per-container, and keeping that 1:1
# shape avoids ever having to prove two different upstreams' repository paths
# cannot collide in one shared S3 prefix.
#
# Usage:
#   registry_mirror.sh hosts
#   registry_mirror.sh port <host>
#   registry_mirror.sh root <host>
#   registry_mirror.sh slug <host>
#   registry_mirror.sh upstream-url <host>
#   registry_mirror.sh host-for <ref>
#   registry_mirror.sh path-for <ref>
#   registry_mirror.sh start-store <host> <bucket> <region> [bind_addr]
#   registry_mirror.sh start-plain <host> <bucket> <region> [bind_addr] [network]
#   registry_mirror.sh stop <host>
#
# start-plain's [network] is "bridge" (default, published with "-p", for a
# developer machine) or "host" (no "-p", the registry binds bind_addr:port on
# the real network stack itself; see the --network host comment in _start
# below for why the box always passes this). start-store never takes a
# network argument: it only ever runs on a developer machine, over "-p", the
# same as "bridge" plain.
#
# Environment:
#   SREGYM_REGISTRY_STORAGE_DRIVER   "s3" (default) or "filesystem", for the
#                                    local smoke test in the PR that added
#                                    this (no AWS credentials needed).
#   SREGYM_REGISTRY_DATA_DIR         filesystem driver only: the local
#                                    directory to store blobs under. Required
#                                    when the driver is "filesystem".

set -uo pipefail

log() { echo "[registry-mirror] $*" >&2; }
die() { echo "[registry-mirror] ERROR: $*" >&2; exit 1; }

# --- the four upstream hosts this benchmark mirrors ------------------------
# Extending to a fifth upstream is one more `|` arm in each of the three
# lookups below, plus that host's images in image_cache.list. No associative
# arrays: this runs on macOS's bash 3.2 (no `declare -A`) as well as the
# box's bash, so a case statement is the portable form.

cmd_hosts() {
  printf '%s\n' docker.io ghcr.io quay.io registry.k8s.io
}

_known_host() {
  case "$1" in
    docker.io | ghcr.io | quay.io | registry.k8s.io) return 0 ;;
    *) return 1 ;;
  esac
}

cmd_port() {
  case "$1" in
    docker.io) echo 5000 ;;
    ghcr.io) echo 5001 ;;
    quay.io) echo 5002 ;;
    registry.k8s.io) echo 5003 ;;
    *) die "unknown upstream host '$1' (see: $0 hosts)" ;;
  esac
}

# A plain directory-name-safe slug, used for the container name and (via
# cmd_root) the S3 key prefix. registry.k8s.io's two dots would otherwise
# collide with the config file's own YAML and shell quoting more often than
# the other three hosts' single dot.
cmd_slug() {
  case "$1" in
    docker.io) echo docker-io ;;
    ghcr.io) echo ghcr-io ;;
    quay.io) echo quay-io ;;
    registry.k8s.io) echo registry-k8s-io ;;
    *) die "unknown upstream host '$1' (see: $0 hosts)" ;;
  esac
}

# Separate S3 prefix per upstream. Not load-bearing for correctness today:
# no two upstreams in image_cache.list share a repository path, but a
# future image could share one (two different orgs publishing a same-named
# repo on two different registries), and this is one line to keep it
# impossible rather than a fact to keep re-checking by hand.
cmd_root() {
  echo "/$(cmd_slug "$1")"
}

# The upstream a `crane copy` source reference resolves against. docker.io is
# the one host where that is not just "https://<host>": the Hub's actual pull
# endpoint is registry-1.docker.io, which is also what every containerd/
# Docker client resolves "docker.io" to before making a request. Kept even
# though crane itself normalizes "docker.io" the same way a normal pull
# would, since seed_image_cache.sh's log lines name the upstream host it
# copied from, and registry-1.docker.io is the one that is actually accurate.
cmd_upstream_url() {
  case "$1" in
    docker.io) echo "https://registry-1.docker.io" ;;
    ghcr.io) echo "https://ghcr.io" ;;
    quay.io) echo "https://quay.io" ;;
    registry.k8s.io) echo "https://registry.k8s.io" ;;
    *) die "unknown upstream host '$1' (see: $0 hosts)" ;;
  esac
}

# --- image_cache.list line helpers -----------------------------------------
# A line is "<host>/<path>:<tag>". <path> itself may contain slashes
# (open-telemetry/demo), so split on the FIRST slash only, not every slash.

cmd_host_for() {
  local ref="$1"
  echo "${ref%%/*}"
}

cmd_path_for() {
  local ref="$1"
  echo "${ref#*/}"
}

# --- config.yml ---------------------------------------------------------
# A full config file, not env vars, for every setting: distribution's env-var
# flattening is forgiving for a flat key (REGISTRY_PROXY_REMOTEURL) but not
# for a key nested three deep (storage.maintenance.readonly.enabled), which
# panics ("readonly config key must contain additional keys") rather than
# doing what REGISTRY_STORAGE_MAINTENANCE_READONLY_ENABLED=true looks like it
# should. A generated file sidesteps relying on that flattening at all.
_write_config() {
  local out="$1" mode="$2" host="$3" bucket="$4" region="$5" bind_addr="$6" port="$7" network="$8"
  local root driver driver_yaml redirect_yaml addr
  root="$(cmd_root "$host")"

  driver="${SREGYM_REGISTRY_STORAGE_DRIVER:-s3}"
  redirect_yaml=""
  case "$driver" in
    s3)
      # Without disabling the redirect, registry:2's S3 driver answers a
      # blob GET/HEAD with a 302 to a presigned HTTPS S3 URL and the CALLER
      # (a kind node's containerd, or this script's own preflight) fetches
      # the bytes directly from S3, bypassing this container entirely. That
      # still never reaches Docker Hub, so it does not break the "never
      # touches Docker Hub" guarantee, but it does mean every kind node
      # needs its own network path to S3, a dependency this architecture
      # does not otherwise have: the registries run in host network mode
      # specifically so only the BOX needs AWS reachability (through the
      # instance role via IMDS), and a kind node only ever needs to reach
      # the box's own private IP (SREGYM_PRIVATE_IP). Disabling the
      # redirect keeps that true: every blob byte is proxied through this
      # process, which already holds the box's credentials, so a kind
      # node's only required network path stays "the box", not "the box,
      # plus S3". Costs more bytes through the registry process than the
      # redirect would; accepted, since the alternative is an undocumented
      # new egress path per kind node.
      #
      # distribution v2.8.3 reads "redirect" as a sibling of the storage
      # driver under "storage:", not a key inside the driver's own block
      # ("storage.s3.redirect" is silently ignored; "storage.redirect" is
      # what the code checks). Emitted as its own variable below so it
      # lands at the right nesting.
      driver_yaml="  s3:
    region: ${region}
    bucket: ${bucket}
    rootdirectory: ${root}"
      redirect_yaml="  redirect:
    disable: true"
      ;;
    filesystem)
      [ -n "${SREGYM_REGISTRY_DATA_DIR:-}" ] || die "SREGYM_REGISTRY_STORAGE_DRIVER=filesystem needs SREGYM_REGISTRY_DATA_DIR"
      driver_yaml="  filesystem:
    rootdirectory: ${SREGYM_REGISTRY_DATA_DIR}${root}"
      ;;
    *) die "unknown SREGYM_REGISTRY_STORAGE_DRIVER '$driver' (want s3 or filesystem)" ;;
  esac

  # "host" network: the container shares the box's own network namespace (no
  # "-p"), so the registry process itself has to bind bind_addr:port, the
  # same address docker would otherwise have published. "bridge": docker
  # still does that publishing, so the process inside the container keeps
  # binding its own fixed internal port, same as before this change.
  if [ "$network" = "host" ]; then
    addr="${bind_addr}:${port}"
  else
    addr=":5000"
  fi

  {
    echo "version: 0.1"
    echo "log:"
    echo "  fields:"
    echo "    service: registry"
    echo "storage:"
    echo "$driver_yaml"
    if [ -n "$redirect_yaml" ]; then
      echo "$redirect_yaml"
    fi
    if [ "$mode" = "plain" ]; then
      # One storage: key, not two. A second top-level "storage:" would just
      # overwrite the driver block above, dropping it silently. Pulls only
      # ever GET, so this maintenance flag is a backstop against a client
      # that tries to push anyway, not the primary enforcement of "no Docker
      # Hub fallback": a plain registry has no proxy.remoteurl and never did,
      # so a missing image 404s instead of downloading.
      echo "  maintenance:"
      echo "    readonly:"
      echo "      enabled: true"
    fi
    # "store" mode (seed_image_cache.sh's crane-copy target) gets no
    # maintenance block at all, i.e. writable: that is the one registry in
    # this script meant to accept a push.
    echo "http:"
    echo "  addr: $addr"
  } >"$out"
}

# --- start/stop ----------------------------------------------------------

# /v2/_catalog, not /v2/: the base route answers 200 before the storage
# driver is ever consulted, so a registry that cannot reach S3 (wrong
# credentials, wrong bucket, no network path to the metadata endpoint) still
# looked "ready" under that check, and the first real failure then showed up
# as an opaque pull error deep inside a benchmark run or a seed pass. The
# catalog listing walks the configured storage backend to enumerate
# repositories, so a credentials or connectivity problem fails here, at
# startup, with the registry's own log available to the caller.
_wait_ready() {
  local port="$1" deadline
  deadline=$(($(date +%s) + 30))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    curl -sf -o /dev/null "http://127.0.0.1:${port}/v2/_catalog" && return 0
    sleep 1
  done
  return 1
}

_start() {
  local mode="$1" host="$2" bucket="$3" region="$4" bind_addr="${5:-0.0.0.0}" network="${6:-bridge}"
  _known_host "$host" || die "unknown upstream host '$host' (see: $0 hosts)"
  case "$network" in
    bridge | host) ;;
    *) die "unknown network mode '$network' (want bridge or host)" ;;
  esac
  local port name
  port="$(cmd_port "$host")"
  name="sregym-registry-$(cmd_slug "$host")"
  # Not "local": a trap's command string runs in the script's top-level
  # scope, which no longer sees a function-local variable once the function
  # that declared it has returned, including when the trap fires right after
  # dispatch falls off the end of the script following a normal return from
  # this function (an unbound-variable error under "set -u", not a no-op).
  START_CONFIG_FILE="$(mktemp -t "sregym-registry-$(cmd_slug "$host").XXXXXX")"
  # Removed on exit, success or die alike: docker already has its own bind
  # mount of this inode once "docker run" below returns, so deleting the host
  # path afterward does not affect the running container, and this stops a
  # generated config (which is not secret, but also not meant to linger) from
  # piling up in the temp directory across many starts.
  trap 'rm -f "$START_CONFIG_FILE"' EXIT
  _write_config "$START_CONFIG_FILE" "$mode" "$host" "$bucket" "$region" "$bind_addr" "$port" "$network"

  docker rm -f "$name" >/dev/null 2>&1 || true
  log "starting $name ($mode, network=$network) on ${bind_addr}:${port}, backed by s3://${bucket}$(cmd_root "$host") (region ${region})"

  # The filesystem driver's rootdirectory is a path inside the container; it
  # needs a bind mount of its own or every byte lands in the container's own
  # throwaway layer and never reaches the host (only the smoke test and local
  # dev use this driver. s3 needs no such mount, since it talks over the
  # network instead of to a local path).
  local fs_mount=()
  if [ "${SREGYM_REGISTRY_STORAGE_DRIVER:-s3}" = "filesystem" ]; then
    fs_mount=(-v "${SREGYM_REGISTRY_DATA_DIR}:${SREGYM_REGISTRY_DATA_DIR}")
  fi

  # AWS credentials for the s3 driver. On a benchmark box, none of this is
  # needed: no AWS_* variables are set, and the registry's own S3 client
  # falls back to the instance role via the EC2 metadata endpoint. That
  # fallback only works because "host" network mode (below) puts the
  # container on the box's own network namespace: the box's IMDS hop limit
  # is 1 (terraform/compute.tf), and a container published the ordinary way,
  # with "-p" on the docker bridge, is one extra network hop away and gets no
  # token at all. A bridge-networked registry silently has no credentials,
  # answers its base route fine (nothing about that route touches S3), and
  # only fails once an actual pull needs the storage backend.
  #
  # On a developer machine seeding the bucket (seed_image_cache.sh, "store"
  # and "plain" mode both run over the ordinary bridge network there, never
  # "host"), that IMDS fallback does not apply at all and would not reach an
  # SSO profile's credentials either way: those live in ~/.aws/sso/cache, and
  # the Go SDK this image's S3 driver is built on does not resolve them the
  # way the AWS CLI does. Resolved to temporary static keys with `aws
  # configure export-credentials`, which is exactly what the CLI itself
  # already does internally for an SSO profile, and passed through as plain
  # environment variables, which every AWS SDK supports. Only attempted when
  # AWS_PROFILE is set and static credentials are not already in the
  # environment. A benchmark box sets neither.
  #
  # "-e NAME", never "-e NAME=VALUE": the latter puts the credential value in
  # this process's own argv, which "ps"/"docker inspect" of the *parent*
  # process (and anything else reading /proc or the process table) can read
  # in plain text for as long as the docker CLI invocation is running. "-e
  # NAME" instead reads the value from this script's own exported
  # environment, which docker then copies into the container's env the same
  # way either form would. The container's own env is not hidden either
  # way, but this closes the ps/argv leak, which is the cheaper and more
  # exposed of the two.
  local aws_env=()
  if [ "${SREGYM_REGISTRY_STORAGE_DRIVER:-s3}" = "s3" ]; then
    if [ -z "${AWS_ACCESS_KEY_ID:-}" ] && [ -n "${AWS_PROFILE:-}" ] && command -v aws >/dev/null 2>&1; then
      local exported var_name
      exported="$(aws configure export-credentials --profile "$AWS_PROFILE" --format env-no-export 2>/dev/null)" \
        || die "could not resolve credentials for AWS_PROFILE=$AWS_PROFILE via 'aws configure export-credentials'"
      while IFS= read -r line; do
        [ -n "$line" ] || continue
        var_name="${line%%=*}"
        export "$var_name=${line#*=}"
        aws_env+=(-e "$var_name")
      done <<EOF
$exported
EOF
    else
      for var in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN; do
        [ -n "${!var:-}" ] && aws_env+=(-e "$var")
      done
    fi
  fi

  # "host", only ever passed by run_shard.sh (the benchmark box): no "-p", so
  # the registry has to bind the real address itself (see _write_config's
  # "addr" above). "bridge" (the default, used by seed_image_cache.sh and any
  # local smoke test) keeps publishing with "-p", unchanged from before this
  # change, since none of those callers need IMDS.
  local net_args=()
  if [ "$network" = "host" ]; then
    net_args=(--network host)
  else
    net_args=(-p "${bind_addr}:${port}:5000")
  fi

  # "${arr[@]+"${arr[@]}"}", not "${arr[@]}": bash 3.2 (macOS's default, and
  # this script runs there as well as on the box) treats expanding a
  # zero-element array under `set -u` as an unbound-variable error, which
  # both arrays legitimately are whenever their branch above added nothing.
  docker run -d --name "$name" \
    "${net_args[@]}" \
    "${aws_env[@]+"${aws_env[@]}"}" \
    -v "${START_CONFIG_FILE}:/etc/distribution/config.yml:ro" \
    "${fs_mount[@]+"${fs_mount[@]}"}" \
    registry:2 /etc/distribution/config.yml >/dev/null \
    || { docker rm -f "$name" >/dev/null 2>&1 || true; die "could not start $name"; }

  if ! _wait_ready "$port"; then
    log "$name did not answer its storage-backed catalog within 30s; last 20 log lines:"
    docker logs "$name" 2>&1 | tail -20 >&2
    docker rm -f "$name" >/dev/null 2>&1 || true
    die "$name did not answer on port $port within 30s (see the log above; an S3 credentials or permission problem reads differently there than a plain startup timeout)"
  fi
  log "$name is up"
}

cmd_start_store() { _start store "$1" "$2" "$3" "${4:-0.0.0.0}" bridge; }
cmd_start_plain() { _start plain "$@"; }

cmd_stop() {
  local host="$1"
  _known_host "$host" || die "unknown upstream host '$host' (see: $0 hosts)"
  docker rm -f "sregym-registry-$(cmd_slug "$host")" >/dev/null 2>&1 || true
}

# --- dispatch ----------------------------------------------------------

cmd="${1:-}"
[ -n "$cmd" ] && shift || true
case "$cmd" in
  hosts) cmd_hosts "$@" ;;
  port) cmd_port "$@" ;;
  root) cmd_root "$@" ;;
  slug) cmd_slug "$@" ;;
  upstream-url) cmd_upstream_url "$@" ;;
  host-for) cmd_host_for "$@" ;;
  path-for) cmd_path_for "$@" ;;
  start-store) cmd_start_store "$@" ;;
  start-plain) cmd_start_plain "$@" ;;
  stop) cmd_stop "$@" ;;
  *) die "usage: $0 {hosts|port|root|slug|upstream-url|host-for|path-for|start-store|start-plain|stop} ..." ;;
esac
