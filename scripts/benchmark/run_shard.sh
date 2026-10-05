#!/usr/bin/env bash
# The shard: resume, problem division, and the slot workers that run them.
#
# Sourced by terraform/templates/bootstrap.sh.tftpl, not executed. It uses that
# file's terminate, hard_stop, sync_results and sync_logs, and relies on the
# traps and watchdogs armed there, none of which survive into a child process.
#
# It lives here because user_data is capped at 16384 bytes and the template had
# run out of room. Anything added to the bootstrap should land here instead.
# scripts/benchmark/measure_user_data.py reports the remaining margin.
#
# Inherited from the bootstrap: WORKDIR, SLOTS_DIR, S3_PREFIX, REGION, RUN_ID,
# SHARD_INDEX, SHARD_COUNT, MANIFEST. Passed as exported shell variables:
# PROBLEM_SET, N_ATTEMPTS, AGENT_TIMEOUT_SECONDS, RUNS_PER_INSTANCE,
# AGENT_MODEL, JUDGE_MODEL, SREGYM_REF, NOISE.
#
# No `set -e` here: the bootstrap sets its own options and a sourced file
# changing them would apply to the caller after this returns.

# --- resume -----------------------------------------------------------------
# Pull whatever this shard already finished, for every slot at once: slot
# assignment below is deterministic (same sorted problem list, same fixed
# runs_per_instance), so a replacement instance redivides problems into the
# same slots as before it died -- one sync call up front is simpler than N,
# and S3 sync only transfers what changed either way. main.py --resume counts
# attempts per problem_id and skips any problem that already has n_attempts
# rows, so a replacement instance costs the in-flight problem and nothing more.
mkdir -p "$SLOTS_DIR"
aws s3 sync "$S3_PREFIX/slots/" "$SLOTS_DIR" --region "$REGION" --only-show-errors || true

# --- shard ------------------------------------------------------------------
# Static sharding: problem index % shard_count. No queue and no coordinator,
# which is the right trade for 90 to 540 runs where the only failure mode that
# matters is instance death, and S3 resume already covers that.
# problem_sets.py is pure data. The registry is not: ProblemRegistry() loads a
# kubeconfig on construction, and at this point the cluster may not exist yet.
# The full-suite path is blocked on NOFireAI/brain-go#2221 anyway, which has to
# reconstruct which 90 of the 123 registered problems the paper used.
if [ -z "$PROBLEM_SET" ]; then
  echo "[bootstrap] problem_set is empty, which means the full registry."
  echo "[bootstrap] That set is undefined: 123 problems are registered, the paper"
  echo "[bootstrap] used 90, and nothing in the repo says which. See NOFireAI/brain-go#2221."
  terminate "full-suite runs need a defined problem set"
  exit 1
fi

# PROBLEM_SET is resolved by problem_sets.py's resolve_problems(): a
# comma-separated mix of named sets and bare problem ids, unioned and
# deduped, hard-stopping on any token that is neither. PROBLEMS_OVERRIDE then
# narrows that resolved set further, which is how a smoke run costs ten
# minutes instead of a full suite. Filtered through the resolved set rather
# than trusted: a typo would otherwise shard to nothing and terminate looking
# like a clean short run, which is the failure mode this whole file keeps
# hitting. An id not in the resolved set is a hard stop, listing what was
# rejected.
# Every value reaches python through the environment, none through string
# interpolation. $PROBLEM_SET used to be spliced into the source as
# PROBLEM_SETS['$PROBLEM_SET'], where a value containing a quote breaks out of
# the literal into arbitrary python. terraform now also charset-validates the
# variable, but the two guards are independent on purpose: this one holds for
# any caller, including a hand-run invocation that never went through
# terraform.
PROBLEMS=$(PROBLEMS_OVERRIDE="${PROBLEMS_OVERRIDE:-}" PROBLEM_SET="$PROBLEM_SET" uv run python -c "
import os, sys
from sregym.conductor.problem_sets import resolve_problems

try:
    ids = resolve_problems(os.environ['PROBLEM_SET'])
except ValueError as exc:
    sys.stderr.write(str(exc) + chr(10))
    sys.exit(2)

want = os.environ.get('PROBLEMS_OVERRIDE', '').split()
if want:
    unknown = [p for p in want if p not in ids]
    if unknown:
        sys.stderr.write('unknown problem ids: ' + ', '.join(unknown) + chr(10))
        sys.exit(2)
    ids = [p for p in ids if p in set(want)]

print(' '.join(p for i, p in enumerate(ids) if i % $SHARD_COUNT == $SHARD_INDEX))
") || {
  terminate "problems override rejected, see the error above"
  exit 1
}

if [ -z "$PROBLEMS" ]; then
  terminate "shard $SHARD_INDEX resolved to no problems"
  exit 0
fi
echo "[bootstrap] shard $SHARD_INDEX owns $(echo $PROBLEMS | wc -w) problems"

# --- stages -----------------------------------------------------------------
# Diagnosis only, unless SREGYM_STAGES says otherwise. brain-go does not do
# mitigation as a product and no analysis reads the mitigation columns, so the
# stage was buying nothing: the archived mitigation-only histories account for
# 21.4M of run-20260921-1250's 60.15M tokens.
#
# It also cost the record of the turn we do measure. brain-go names every RCI
# child workflow rci-<investigation uuid> and both turns of an attempt run on
# one investigation, so the mitigation turn starts a second run under the same
# workflow id, and `temporal workflow show` without a run id returns the latest
# run. 119 of the 121 histories archived from that run are the mitigation turn.
#
# Generated from $PROBLEMS, not written by hand, because "not listed" is the
# failure mode: the conductor grades both stages for any problem the file
# forgets and only logs it. write_tasklist.py reads the file back and fails if
# its problem set is not exactly $PROBLEMS.
#
# SREGYM_STAGES is diagnosis or diagnosis,mitigation and nothing else. A typo
# would otherwise leave no file at all, which is the conductor's both-stage
# default again, at twice the cost and looking like a normal run.
TASKLIST_PATH="$WORKDIR/sregym/conductor/tasklist.yml"
if ! TASKLIST_PATH="$TASKLIST_PATH" PROBLEMS="$PROBLEMS" \
     SREGYM_STAGES="${SREGYM_STAGES:-diagnosis}" \
     uv run --project "$WORKDIR" python "$WORKDIR/scripts/benchmark/write_tasklist.py"; then
  terminate "tasklist generation rejected, see the error above"
  exit 1
fi

# The manifest was written before this file existed: the bootstrap calls
# write_run_manifest.sh and only then sources this one. Left alone it would
# record the conductor's both-stage default on every diagnosis-only run, and a
# run that misreports which stages it graded cannot be compared with anything.
# Refreshed from the same derivation that wrote the line, not a second copy of
# it. Best effort, like the original call: the manifest is provenance, and
# losing it must not end a run that is otherwise ready to go.
if [ -n "${MANIFEST:-}" ] && [ -f "${MANIFEST:-}" ]; then
  bash "$WORKDIR/scripts/benchmark/write_run_manifest.sh" --stages "$MANIFEST" || true
fi

