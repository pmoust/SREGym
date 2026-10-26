#!/usr/bin/env bash
# Record what produced a run's numbers, as one object beside its logs.
#
# Without this a finished run cannot state its own configuration.
# setup_brain_go.sh logs brain_env key names only, deliberately, because that
# log ships to S3 and the mechanism is generic enough to carry a secret. The
# values then die with the instance, and analysis has to quote brain-go's
# compiled defaults as though they were the run's values.
#
# That is what happened to run-20260831-1752: its MaxHypotheses,
# ParallelBatchSize and Temperature are unrecoverable. Temperature is the worst
# of the three to lose, being the largest driver of the run-to-run variance
# that run existed to measure.
#
# Values, not just keys, because this is a different surface from the log: the
# bucket is private and encrypted, and brain_env's own documentation already
# tells operators not to put secrets in it, since the values reach terraform
# state, plan output and user_data regardless.
#
# Usage: write_run_manifest.sh <output-path>
#        write_run_manifest.sh --stages <output-path>
#        write_run_manifest.sh --host-images <output-path>
#
# Reads its content from the environment, so the caller decides what is known.
# --stages and --host-images each rewrite one field of a manifest this script
# already wrote, for a value that is not knowable when the rest is: the
# bootstrap writes the manifest and only then sources run_shard.sh, which is
# where the tasklist gets generated (--stages) and where registry:3 and the
# kind node image get appended to the host-image TSV (--host-images). Same
# derivation either way, called from the place that produced the new bytes,
# so a refresh cannot read its source two different ways from the first
# write.

set -uo pipefail

STAGES_ONLY=0
HOST_IMAGES_ONLY=0
case "${1:-}" in
  --stages) STAGES_ONLY=1; shift ;;
  --host-images) HOST_IMAGES_ONLY=1; shift ;;
esac

OUT="${1:?usage: write_run_manifest.sh [--stages|--host-images] <output-path>}"

# Which stages ran, derived rather than asserted. sregym/conductor/tasklist.yml
# selects stages per problem (diagnosis, mitigation, in that order); with no
# such file the conductor runs both. The file is gitignored, so a git clone
# never brings one: scripts/benchmark/write_tasklist.py generates it at shard
# start, and the smoke-test workflow writes a mitigation-only one of its own.
# Anything left on a box would silently change what a run measures, and a run
# that cannot say which stages it graded cannot be compared against one that
# graded more.
#
# A stage still needs its oracle attached: a problem with no mitigation_oracle
# is skipped with "Mitigation oracle is not attached", which cannot be seen
# from here. All 21 sregym-lite problems have one, checked against the
# archived results rather than by reading the registry. For the rest of the
# registry only a static read backs it: every registered class assigns a
# diagnosis_oracle, a mitigation_oracle and a root_cause (the per-problem
# ground truth the judge compares against) in its own or a base __init__.
emit_stages() {
  local tasklist="${SREGYM_TASKLIST:-${WORKDIR:-.}/sregym/conductor/tasklist.yml}"
  if [ -f "$tasklist" ]; then
    echo "stages_source=tasklist.yml"
    # Comments stripped first. The generated file carries a header saying where
    # it came from, and a header that happened to name a stage would otherwise
    # be read as that stage having run.
    echo "stages=$(grep -v '^[[:space:]]*#' "$tasklist" | grep -oE '(diagnosis|mitigation)' | sort -u | paste -sd, -)"
    echo "tasklist_sha256=$(shasum -a 256 "$tasklist" 2>/dev/null | cut -d" " -f1)"
  else
    echo "stages_source=conductor-default-no-tasklist"
    echo "stages=diagnosis,mitigation"
  fi
}

if [ "$STAGES_ONLY" = "1" ]; then
  if [ ! -f "$OUT" ]; then
    echo "write_run_manifest.sh --stages: $OUT does not exist" >&2
    exit 1
  fi
  # Written to a sibling and moved, so a failure halfway through leaves the
  # previous manifest intact rather than a truncated one. The refreshed lines
  # land at the end of the file; nothing reads the manifest positionally.
  TMP="$OUT.stages.$$"
  {
    grep -v -E '^(stages|stages_source|tasklist_sha256)=' "$OUT"
    emit_stages
  } >"$TMP" && mv "$TMP" "$OUT"
  exit $?
fi

# "<name>:<ref>@<id>" per image, comma-joined, not a second JSON file: every
# other per-image field in this manifest already uses that shape (see
# seeded_connection_buckets below). Reads SREGYM_HOST_IMAGE_MANIFEST fresh
# each call rather than caching a value, because the two callers (the first
# write below, and the --host-images refresh) read it at two different
# points in the TSV's life: setup_brain_go.sh has written brain-go's own
# rows by the first call, and run_shard.sh has appended registry:3 and the
# kind node image by the second.
# A brain_env name whose underscore-separated words include one of these
# holds a credential (TYPESAFE_API_KEY, GITHUB_TOKEN, DB_PASSWORD). The
# manifest is uploaded to the results bucket next to every run's results, so
# its value must never be written here.
SECRET_NAME_WORDS=" KEY APIKEY TOKEN SECRET PASSWORD PASSWD PASS CREDENTIAL CREDENTIALS PRIVATE "

_is_secret_name() {
  local word upper
  upper=$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')
  for word in ${upper//_/ }; do
    case "$SECRET_NAME_WORDS" in *" $word "*) return 0 ;; esac
  done
  return 1
}

_sha256_hex() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | cut -d' ' -f1
  else
    shasum -a 256 | cut -d' ' -f1
  fi
}

# BRAIN_ENV_EXTRA, one KEY=VALUE per line, with every secret's value replaced
# by "<redacted len=N sha256:XXXXXXXXXXXX>". The length and a 12-hex digest
# still let two runs be compared ("same key or not") without the value. The
# value reaches the digest through a pipe from printf, a builtin, so it never
# appears in a process's argv.
emit_brain_env() {
  local line name value
  while IFS= read -r line; do
    name=${line%%=*}
    if [ "$name" != "$line" ] && _is_secret_name "$name"; then
      value=${line#*=}
      printf '%s=<redacted len=%d sha256:%s>\n' "$name" "${#value}" \
        "$(printf '%s' "$value" | _sha256_hex | cut -c1-12)"
    else
      printf '%s\n' "$line"
    fi
  done <<<"${BRAIN_ENV_EXTRA:-}"
}

emit_host_images() {
  local manifest="${SREGYM_HOST_IMAGE_MANIFEST:-/tmp/sregym-brain-go-host-images.tsv}"
  local images=""
  if [ -f "$manifest" ]; then
    images=$(awk -F'\t' '{printf "%s%s:%s@%s", (NR>1?",":""), $1, $2, $3}' "$manifest")
  fi
  echo "brain_go_host_images=$images"
}

if [ "$HOST_IMAGES_ONLY" = "1" ]; then
  if [ ! -f "$OUT" ]; then
    echo "write_run_manifest.sh --host-images: $OUT does not exist" >&2
    exit 1
  fi
  # Same written-to-a-sibling-and-moved shape as --stages above, for the same
  # reason: a failure halfway through must not leave a truncated manifest.
  TMP="$OUT.host-images.$$"
  {
    grep -v -E '^brain_go_host_images=' "$OUT"
    emit_host_images
  } >"$TMP" && mv "$TMP" "$OUT"
  exit $?
fi

# The fully resolved problem id list, not PROBLEM_SET or PROBLEMS_OVERRIDE
# verbatim: a run's provenance should not depend on whether the selection
# was one named set, a mix of sets and bare ids, or a narrowed override, only
# on which ids actually ran. Resolved with the same resolve_problems() that
# run_shard.sh uses, so the two cannot read the same PROBLEM_SET two
# different ways.
#
# This script runs before run_shard.sh (see bootstrap.sh.tftpl) and its own
# caller already tolerates its failure with `|| true`, so a bad PROBLEM_SET
# here must not blank out every other field: only this line goes empty, and
# run_shard.sh is still what hard-stops the run over it.
PROBLEMS_RESOLVED=$(PROBLEM_SET="${PROBLEM_SET:-}" PROBLEMS_OVERRIDE="${PROBLEMS_OVERRIDE:-}" uv run python -c "
import os, sys
from sregym.conductor.problem_sets import resolve_problems

try:
    ids = resolve_problems(os.environ['PROBLEM_SET'])
except ValueError as exc:
    sys.stderr.write(str(exc) + chr(10))
    sys.exit(1)

want = os.environ.get('PROBLEMS_OVERRIDE', '').split()
if want:
    ids = [p for p in ids if p in set(want)]

print(' '.join(ids))
" 2>/dev/null) || PROBLEMS_RESOLVED=""

# What each seeded connection was tagged with, read straight from the table
# the seeding script actually uses rather than copied here, so this cannot
# drift from what got seeded. Which table depends on the arm:
# seed_connections.py's ENDPOINTS under mcp, seed_native_connections.py's
# NATIVE_CONNECTIONS under native. This is the harness's own side of the toolset
# a run had: which MCP endpoint became which CUSTOM_MCP connection, and which
# investigation bucket(s) it was registered under.
#
# It is not the resolved per-node toolset. Which tools a given RCI node (state,
# metrics, logs, traces) actually binds on a turn depends on brain-go's own
# logic -- nodeBucket and BuildCustomTools's bucket filter in
# internal/chat/tools/registry.go, and DefaultBucketForKind in
# internal/core/domain/connection.go -- which this script has no view into and
# must not guess at: a copy here would silently drift the first time that logic
# changes. Recovering the per-node toolset for a specific run still means
# reading those three functions at the run's brain_go_sha and checking
# tool-calls.json, the way this field's own reasoning was worked out.
SEEDED_CONNECTION_BUCKETS=$(uv run python -c "
from scripts.benchmark.arm import current_arm, runs_edge_agent
from scripts.benchmark.seed_connections import ENDPOINTS

if runs_edge_agent():
    from scripts.benchmark.seed_native_connections import BUCKET, connections_for_arm

    print(','.join(f'{service}:{\"+\".join(BUCKET)}' for _, _, service in connections_for_arm(current_arm())))
else:
    print(','.join(f'{path}:{\"+\".join(buckets)}' for _, path, buckets, _ in ENDPOINTS))
" 2>/dev/null) || SEEDED_CONNECTION_BUCKETS=""

# The signal each seeded connection declared, same source and same caveat as
# the buckets above. A connection with no entry is written as "-": it reaches
# every node in its bucket, which is what every run before signals existed
# had. Every native connection is written that way, and not because the value
# was lost: `signals` scopes CUSTOM_MCP and CUSTOM_TOOL tools only, and a
# native family is already partitioned by service. Recorded because it is the
# difference between a run whose metrics, logs and traces agents held one
# server each and a run where they held the same three.
SEEDED_CONNECTION_SIGNALS=$(uv run python -c "
from scripts.benchmark.arm import current_arm, runs_edge_agent
from scripts.benchmark.seed_connections import ENDPOINTS, SIGNALS

if runs_edge_agent():
    from scripts.benchmark.seed_native_connections import connections_for_arm

    print(','.join(f'{service}:-' for _, _, service in connections_for_arm(current_arm())))
else:
    print(','.join(f'{path}:{\"+\".join(SIGNALS.get(path) or [\"-\"])}' for _, path, _, _ in ENDPOINTS))
" 2>/dev/null) || SEEDED_CONNECTION_SIGNALS=""

# The image the MCP pod is meant to run. Asked of the script that builds it
# rather than restated here, so the manifest cannot name a tag nothing was
# built under. This runs before run_shard.sh builds anything, so it records
# the intent; what each pod actually started is recorded per slot in
# mcp_pod_image.json.
#
# Worth recording at all because it was unrecorded and wrong: every run before
# this one ran ghcr.io/sregym/sregym-mcp:latest, upstream's 2026-09-06 build,
# and no artifact said so.
MCP_IMAGE=$("$(dirname "$0")/build_mcp_image.sh" --print-name 2>/dev/null) || MCP_IMAGE=""

# Which arm produced these numbers. Recorded before anything else about the
# seeding, because every other line describing a connection means something
# different depending on it. Empty means SREGYM_ARM named an arm that does
# not exist, which run_shard.sh hard-stops the run over.
ARM=$(uv run python "$(dirname "$0")/arm.py" 2>/dev/null) || ARM=""

# Which kubernetes connection, if any, gives brain-go cluster access: "mcp"
# for the CUSTOM_MCP sregym-kubectl endpoint, "native" for brain-go's own
# Kubernetes connector, or "none" for the edge arm, which has neither -- the
# edge agent's entity graph is the only cluster access brain-go gets. Empty
# when ARM is empty, the same way every other arm-dependent field is.
KUBERNETES_CONNECTION=""
case "$ARM" in
  mcp) KUBERNETES_CONNECTION="mcp" ;;
  native) KUBERNETES_CONNECTION="native" ;;
  edge) KUBERNETES_CONNECTION="none" ;;
esac

# The edge agent behind the entity graph, asked of the script that installs it
# rather than restated here. Empty under the MCP arm, which runs no edge
# agent: the keys are always present so a reader parses one shape. The native
# and edge arms both run it the same way, so both get it recorded.
#
# Recorded because the graph is these arms' whole claim, and two runs whose
# graphs came from different agent builds are not the same run.
EDGE_IMAGE=""
EDGE_CHART_VERSION=""
if [ "$ARM" = "native" ] || [ "$ARM" = "edge" ]; then
  EDGE_VERSIONS=$("$(dirname "$0")/native_arm.sh" edge-versions 2>/dev/null) || EDGE_VERSIONS=""
  EDGE_IMAGE=$(printf '%s' "$EDGE_VERSIONS" | cut -f1)
  EDGE_CHART_VERSION=$(printf '%s' "$EDGE_VERSIONS" | cut -f2)
fi

# The kubernetes connection's configmap_values setting as actually seeded
# (brain-go#4052), native arm only: the MCP arm seeds no native kubernetes
# connection and the edge arm seeds no kubernetes connection at all, so the
# key means nothing to either. Read from resolve_configmap_values() rather
# than restated here, so this cannot drift from what seed_native_connections.py
# actually sent if the override or the default ever change. Empty outside the
# native arm, the same way edge_image is, so a reader parses one shape
# regardless of arm.
SEEDED_CONFIGMAP_VALUES=""
if [ "$ARM" = "native" ]; then
  SEEDED_CONFIGMAP_VALUES=$(uv run python -c "
from scripts.benchmark.seed_native_connections import resolve_configmap_values

print(f'kubernetes:{resolve_configmap_values()}')
" 2>/dev/null) || SEEDED_CONFIGMAP_VALUES=""
fi

# The flag that actually gates brain-go's k8s_* tool family, read back from
# the box's own deploy/.env rather than re-derived from SREGYM_ARM, so this
# cannot drift from what setup_brain_go.sh actually wrote after BRAIN_ENV_EXTRA
# was applied: BRAIN_ENV_EXTRA can set this key on any arm, and a re-derived
# value would silently disagree with a box where it did. Defaults to "false",
# brain-go's own default when the key is absent, so a reader always gets
# true or false rather than sometimes an empty string.
BRAIN_GO_DIR="${BRAIN_GO_DIR:-/opt/brain-go}"
KUBERNETES_TOOLS_ENABLED="false"
if [ -f "$BRAIN_GO_DIR/deploy/.env" ]; then
  effective_k8s_tools=$(grep '^FEATURE_KUBERNETES_TOOLS_ENABLED=' "$BRAIN_GO_DIR/deploy/.env" | tail -1 | cut -d= -f2-)
  [ -n "$effective_k8s_tools" ] && KUBERNETES_TOOLS_ENABLED="$effective_k8s_tools"
fi

# The LLM_EFFORT brain-go actually starts with, read back the same way:
# setup_brain_go.sh writes it from REASONING_EFFORT and then applies
# BRAIN_ENV_EXTRA, whose set_env_key deletes-then-appends, so a brain_env
# entry for LLM_EFFORT wins over REASONING_EFFORT silently. Re-deriving from
# REASONING_EFFORT alone would disagree with a box where it did. Empty, not
# "medium", when the key is absent: that still means the run left it unset
# and brain-go answered at its own default, not at a value this script can
# name.
EFFECTIVE_REASONING_EFFORT=""
if [ -f "$BRAIN_GO_DIR/deploy/.env" ]; then
  effective_reasoning_effort=$(grep '^LLM_EFFORT=' "$BRAIN_GO_DIR/deploy/.env" | tail -1 | cut -d= -f2-)
  [ -n "$effective_reasoning_effort" ] && EFFECTIVE_REASONING_EFFORT="$effective_reasoning_effort"
fi

{
  echo "run_id=${RUN_ID:-}"
  echo "shard_index=${SHARD_INDEX:-}"
  echo "shard_count=${SHARD_COUNT:-}"
  echo "problem_set=${PROBLEM_SET:-}"
  # The resolved list, independent of how selection was expressed. Empty
  # here means resolution failed (see above), not that the whole registry
  # ran: an unresolvable PROBLEM_SET makes run_shard.sh terminate the run
  # with nothing scored.
  echo "problems=$PROBLEMS_RESOLVED"
  echo "n_attempts=${N_ATTEMPTS:-}"
  echo "noise=${NOISE:-}"
  echo "agent_timeout_seconds=${AGENT_TIMEOUT_SECONDS:-}"
  echo "runs_per_instance=${RUNS_PER_INSTANCE:-}"
  # The instance family, because slot count alone does not describe the box
  # the slots ran on. run-20260902-0606 recorded runs_per_instance=3 and
  # nothing else, so recovering that it ran on r7a.24xlarge took a CloudTrail
  # lookup of RunInstances. Two runs at the same slot count on different
  # families are not the same run.
  echo "instance_type=${INSTANCE_TYPE:-}"
  # Where the results went. One bucket now holds every run under its own
  # <run-id>/ prefix (#48), so the bucket name is no longer recoverable from
  # the run id and an archived run should say where it came from.
  echo "results_bucket=${RESULTS_BUCKET:-}"
  echo "agent_model=${AGENT_MODEL:-}"
  echo "judge_model=${JUDGE_MODEL:-}"
  # Read back from the box's own deploy/.env. See the comment above
  # EFFECTIVE_REASONING_EFFORT for why REASONING_EFFORT alone is not enough.
  echo "reasoning_effort=$EFFECTIVE_REASONING_EFFORT"
  echo "sregym_ref=${SREGYM_REF:-}"
  echo "sregym_sha=${SREGYM_SHA:-}"
  echo "brain_go_ref=${BRAIN_GO_REF:-}"
  echo "brain_go_sha=${BRAIN_GO_SHA:-}"
  # mcp, native, or edge. Every line below that describes a connection means
  # something different depending on it.
  echo "arm=$ARM"
  # mcp, native, or none. See the comment above KUBERNETES_CONNECTION.
  echo "kubernetes_connection=$KUBERNETES_CONNECTION"
  # true or false, read back from the box's deploy/.env. See the comment
  # above KUBERNETES_TOOLS_ENABLED.
  echo "kubernetes_tools_enabled=$KUBERNETES_TOOLS_ENABLED"
  # sregym-mcp:<sregym sha>, built on the box and loaded into each slot's kind
  # node. See the comment above for what it does and does not promise.
  echo "mcp_image=$MCP_IMAGE"
  # nofireai/edge:<tag> and the nofire-edge chart version, both empty under
  # the MCP arm. See the comment above for why they are recorded at all.
  echo "edge_image=$EDGE_IMAGE"
  echo "edge_chart_version=$EDGE_CHART_VERSION"
  # kind:bucket[+bucket](,kind:bucket...) for each CUSTOM_MCP connection this
  # run seeded. See the comment above SEEDED_CONNECTION_BUCKETS for what this
  # does and does not tell a reader about a node's actual bound toolset.
  echo "seeded_connection_buckets=$SEEDED_CONNECTION_BUCKETS"
  echo "seeded_connection_signals=$SEEDED_CONNECTION_SIGNALS"
  # kind:value for the kubernetes connection's configmap_values setting,
  # native arm only. See the comment above SEEDED_CONFIGMAP_VALUES.
  echo "seeded_configmap_values=$SEEDED_CONFIGMAP_VALUES"
  # Which bytes this run's host images actually were, read back from
  # SREGYM_HOST_IMAGE_MANIFEST: setup_brain_go.sh's own gate has written
  # brain-go's own rows there by this point, but run_shard.sh has not yet
  # appended registry:3 and the kind node image, since the bootstrap calls
  # this script before it sources run_shard.sh. Empty when
  # REGISTRY_CACHE_BUCKET was unset (a developer machine) or
  # setup_brain_go.sh has not run yet. See emit_host_images above for the
  # "<name>:<ref>@<id>" shape and the --host-images refresh that adds the
  # pair this first write cannot see.
  emit_host_images

  # Which stages ran. See emit_stages above; run_shard.sh calls this script a
  # second time with --stages once it has generated the tasklist, because at
  # this point in the bootstrap the file does not exist yet.
  emit_stages
  # One KEY=VALUE per line, as written into brain-go's deploy/.env, except
  # that a secret's value is replaced by its length and a short digest. See
  # emit_brain_env above.
  emit_brain_env
} >"$OUT"