# --- run --------------------------------------------------------------------
export AGENT_MODEL_ID="$AGENT_MODEL"
export JUDGE_MODEL_ID="$JUDGE_MODEL"
export AWS_DEFAULT_REGION="$REGION"

# Every SREGYM_* intervention stays off. They exist to work around a 24 GB ARM
# laptop and they are why engineering#799's numbers cannot be read.
# SREGYM_SETTLE_SECONDS is not among them here: this shard hardcodes
# `--agent brain_go` below, and clients/brain_go/driver.py never reads that
# variable (only clients/brain/driver.py, the Python driver, does).
unset SREGYM_WAIT_FAULT_EVENT SREGYM_WAIT_EDGE_SYNC SREGYM_STELLARIZE \
      SREGYM_STABILIZE_PODS SREGYM_GATE_SKIP SREGYM_BASELINE_SYNC_SECONDS \
      SREGYM_SKIP_JUDGE_PREFLIGHT

# --- arm --------------------------------------------------------------------
# Which arm this shard runs. Resolved by arm.py, which is the one place that
# knows the accepted values, so an unrecognised SREGYM_ARM ends the run here
# rather than silently producing the default arm under another arm's label.
#
# Exported, because it is read in four more places: setup_brain_go.sh (already
# run by the bootstrap above), seed_slot.sh, the conductor's pre-fault gate and
# main.py's per-attempt evidence.
SREGYM_ARM=$(uv run --project "$WORKDIR" python "$WORKDIR/scripts/benchmark/arm.py") || {
  terminate "SREGYM_ARM is not a recognised arm, see the error above"
  exit 1
}
export SREGYM_ARM
echo "[bootstrap] arm $SREGYM_ARM"

if [ "$SREGYM_ARM" = "native" ] || [ "$SREGYM_ARM" = "edge" ]; then
  # Both arms run the edge agent and need everything in this block: the
  # pre-fault window, the private address the agent and the telemetry
  # connections use, and the agent's own image and chart. Only the API
  # server's private-IP bind below is native-only, because that is what
  # brain-go's Kubernetes connection needs and edge has none.
  #
  # The pre-fault window. The conductor sleeps this long after deploying the
  # application and before injecting the fault, which is the hook
  # SREGYM_BASELINE_SYNC_SECONDS was added for and never used.
  #
  # 150s, which holds at least two of the edge agent's 60s publish ticks
  # (see native_arm.sh for the arithmetic; 60s is the agent's floor). The
  # window has to contain the agent publishing the just-deployed application
  # and brain-go ingesting it, and 150s absorbs one missed tick without being
  # a large share of an attempt.
  #
  # The number is not what makes this correct. The gate that runs at the end
  # of the wait fails the attempt when the graph is still empty, so a window
  # that turns out to be too short is loud rather than a run of attempts that
  # quietly measured nothing.
  export SREGYM_BASELINE_SYNC_SECONDS="${SREGYM_BASELINE_SYNC_SECONDS:-150}"
  echo "[bootstrap] pre-fault window ${SREGYM_BASELINE_SYNC_SECONDS}s"

  # The address the in-cluster edge agent reaches brain-go's api service on,
  # and the address the native telemetry connections are registered against.
  # Resolved through native_arm.sh so this, setup_brain_go.sh and the
  # validation all read one implementation, and exported so seed_slot.sh
  # takes the same answer.
  SREGYM_PRIVATE_IP=$("$WORKDIR/scripts/benchmark/native_arm.sh" private-ip) || {
    terminate "could not determine a private IPv4 address; the edge agent would have nowhere to publish"
    exit 1
  }
  export SREGYM_PRIVATE_IP
  EDGE_GRAPH_URL="http://${SREGYM_PRIVATE_IP}:8000/api/edge"
  echo "[bootstrap] edge graph endpoint $EDGE_GRAPH_URL"

  # Not pre-pulled and not loaded into the node. Each kind node pulls the
  # image itself, and the Helm release leaves the chart's own
  # imagePullPolicy: Always in place.
  #
  # Two attempts to pre-load it failed the same way. kind runs
  # `ctr images import --all-platforms` for both `kind load docker-image` and
  # `kind load image-archive`, and the edge image is a manifest list:
  # docker fetches one platform's blobs, `docker save` keeps the whole list,
  # and the import then asks the node for a digest nobody ever downloaded:
  #
  #   ctr: content digest sha256:62d20cf4... not found
  #
  # That aborted the slot in run-20260925-0933 and again, with the archive, in
  # run-20260928-0703. The MCP image does not hit it because it is built on
  # this box, so every blob it references is present.
  #
  # What the pre-pull was for was Docker Hub's anonymous rate limit: one pull
  # per box rather than one per slot. A full run is four boxes of three slots,
  # so twelve pulls of one public image, which is not near the limit. Paying
  # that is cheaper than carrying a platform-selection workaround for it.
  EDGE_IMAGE=$("$WORKDIR/scripts/benchmark/native_arm.sh" edge-versions | cut -f1)
  echo "[bootstrap] edge agent image $EDGE_IMAGE, pulled by each kind node"

  # The chart, once per box and for the same reason. Slots run concurrently in
  # background subshells, so a per-slot download to one shared path lets one
  # slot hand helm a file another is still writing, and the install fails
  # non-deterministically. Fetched here, before any slot starts, and passed
  # down by path.
  # GH_TOKEN passed on the command rather than exported. The bootstrap sets it
  # as a plain assignment, so a script it executes as a child does not inherit
  # it, and the chart repo is private. This is how the bootstrap already hands
  # the same token to setup_brain_go.sh, and it keeps the token off every other
  # child process in this shard.
  SREGYM_EDGE_CHART=$(GH_TOKEN="${GH_TOKEN:-}" "$WORKDIR/scripts/benchmark/native_arm.sh" fetch-chart) || {
    terminate "could not fetch the edge agent chart"
    exit 1
  }
  export SREGYM_EDGE_CHART
  echo "[bootstrap] edge agent chart $SREGYM_EDGE_CHART"
fi

# Sync after every problem rather than at the end, so an interruption costs one
# problem. The watcher is cheap and independent of the run's own exit path.
#
# Logs go up on the same tick, not only on exit. Syncing them only from
# terminate() means any death that skips it -- a spot reclaim, a kernel OOM,
# a hard kill -- takes the whole diagnostic trail with it. Run
# run-20260828-2122 lost three hours that way: all three slots stopped
# progressing right after "Deploying Loki", the box was reclaimed before
# terminate() ran, and the only surviving record was helm's cache directory.
# The per-slot logs, which had whatever error followed, died with the box.
( while true; do sleep 60; sync_results || true; sync_logs || true; done ) &

NOISE_FLAG=""
if [ "$NOISE" = "true" ]; then
  NOISE_FLAG="--noise"
fi

# --- MCP port-forward probe -------------------------------------------------
# Prints curl's status code for one request through the slot's port-forward.
# 000 is curl's code for "no HTTP response at all".
#
# Probed on /healthz, a plain route outside the /kubectl, /jaeger, /loki,
# /prometheus, /submit SSE mounts: curl against one of those holds the
# connection open for the whole --max-time even on a healthy, instantly
# answering endpoint, because the body is an endless stream and only
# --max-time ends it. See the healthz() docstring in
# mcp_server/sregym_mcp_server.py.
#
# 3 seconds is the budget for one probe. /healthz answers in milliseconds when
# the server's event loop is free, so anything past a few seconds means the
# loop is busy or the forward is gone, and the probe has to end well inside
# PF_PROBE_INTERVAL_SECONDS (15) so a cycle cannot overrun the next one.
MCP_PROBE_TIMEOUT_SECONDS=3

mcp_probe_code() {
  curl -s -o /dev/null -w '%{http_code}' --max-time "$MCP_PROBE_TIMEOUT_SECONDS" \
    "http://127.0.0.1:$MCP_SERVER_PORT/healthz" 2>/dev/null
}

# Whether the app answered at all. Any status counts, not specifically 200.
# A reply of any status proves the request crossed the forward and reached the
# app, which is the only thing either caller is entitled to conclude from it.
#
# The pod now has the route. It runs the image built from this checkout (see
# the MCP image section below), whose sregym_mcp_server.py serves /healthz and
# answers 200. Accepting any status is what this file needed while the pod ran
# ghcr.io/sregym/sregym-mcp:latest instead: that image was built 2026-09-06,
# /healthz entered this repo on 2026-09-11 in the same commit that pointed both
# probes at it (#94), and its five SSE mounts have no Mount("/") to absorb the
# path, so Starlette answered 404 and every one of the 12 slots in
# run-20260914-0912 aborted on it before running a problem.
#
# Kept as "any status" rather than tightened back to 200 now that 200 is what a
# healthy pod sends. Neither caller can act on the difference between 200 and
# 404 -- see the paragraph below -- and a slot that somehow deploys the old
# image should reach seed_slot.sh's strict-200 wait on /kubectl/sse and fail
# there, where the message names the endpoint the agent actually uses.
#
# 000 still fails, which is the failure #94 exists to catch: connection
# refused, nothing listening, or a socket that accepts and never answers
# within --max-time.
#
# The two callers do different things with a failure, so neither reads a
# status as application health. The supervisor restarts `kubectl
# port-forward`, which cannot change a status the app chose to send. The
# startup gate aborts the slot, and deliberately leaves judging the app to
# seed_slot.sh, which waits on /kubectl/sse -- the route the agent actually
# uses -- and still requires a strict 200 there.
mcp_probe_answered() {
  [ -n "$1" ] && [ "$1" != "000" ]
}

# Whether the forwarded local port can still carry a stream.
#
# Read from curl's exit status alone. Two exits mean the forward is fine:
# 0, the app replied with some status, and 28, the connect completed and
# then nothing came back inside the budget. 28 is the case this function
# exists for and it must read as alive: a server whose event loop is blocked
# accepts the handshake and says nothing, which is exactly what a dead
# forward looks like to the /healthz probe, and only differs here.
#
# Every other exit means the forward cannot carry a stream. 7 is "failed to
# connect": nothing is bound, `kubectl port-forward` is no longer listening.
# 52 (empty reply) and 56 (recv failure) are a listener that accepted and
# then closed or reset: that is what kubectl does when its process is still
# up but the pod behind it cannot take the stream, and every real call on
# that forward fails the same way. Treating those as alive would leave the
# slot on a forward nothing can recover.
#
# The path is "/" rather than /healthz on purpose: the reply is discarded
# either way, and routing is not what is being measured.
#
# 3 seconds, the same budget as MCP_PROBE_TIMEOUT_SECONDS, bounds both the
# handshake and the wait after it. A loopback connect either completes or is
# refused at once, so the budget only matters when the listen backlog is
# full, which is itself a forward that is carrying traffic.
PF_CONNECT_TIMEOUT_SECONDS=3

pf_port_accepts() {
  local rc
  curl -s -o /dev/null \
    --connect-timeout "$PF_CONNECT_TIMEOUT_SECONDS" \
    --max-time "$PF_CONNECT_TIMEOUT_SECONDS" \
    "http://127.0.0.1:$MCP_SERVER_PORT/" 2>/dev/null
  rc=$?
  [ "$rc" -eq 0 ] || [ "$rc" -eq 28 ]
}

# Whether the forward is worth keeping. Alive means both halves hold: the
# `kubectl port-forward` process is still there, and the port it bound still
# accepts a connection. Either half failing means the forward is dead and a
# restart is the only thing that can fix it.
#
# An empty pid counts as dead. The pid file is written before the supervisor
# starts and rewritten atomically on every restart, so an unreadable one
# means the supervisor has lost track of what it is supervising.
pf_forward_alive() {
  local pid="$1"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  pf_port_accepts
}

# --- MCP image --------------------------------------------------------------
# Build the MCP server image from this checkout, so the pod runs the code this
# repo has rather than upstream's published build. Until this ran, the
# kustomization pointed at ghcr.io/sregym/sregym-mcp:latest with
# imagePullPolicy: Always, an image built 2026-09-06: no /healthz route and no
# anyio.to_thread.run_sync offload, so a `kubectl rollout status
# --timeout=120s` held the single event loop and the supervisor below restarted
# a working forward instead. That accounts for all 43 restarts in
# run-20260921-1250.
#
# Once per box, here rather than in the bootstrap: the image is the same for
# every slot, docker's cache is per box, and this file exists because
# user_data is capped at 16384 bytes and anything addable to the bootstrap
# should land here instead. The per-slot half is the `kind load` in run_slot
# below, which has to wait for each slot's cluster to exist.
#
# A failed build is a hard stop. A slot that carried on would deploy the pod
# with imagePullPolicy: Never against an image no node holds, sit in
# ErrImageNeverPull, and abort at the port-forward gate with a message about
# the forward rather than about the image.
MCP_IMAGE=$("$WORKDIR/scripts/benchmark/build_mcp_image.sh") || {
  terminate "could not build the MCP server image"
  exit 1
}
# Read by MCPServer.deploy() (sregym/service/mcp_server.py), which generates a
# kustomize overlay naming this image with imagePullPolicy: Never. Exported
# here so every slot's deploy_mcp.py call inherits it.
export SREGYM_MCP_IMAGE="$MCP_IMAGE"
echo "[bootstrap] MCP server image $MCP_IMAGE"

# --- slot environment -------------------------------------------------------
# Run a command that prints `export` lines on stdout, and eval them only if the
# command exited 0. Returns the command's status when it failed.
#
# `eval "$(cmd)"` cannot do this. eval returns the status of the string it
# evaluates, not the status of the substitution that produced it, and both
# callers below (run_slot_env.sh and seed_slot.sh) write their errors to stderr
# and nothing to stdout when they fail. A failed run therefore evals an empty
# string and returns 0, so `if ! eval "$(cmd)"` never takes its failure branch.
# That is how a failed tenant seeding in run-20260928-0801-f50f50db carried on
# into the edge install and died there on an unbound BRAIN_EDGE_API_KEY.
#
# `local` is on its own line because `local x=$(cmd)` returns local's status,
# which is 0, and hides cmd's the same way eval does. Pass environment
# assignments through `env`, e.g. `eval_exports env FOO=bar script args`.
eval_exports() {
  local eval_exports_out
  eval_exports_out=$("$@") || return $?
  eval "$eval_exports_out"
}

# --- slot workers -----------------------------------------------------------
# Each slot is one concurrent SREGym run on this instance, and needs four
# things kept to itself. run_slot_env.sh supplies the first three (its own kind
# cluster name, its own KUBECONFIG, its own cache dir, and the API/MCP/k8s-proxy
# host ports); the fourth is its brain-go tenant, seeded per slot below.
#
# A fifth is this function's own doing: main.py writes results to
# `Path("results")` relative to the *current directory*, and
# `results/traces.db` is shared by every run under one results tree. Two slots
# in one directory would interleave into the same tree and contend on that one
# SQLite file, so each slot runs with its own CWD and gets its own results tree.
# main.py resolves its own module paths absolutely, so running it by absolute
# path from elsewhere is safe (verified).
run_slot() {
  local slot="$1"
  shift
  local problems="$*"

  local slot_dir="$WORKDIR/slots/$slot"
  local slot_log="/var/log/sregym-slot-$slot.log"
  mkdir -p "$slot_dir"

  {
    echo "[slot $slot] starting with $(echo $problems | wc -w) problems"

    # Ports, cluster name, kubeconfig and cache dir for this slot. Nothing has
    # started yet, so a failure here has nothing to stop. Without the check a
    # bad slot index or a failed mkdir left every one of these unset and the
    # slot failed later on whichever variable it read first.
    if ! eval_exports env SREGYM_SLOT_ROOT="$WORKDIR/slot-state" \
           "$WORKDIR/scripts/benchmark/run_slot_env.sh" "$slot"; then
      echo "[slot $slot] could not compute the slot environment, slot aborted"
      return 1
    fi

    # Which run, shard and slot produced a row. The driver stamps these onto
    # each investigation it opens, so a result can be traced back to the sweep
    # that made it; without them every investigation is titled alike and rows
    # are told apart only by creation time. RUN_ID and SHARD_INDEX exist above
    # but were never exported, so the driver saw none of the three.
    export SREGYM_RUN_ID="$RUN_ID"
    export SREGYM_SHARD_INDEX="$SHARD_INDEX"
    export SREGYM_SLOT="$slot"

    # The native arm binds the cluster's API server to the host's private
    # address instead of loopback, because brain-go's Kubernetes connector
    # dials it from inside a container. kind puts that address in the API
    # server certificate's SANs, so the slot's own kubeconfig stays usable and
    # TLS still verifies. The MCP and edge arms get the committed config
    # untouched: edge has no Kubernetes connection to dial the API server at
    # all, so there is nothing that needs the private bind.
    if [ "$SREGYM_ARM" = "native" ]; then
      KIND_CONFIG=$(SREGYM_KIND_CONFIG_OUT="$slot_dir/kind-config.yaml" \
        "$WORKDIR/scripts/benchmark/native_arm.sh" kind-config x86 "$SREGYM_PRIVATE_IP") || {
        echo "[slot $slot] could not write the native arm's kind config, slot aborted"
        return 1
      }
      export KIND_CONFIG
    fi

    # Each slot needs its own kind cluster. Nothing else creates it: main.py
    # exits with "Missing kubeconfig. Please set up a cluster." if it is absent,
    # and the bootstrap installs kind without ever calling it.
    echo "[slot $slot] creating kind cluster $KIND_CLUSTER_NAME"
    if ! bash "$WORKDIR/kind/setup_kind_cluster.sh" x86; then
      echo "[slot $slot] cluster creation failed, slot aborted"
      return 1
    fi

    # A kind node keeps its own image store, separate from the host docker
    # daemon that built the image, so the build above is invisible in here
    # until it is loaded. Per slot rather than per box for that reason: each
    # slot owns one cluster and the load has to follow that cluster's creation.
    # It costs one copy of the image per slot and no registry round trip.
    echo "[slot $slot] loading $SREGYM_MCP_IMAGE into $KIND_CLUSTER_NAME"
    if ! kind load docker-image "$SREGYM_MCP_IMAGE" --name "$KIND_CLUSTER_NAME"; then
      echo "[slot $slot] loading the MCP image into $KIND_CLUSTER_NAME failed, slot aborted"
      return 1
    fi

    # Put an MCP server in the cluster before seeding. The conductor deploys one
    # itself, but from deploy_app(), which runs per problem inside
    # start_problem() -- too late, because seeding has to discover the tool
    # schemas from a live server first (brain-go does not populate tools_cache
    # itself). Deploying here is not duplicative: MCPServer.deploy() skips the
    # apply when the deployment is already ready.
    #
      # It is not a no-op for the port-forward, though: deploy() has two paths
      # and either could replace one. SREGYM_EXTERNAL_PORT_FORWARD below closes
      # both. The incident and the reasoning live in one place, the
      # _forward_is_external docstring in sregym/service/mcp_server.py, rather
      # than being restated at every call site where a later correction would
      # then have to find them all.
    echo "[slot $slot] deploying the MCP server into $KIND_CLUSTER_NAME"
      # External here too, so the flag means one thing for the whole slot and
      # deploy_mcp.py's own teardown is honestly a no-op.
    if ! SREGYM_EXTERNAL_PORT_FORWARD=1 \
         uv run --project "$WORKDIR" python "$WORKDIR/scripts/benchmark/deploy_mcp.py"; then
      echo "[slot $slot] MCP server deploy failed, slot aborted"
      return 1
    fi

    # What the pod is actually running, read off the pod rather than assumed
    # from what was asked for. imageID is the resolved digest, so it separates
    # "the requested tag" from "the bytes that started", which is exactly the
    # distinction that went unnoticed while every pod silently ran a build from
    # 2026-09-06. Written as .json into slot_dir, under SLOTS_DIR, so the
    # periodic and final `aws s3 sync` ship it with that slot's results and
    # archive_run.sh's existing *.json include pulls it back down.
    jq -nc \
      --arg requested "$SREGYM_MCP_IMAGE" \
      --arg image "$(kubectl get pod -n sregym -l app.kubernetes.io/name=mcp-server \
        -o jsonpath='{.items[*].status.containerStatuses[*].image}' 2>/dev/null)" \
      --arg image_id "$(kubectl get pod -n sregym -l app.kubernetes.io/name=mcp-server \
        -o jsonpath='{.items[*].status.containerStatuses[*].imageID}' 2>/dev/null)" \
      '{requested_image: $requested, pod_image: $image, pod_image_id: $image_id}' \
      >"$slot_dir/mcp_pod_image.json" || true

    # The port-forward is ours, not the deploy script's. MCPServer starts one
    # as a subprocess.Popen child, which dies when that script exits, so the
    # script deliberately tears its own down and leaves this to us. Started
    # with nohup under this slot's subshell so it lives as long as the slot.
    #
    # Bound to 0.0.0.0 because brain-go dials it from inside a container, where
    # 127.0.0.1 is that container's own loopback: seed_slot.sh registers the
    # connections against the host's private address, not localhost.
    echo "[slot $slot] starting the MCP port-forward on $MCP_SERVER_PORT"
    nohup kubectl port-forward svc/mcp-server "$MCP_SERVER_PORT:9954" \
      -n sregym --address 0.0.0.0 >>"/var/log/sregym-pf-$slot.log" 2>&1 &
    PF_PID=$!

    # Confirm it is actually serving before handing off. Without this the slot
    # runs on to seed_slot.sh, which waits 300s on an endpoint that will never
    # answer and then aborts, which is precisely how the previous run failed.
    #
    # Serving means the app answered, not that it answered 200: see
    # mcp_probe_answered above for why the deployed image replies 404 here and
    # why that still proves the forward carries traffic. A slot that gets past
    # this and then meets a genuinely broken server still dies, at
    # seed_slot.sh's strict-200 wait on /kubectl/sse.
    pf_ok=0
    pf_deadline=$(( $(date +%s) + 60 ))
    while [ "$(date +%s)" -lt "$pf_deadline" ]; do
      if mcp_probe_answered "$(mcp_probe_code)"; then
        pf_ok=1
        break
      fi
      kill -0 "$PF_PID" 2>/dev/null || break
      sleep 2
    done
    if [ "$pf_ok" != "1" ]; then
      echo "[slot $slot] MCP port-forward never served on $MCP_SERVER_PORT, slot aborted"
      kill "$PF_PID" 2>/dev/null || true
      return 1
    fi
    echo "[slot $slot] MCP port-forward serving on $MCP_SERVER_PORT (pid $PF_PID)"

    # Through a file, not a variable. The supervisor below runs in a subshell,
    # so a PF_PID it assigns there is invisible out here: teardown would kill
    # the original pid, already killed by the supervisor, while the forward
    # really running had been reparented to init and outlived the slot.
    PF_PID_FILE="$slot_dir/mcp-pf.pid"
    echo "$PF_PID" >"$PF_PID_FILE"

    # Every restart the supervisor below performs, every probe timeout it
    # declines to restart on, and every periodic deep check (see
    # PF_DEEP_CHECK_EVERY_N_CYCLES below), is appended here as one JSON
    # line. Three event names, and only one of them is a restart:
    # "restart" the forward was replaced, "probe_timeout_forward_alive"
    # /healthz went unanswered for PF_MISSES_BEFORE_RESTART probes in a row
    # while the forward was still up, so the server was the unresponsive part
    # and nothing was killed, and "deep_check_failed" /kubectl/sse answered
    # something other than 200. readout_run.py counts "restart" lines, so a
    # non-restart event must never borrow that name.
    #
    # Fields are added, never renamed: a reader of an older run's file still
    # finds every field it knew. consecutive_misses is the newest, and says
    # how many probes in a row had missed when the line was written.
    #
    # The file lives under slot_dir, inside SLOTS_DIR, so the
    # periodic and final `aws s3 sync` calls already ship it beside that
    # slot's *_results.csv files with no separate wiring: a post-hoc pass can
    # join on problem_id to flag which attempts ran through an outage instead
    # of scoring them as plain reasoning failures.
    #
    # What a line can and cannot promise: problem_id and previous_problem_id
    # come from two marker files the problem loop below maintains --
    # current_problem (the problem actively running right now, if any) and
    # previous_problem (whichever ran immediately before that, updated when
    # each problem ends, and otherwise left alone). detection_lag_seconds is
    # the measured wall-clock time since the supervisor's last confirmed
    # /healthz success, not the static PF_PROBE_INTERVAL_SECONDS: usually
    # close to that value, but a restart detected on the cycle right after a
    # deep check (see PF_DEEP_CHECK_EVERY_N_CYCLES below) reports roughly
    # PF_PROBE_INTERVAL_SECONDS + PF_DEEP_CHECK_TIMEOUT_SECONDS instead,
    # because that earlier cycle's deep check blocked for close to
    # PF_DEEP_CHECK_TIMEOUT_SECONDS against a live SSE stream (the same
    # endless-body mechanism healthz() documents) before this cycle's sleep
    # even began. Recording the real elapsed time rather than the nominal
    # interval means this field is always a true bound on how far before ts
    # the outage could have started, never an approximation a reader could be
    # misled by. That bound is comfortably long enough for the outage to have
    # begun while previous_problem_id, not problem_id, was the one actively
    # running. This file can narrow a contaminated attempt to one of those
    # two candidates; it cannot say which one, and a reader who needs that
    # precision has to cross-reference each attempt's own timing.
    #
    # Never truncated here. The resume block above pulls down whatever this
    # slot already wrote to S3 before a prior instance died, same as its
    # *_results.csv files, and clearing it on every slot start would discard a
    # replaced instance's restart history right before the next periodic sync
    # overwrote S3 with the empty file. Append-only otherwise: an event
    # recorded twice is a false positive a reader can discount, where one
    # recorded zero times cannot be recovered.
    #
    # Built with jq, not string-interpolated into a literal. problem ids come
    # from a fixed, code-reviewed registry today, but a `printf` literal would
    # still corrupt every later line's parseability the day one carries a
    # quote, and jq costs nothing extra here: it is already an apt-get
    # dependency of this box (see bootstrap.sh.tftpl's host-prerequisites
    # section) for exactly this kind of small, safe JSON assembly.
    PF_RESTARTS_FILE="$slot_dir/mcp_pf_restarts.jsonl"
    CURRENT_PROBLEM_FILE="$slot_dir/current_problem"
    PREVIOUS_PROBLEM_FILE="$slot_dir/previous_problem"
    record_pf_event() {
      local restarts_file="$1" current_file="$2" previous_file="$3"
      local event="$4" old_pid="$5" new_pid="$6" detail="$7" lag_seconds="$8"
      local misses="${9:-0}"
      local problem previous
      problem="$(cat "$current_file" 2>/dev/null || true)"
      previous="$(cat "$previous_file" 2>/dev/null || true)"
      jq -nc \
        --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --arg event "$event" \
        --arg old_pid "$old_pid" \
        --arg new_pid "$new_pid" \
        --arg problem_id "$problem" \
        --arg previous_problem_id "$previous" \
        --arg detail "$detail" \
        --argjson detection_lag_seconds "$lag_seconds" \
        --argjson consecutive_misses "$misses" \
        '{ts: $ts, event: $event, old_pid: $old_pid, new_pid: $new_pid,
          problem_id: $problem_id, previous_problem_id: $previous_problem_id,
          detection_lag_seconds: $detection_lag_seconds,
          consecutive_misses: $consecutive_misses, detail: $detail}' \
        >>"$restarts_file"
    }

    # Supervise it. A forward dies on its own: three attempts in
    # run-20260831-1752 stalled with nothing logging a restart. The conductor
    # no longer watches it either (SREGYM_EXTERNAL_PORT_FORWARD below), so
    # ownership without supervision would move the outage, not remove it.
    #
    # Probed, not pinged: the process stays alive while forwarding to a pod
    # that never becomes ready, and requests then cost a full timeout each.
    # /healthz answers immediately rather than blocking for --max-time against
    # an endless text/event-stream, so a healthy probe no longer adds its own
    # 5s to every cycle: this loop costs one cycle of blindness on a death, not
    # two, against streaks of 28 to 85 measured in run-20260831-1752.
    #
    # This narrows the exposure window; it does not close it. The deeper cause
    # (sregym#95) is that exec_kubectl_cmd_safely is a sync def FastMCP calls
    # inline on a single-worker uvicorn, so a blocking kubectl call can stall
    # the whole event loop and starve /healthz exactly as it used to starve
    # /kubectl/sse. A busy server and a dead forward look identical to
    # /healthz, so this loop does not decide on /healthz alone. Two rules
    # below separate them: PF_MISSES_BEFORE_RESTART, and a check that the
    # forward itself is gone before anything is killed.
    #
    # run-20260921-1250-532577fe is why. All 43 restarts in that run were
    # false positives. A `kubectl rollout status --timeout=60s` or
    # `--timeout=120s` inside the MCP server blocked its single event loop,
    # /healthz went unanswered for the length of the command, and this loop
    # killed a healthy forward every 19 seconds for that whole stretch. Each
    # kill severed brain-go's SSE session, so a 30s tool call that would have
    # finished became three failed retries and a 120s circuit-breaker trip.
    # Five of the run's eight trips came from these loops, and one slot lost
    # the last two minutes of all three mitigations to them.
    #
    # /healthz does not cover the SSE mounts themselves, so a broken Mount or
    # a fastmcp/starlette regression isolated to create_sse_app would report
    # healthy here while every real tool call failed silently, with nothing
    # to restart and no signal. PF_DEEP_CHECK_EVERY_N_CYCLES below trades
    # that coverage back at a much lower frequency, cheaply, rather than
    # leaving the gap open: at PF_PROBE_INTERVAL_SECONDS=15 and
    # PF_DEEP_CHECK_EVERY_N_CYCLES=20 it costs at most
    # PF_DEEP_CHECK_TIMEOUT_SECONDS once every 5 minutes per slot, against
    # that same cost on every 15s cycle under the old probe. It only logs a
    # failure (see record_pf_event's "deep_check_failed" event, below); it
    # never restarts on one. Restarting the local kubectl process cannot fix
    # a server-side application bug, and tying a known-futile restart to it
    # would only relearn run-20260831-1752's lesson from the other direction
    # -- action with no effect, this time against the wrong layer.
    PF_PROBE_INTERVAL_SECONDS=15
    PF_DEEP_CHECK_EVERY_N_CYCLES=20
    PF_DEEP_CHECK_TIMEOUT_SECONDS=5

    # How many probes in a row must miss before a restart is considered at
    # all. 3 at PF_PROBE_INTERVAL_SECONDS=15 is 45 seconds of silence, which
    # covers brain-go's 30s per-call timeout: a tool call that is going to
    # fail on its own has already failed before the supervisor acts, so a
    # restart can no longer be what breaks a call that would have completed.
    # One miss is not evidence of anything. A single success resets the
    # count, so the three have to be consecutive.
    PF_MISSES_BEFORE_RESTART=3
    (
      pf_cycle=0
      # Consecutive /healthz misses. Reset by any success and by a restart,
      # never by a miss.
      pf_consecutive_misses=0
      # The lag record_pf_event writes is measured against this, not against
      # PF_PROBE_INTERVAL_SECONDS. Updated only when the healthz probe below
      # succeeds, never on a failure and never after the deep check: the
      # checkpoint means "the last time /healthz confirmed the forward up".
      # Leaving it alone on a failure is what makes a crash loop's lag grow
      # cycle over cycle instead of resetting to one interval every time (see
      # the comment where pf_lag is used, below). Leaving it alone after the
      # deep check is what makes that check's own several seconds show up as
      # exposure on whichever event reports it, not get silently absorbed
      # into the baseline a later cycle measures against.
      pf_prev_checkpoint_epoch=$(date +%s)
      while true; do
        sleep "$PF_PROBE_INTERVAL_SECONDS"
        pf_cycle=$(( pf_cycle + 1 ))
        pf_code=$(mcp_probe_code)
        pf_checkpoint_epoch=$(date +%s)
        pf_lag=$(( pf_checkpoint_epoch - pf_prev_checkpoint_epoch ))
        if ! mcp_probe_answered "$pf_code"; then
          # pf_prev_checkpoint_epoch is deliberately NOT advanced here. It
          # still names the last confirmed-good probe, so a second
          # consecutive failure's pf_lag accumulates on top of the first
          # instead of resetting to one interval every cycle -- a crash loop
          # of N restarts must report growing lag, not N copies of ~15.
          pf_consecutive_misses=$(( pf_consecutive_misses + 1 ))
          if [ "$pf_consecutive_misses" -lt "$PF_MISSES_BEFORE_RESTART" ]; then
            echo "[slot $slot] MCP probe missed ($pf_consecutive_misses of $PF_MISSES_BEFORE_RESTART), waiting"
            continue
          fi

          old_pid="$(cat "$PF_PID_FILE" 2>/dev/null || true)"
          if pf_forward_alive "$old_pid"; then
            # The process is running and the port still completes a
            # handshake, so the forward is carrying traffic and killing it
            # would sever whatever is on it. The server is the unresponsive
            # part, and a local restart cannot fix a server. Recorded under
            # its own event name so the archive still shows the outage, and
            # so the "restart" count stays a count of restarts.
            echo "[slot $slot] MCP probe silent for $pf_consecutive_misses cycles, forward alive, not restarting"
            record_pf_event "$PF_RESTARTS_FILE" "$CURRENT_PROBLEM_FILE" "$PREVIOUS_PROBLEM_FILE" \
              "probe_timeout_forward_alive" "$old_pid" "$old_pid" "healthz $pf_code" \
              "$pf_lag" "$pf_consecutive_misses"
            continue
          fi

          echo "[slot $slot] MCP port-forward gone after $pf_consecutive_misses probes, restarting"
          kill "$old_pid" 2>/dev/null || true
          # No pause before rebinding. The old one-second wait existed so the
          # dying kubectl could release the port first, and it cost a live
          # tool call a "connection refused" every time it ran. It is no
          # longer needed: the branch is only reached once pf_forward_alive
          # has said the process is gone or the port cannot carry a stream.
          # In the first two cases there is no listener left to release; in
          # the accept-and-close case the old process is killed just above
          # and its socket was already dropping connections, so the new
          # kubectl has nothing to race.
          nohup kubectl port-forward svc/mcp-server "$MCP_SERVER_PORT:9954" \
            -n sregym --address 0.0.0.0 >>"/var/log/sregym-pf-$slot.log" 2>&1 &
          new_pid="$!"
          # Atomic, so a concurrent stop_pf reads either the old pid or the
          # new one, never a half-written file.
          echo "$new_pid" >"$PF_PID_FILE.tmp" && mv "$PF_PID_FILE.tmp" "$PF_PID_FILE"
          record_pf_event "$PF_RESTARTS_FILE" "$CURRENT_PROBLEM_FILE" "$PREVIOUS_PROBLEM_FILE" \
            "restart" "$old_pid" "$new_pid" "healthz $pf_code" "$pf_lag" "$pf_consecutive_misses"
          # The forward that was counted against is gone. Count the new one
          # from zero, or the next single miss would restart immediately.
          pf_consecutive_misses=0
          continue
        fi
        pf_consecutive_misses=0
        pf_prev_checkpoint_epoch="$pf_checkpoint_epoch"

        if [ $(( pf_cycle % PF_DEEP_CHECK_EVERY_N_CYCLES )) -eq 0 ]; then
          deep_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time "$PF_DEEP_CHECK_TIMEOUT_SECONDS" \
            "http://127.0.0.1:$MCP_SERVER_PORT/kubectl/sse" 2>/dev/null)
          if [ "$deep_code" != "200" ]; then
            # Measured from the same pf_prev_checkpoint_epoch as above, on
            # purpose: it is not touched here (see the comment by its
            # declaration), so this reports roughly the deep check's own
            # PF_DEEP_CHECK_TIMEOUT_SECONDS, not a stale value that already
            # absorbed it.
            deep_lag=$(( $(date +%s) - pf_prev_checkpoint_epoch ))
            cur_pid="$(cat "$PF_PID_FILE" 2>/dev/null || true)"
            echo "[slot $slot] deep check: /kubectl/sse answered $deep_code, logging only"
            record_pf_event "$PF_RESTARTS_FILE" "$CURRENT_PROBLEM_FILE" "$PREVIOUS_PROBLEM_FILE" \
              "deep_check_failed" "$cur_pid" "$cur_pid" "kubectl/sse $deep_code" "$deep_lag" \
              "$pf_consecutive_misses"
          fi
        fi
      done
    ) &
    PF_WATCH_PID=$!
    echo "[slot $slot] MCP port-forward supervisor started (pid $PF_WATCH_PID)"

    # Every exit below must stop it, or it keeps restarting a forward nothing
    # will use, into a log sync_logs uploads, for the instance's whole life.
    stop_pf() {
      # Supervisor first, so it cannot start a replacement after the kill.
      kill "$PF_WATCH_PID" 2>/dev/null || true
      kill "$(cat "$PF_PID_FILE" 2>/dev/null)" 2>/dev/null || true

      # Then by port, because the pid file alone is not enough. The
      # supervisor forks kubectl and records its pid as two statements, so a
      # stop_pf landing between them kills the pid already on file, which the
      # supervisor had just killed itself, and the new kubectl survives
      # reparented to init with nothing left that knows its pid. Each slot
      # owns its own MCP_SERVER_PORT, so this is slot-scoped, and it is what
      # mcp_server.py's _kill_stale_port_forward already does.
      for pid in $(lsof -ti "tcp:$MCP_SERVER_PORT" 2>/dev/null); do
        kill "$pid" 2>/dev/null || true
      done

      # The native arm's telemetry keeper and the three forwards it owns.
      # Keeper first, for the same reason the MCP supervisor goes first: it
      # would otherwise replace a forward after the kill. Then by port,
      # because the keeper forks kubectl and records its pid as two
      # statements and a kill landing between them leaves an orphan
      # reparented to init.
      if [ -n "${TELEMETRY_KEEPER_PID:-}" ]; then
        kill "$TELEMETRY_KEEPER_PID" 2>/dev/null || true
        for port in "$SREGYM_PROMETHEUS_PORT" "$SREGYM_LOKI_PORT" "$SREGYM_JAEGER_PORT"; do
          for pid in $(lsof -ti "tcp:$port" 2>/dev/null); do
            kill "$pid" 2>/dev/null || true
          done
        done
      fi
    }

    # The native arm's prerequisite for seeding its Kubernetes connection: the
    # credential brain-go authenticates as. Edge seeds no Kubernetes
    # connection, so it needs no kubeconfig and this step does not run for it.
    if [ "$SREGYM_ARM" = "native" ]; then
      echo "[slot $slot] creating the read-only ServiceAccount and kubeconfig"
      SREGYM_NATIVE_KUBECONFIG="$slot_dir/brain-readonly.kubeconfig"
      if ! "$WORKDIR/scripts/benchmark/native_arm.sh" kubeconfig nofire-edge "$SREGYM_NATIVE_KUBECONFIG"; then
        echo "[slot $slot] could not build the read-only kubeconfig, slot aborted"
        stop_pf
        return 1
      fi
      export SREGYM_NATIVE_KUBECONFIG
    fi

    # The host forwards the three telemetry connections are registered
    # against. Both native and edge need these: neither arm's Prometheus,
    # Loki or Jaeger connector differs from the other's.
    if [ "$SREGYM_ARM" = "native" ] || [ "$SREGYM_ARM" = "edge" ]; then
      echo "[slot $slot] keeping telemetry forwards on $SREGYM_PROMETHEUS_PORT/$SREGYM_LOKI_PORT/$SREGYM_JAEGER_PORT"
      TELEMETRY_KEEPER_PID=$("$WORKDIR/scripts/benchmark/native_arm.sh" forwards \
        "$SREGYM_PROMETHEUS_PORT" "$SREGYM_LOKI_PORT" "$SREGYM_JAEGER_PORT" \
        "/var/log/sregym-telemetry-pf-$slot.log")
    fi

    # Every slot seeds its own tenant, slot 0 included. It used to be special,
    # seeded by setup_brain_go.sh while that script brought the shared stack up,
    # but that ran before any cluster existed and so before any MCP server did:
    # the connection registration inside it could never succeed on a fresh box.
    #
    # The api URL is passed explicitly. Without it seed_slot.sh falls back to
    # $BRAIN_API_URL, which the profile file sourced above sets to the CHAT
    # service (8001) because that is what the driver talks to. Connections are
    # an api-service concern: POST /api/connections lives there, and so does
    # the csrfCookie middleware. Chat answers GET /health with a 200 and no
    # CSRF-TOKEN, so seeding failed with a message that pointed at cookies
    # rather than at the wrong port:
    #   no CSRF-TOKEN cookie in the /health response (200)
    echo "[slot $slot] seeding its own brain-go tenant"
    if ! eval_exports env BRAIN_GO_DIR="$BRAIN_GO_DIR" \
           "$WORKDIR/scripts/benchmark/seed_slot.sh" \
           "sregym-shard-$SHARD_INDEX-slot-$slot" "$MCP_SERVER_PORT" \
           "http://127.0.0.1:8000"; then
      echo "[slot $slot] tenant seeding failed, slot aborted"
      stop_pf
      return 1
    fi

    # Last, because it needs the edge API key seed_slot.sh only mints once the
    # tenant exists. install-edge waits for the agent's pod and for its first
    # publish to reach brain-go, so a slot whose agent cannot start or cannot
    # publish fails here, with the agent's logs in the slot log, rather than
    # at the first attempt's entity-count gate. Both native and edge run the
    # same edge agent the same way; neither arm changes what install-edge does.
    if [ "$SREGYM_ARM" = "native" ] || [ "$SREGYM_ARM" = "edge" ]; then
      # seed_slot.sh prints BRAIN_EDGE_API_KEY only when it minted one. Checked
      # here so a seeding that exits 0 without it aborts with this message
      # rather than with `set -u` killing the slot on the read below.
      if [ -z "${BRAIN_EDGE_API_KEY:-}" ]; then
        echo "[slot $slot] seeding printed no BRAIN_EDGE_API_KEY, slot aborted"
        stop_pf
        return 1
      fi
      echo "[slot $slot] installing the edge agent into $KIND_CLUSTER_NAME"
      if ! "$WORKDIR/scripts/benchmark/native_arm.sh" install-edge \
             "$KIND_CLUSTER_NAME" "$EDGE_GRAPH_URL" "$BRAIN_EDGE_API_KEY" nofire-edge; then
        echo "[slot $slot] edge agent install failed, slot aborted"
        stop_pf
        return 1
      fi
    fi

    cd "$slot_dir" || { stop_pf; return 1; }

    # This slot's own prior results, pulled above as part of the whole slots/
    # tree. Each slot resumes from its own CSV, not a shared one, and a
    # restarted slot has several, so they are merged rather than picked.
    local resume_arg resume_csv
    resume_arg=""
    resume_csv=""
    if [ -x "$WORKDIR/scripts/benchmark/merge_resume.py" ]; then
      resume_csv=$("$WORKDIR/scripts/benchmark/merge_resume.py" "$slot_dir" || true)
    else
      # Loud, because the silent version of this is a full re-run that looks
      # like a normal run. SREGYM_REF can point at a commit older than the
      # script, and the cost is the whole shard's completed work.
      echo "[slot $slot] WARNING: merge_resume.py absent at $SREGYM_REF; not resuming" >&2
    fi
    if [ -n "$resume_csv" ]; then
      echo "[slot $slot] resuming from $resume_csv"
      resume_arg="--resume $resume_csv"
    fi

    # The forward is this subshell's, established and supervised above; without
    # this the conductor replaces it once per problem. See the deploy comment.
    export SREGYM_EXTERNAL_PORT_FORWARD=1

    for pid in $problems; do
      echo "[slot $slot] === $pid ==="
      # Named so a restart the supervisor records while this call is running
      # can be attributed to it (see record_pf_event above). Removed right
      # after, not merely overwritten next iteration: a restart landing in the
      # gap between problems must read as unattributed-to-this-problem, not
      # be misattributed to whichever problem happened to run last.
      echo "$pid" >"$CURRENT_PROBLEM_FILE"
      uv run --project "$WORKDIR" python "$WORKDIR/main.py" \
        --agent brain_go \
        --problem "$pid" \
        --model "$AGENT_MODEL" \
        --judge-model "$JUDGE_MODEL" \
        --n-attempts $N_ATTEMPTS \
        --agent-timeout $AGENT_TIMEOUT_SECONDS \
        $NOISE_FLAG $resume_arg || echo "[slot $slot] $pid failed, continuing"
      # Snapshotted into previous_problem before current is cleared, not
      # after: this is the one write site where current still holds this
      # problem's own id, which is exactly the value a restart detected up to
      # PF_PROBE_INTERVAL_SECONDS after this problem ends should be able to
      # name as a candidate (see record_pf_event's own comment above).
      cp "$CURRENT_PROBLEM_FILE" "$PREVIOUS_PROBLEM_FILE" 2>/dev/null || true
      rm -f "$CURRENT_PROBLEM_FILE"
      sync_results || true
    done

    # Stop supervising before the slot's subshell exits, so a finished slot
    # does not leave a loop restarting a forward nothing will use.
    stop_pf

    echo "[slot $slot] complete"
  } >>"$slot_log" 2>&1
}

# Split this shard's problems across the slots the same way the shard itself
# was split across instances: static, index-based, no coordinator. A slot that
# draws no problems simply never starts.
SLOT_PIDS=""
for slot in $(seq 0 $(( RUNS_PER_INSTANCE - 1 ))); do
  slot_problems=""
  i=0
  for pid in $PROBLEMS; do
    if [ $(( i % RUNS_PER_INSTANCE )) -eq "$slot" ]; then
      slot_problems="$slot_problems $pid"
    fi
    i=$(( i + 1 ))
  done

  if [ -z "$slot_problems" ]; then
    echo "[bootstrap] slot $slot drew no problems, skipping"
    continue
  fi

  run_slot "$slot" $slot_problems &
  SLOT_PIDS="$SLOT_PIDS $!"
done

# Wait on the slots by pid rather than a bare `wait`, which would also wait on
# the sync watcher and the runtime-cap timer and never return.
for p in $SLOT_PIDS; do
  wait "$p" || echo "[bootstrap] a slot exited non-zero"
done

# --- coverage ----------------------------------------------------------
# A problem that lost every attempt to an exhausted deploy retry writes no
# row under any run_N directory: main.py's marker for it lives only in the
# _ALL_ rollup, a sibling this glob does not match on purpose (the same
# pattern score_run.py's load() uses, to avoid double-counting that rollup).
# Every slot still exits 0 and prints "complete" for a shard that lost a
# problem this way, so "shard complete" said only that the for loop above
# ended, not that the work landed -- exactly how run-20260902-0606 read as
# clean at this layer. Count what actually landed instead.
#
# Per attempt, not per problem: a problem with 1 of 3 attempts scored is not
# "covered" in the sense this message means to report, and counting distinct
# problem_ids alone would have printed a shard missing two thirds of its
# attempts as fully covered. Matches score_run.py's own coverage() unit.
#
# Through the environment, not string interpolation: this file already
# learned that lesson once, splicing PROBLEM_SET straight into a python
# literal until a value containing a quote could break out of it (see
# PROBLEMS' own resolution above).
COVERED_ATTEMPTS=$(PROBLEMS="$PROBLEMS" SLOTS_DIR="$SLOTS_DIR" N_ATTEMPTS="$N_ATTEMPTS" uv run python -c "
import csv
import os
from pathlib import Path

problems = set(os.environ.get('PROBLEMS', '').split())
try:
    n_attempts = int(os.environ.get('N_ATTEMPTS', '0'))
except ValueError:
    n_attempts = 0
covered = set()
for path in Path(os.environ['SLOTS_DIR']).rglob('*/run_*/*_results.csv'):
    with path.open(newline='', encoding='utf-8', errors='replace') as fh:
        for row in csv.DictReader(fh):
            pid = (row.get('problem_id') or '').strip()
            attempt = (row.get('attempt') or '').strip()
            if pid in problems and attempt.isdigit() and 1 <= int(attempt) <= n_attempts:
                covered.add((pid, int(attempt)))
print(len(covered))
" 2>/dev/null)
TOTAL_ATTEMPTS=$(( $(echo $PROBLEMS | wc -w | tr -d ' ') * ${N_ATTEMPTS:-0} ))
# Empty rather than trusted: a failure in the python above (an unreadable
# CSV, no uv on PATH) must not make a lost attempt print as covered.
[ -n "$COVERED_ATTEMPTS" ] || COVERED_ATTEMPTS=0

echo "[bootstrap] shard $SHARD_INDEX complete (all $RUNS_PER_INSTANCE slots)"
# "success" so keep_alive_on_failure does not hold a finished box up. Every
# other terminate call omits it, which reads as a failure and is.
#
# The flag is named for failures but terminate() is also this path, so before
# this a debug flag meant to preserve a broken box for inspection preserved a
# healthy one with nothing left to inspect. It went unnoticed for as long as
# smoke could not finish inside its runtime cap, since hard_stop ignores the
# flag and the cap was the only exit smoke ever reached. Raising that cap
# surfaced it on run-20260902-2008 and again on run-20260903-0558.
terminate "shard complete ($COVERED_ATTEMPTS/$TOTAL_ATTEMPTS attempts)" success
