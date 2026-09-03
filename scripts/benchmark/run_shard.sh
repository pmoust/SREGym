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
# reconstruct which 90 of the 137 registered problems the paper used.
if [ -z "$PROBLEM_SET" ]; then
  echo "[bootstrap] problem_set is empty, which means the full registry."
  echo "[bootstrap] That set is undefined: 137 problems are registered, the paper"
  echo "[bootstrap] used 90, and nothing in the repo says which. See NOFireAI/brain-go#2221."
  terminate "full-suite runs need a defined problem set"
  exit 1
fi

# PROBLEMS_OVERRIDE narrows the set to a named list, which is how a smoke run
# costs ten minutes instead of a full suite. Filtered through the set rather
# than trusted: a typo would otherwise shard to nothing and terminate looking
# like a clean short run, which is the failure mode this whole file keeps
# hitting. An id not in the set is a hard stop, listing what was rejected.
# Every value reaches python through the environment, none through string
# interpolation. $PROBLEM_SET used to be spliced into the source as
# PROBLEM_SETS['$PROBLEM_SET'], where a value containing a quote breaks out of
# the literal into arbitrary python. terraform now also charset-validates the
# variable, but the two guards are independent on purpose: this one holds for
# any caller, including a hand-run invocation that never went through
# terraform.
PROBLEMS=$(PROBLEMS_OVERRIDE="${PROBLEMS_OVERRIDE:-}" PROBLEM_SET="$PROBLEM_SET" uv run python -c "
import os, sys
from sregym.conductor.problem_sets import PROBLEM_SETS

name = os.environ['PROBLEM_SET']
if name not in PROBLEM_SETS:
    sys.stderr.write('unknown problem_set: ' + name + chr(10))
    sys.stderr.write('known: ' + ', '.join(sorted(PROBLEM_SETS)) + chr(10))
    sys.exit(2)

ids = sorted(PROBLEM_SETS[name])
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

# --- run --------------------------------------------------------------------
export AGENT_MODEL_ID="$AGENT_MODEL"
export JUDGE_MODEL_ID="$JUDGE_MODEL"
export AWS_DEFAULT_REGION="$REGION"

# Every SREGYM_* intervention stays off. They exist to work around a 24 GB ARM
# laptop and they are why engineering#799's numbers cannot be read. SETTLE_SECONDS
# defaults to 180, so leaving it unset is not the same as turning it off.
export SREGYM_SETTLE_SECONDS=0
unset SREGYM_WAIT_FAULT_EVENT SREGYM_WAIT_EDGE_SYNC SREGYM_STELLARIZE \
      SREGYM_STABILIZE_PODS SREGYM_GATE_SKIP SREGYM_BASELINE_SYNC_SECONDS \
      SREGYM_SKIP_JUDGE_PREFLIGHT

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

    # Ports, cluster name, kubeconfig and cache dir for this slot.
    eval "$(SREGYM_SLOT_ROOT="$WORKDIR/slot-state" "$WORKDIR/scripts/benchmark/run_slot_env.sh" "$slot")"

    # Which run, shard and slot produced a row. The driver stamps these onto
    # each investigation it opens, so a result can be traced back to the sweep
    # that made it; without them every investigation is titled alike and rows
    # are told apart only by creation time. RUN_ID and SHARD_INDEX exist above
    # but were never exported, so the driver saw none of the three.
    export SREGYM_RUN_ID="$RUN_ID"
    export SREGYM_SHARD_INDEX="$SHARD_INDEX"
    export SREGYM_SLOT="$slot"

    # Each slot needs its own kind cluster. Nothing else creates it: main.py
    # exits with "Missing kubeconfig. Please set up a cluster." if it is absent,
    # and the bootstrap installs kind without ever calling it.
    echo "[slot $slot] creating kind cluster $KIND_CLUSTER_NAME"
    if ! bash "$WORKDIR/kind/setup_kind_cluster.sh" x86; then
      echo "[slot $slot] cluster creation failed, slot aborted"
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
    pf_ok=0
    pf_deadline=$(( $(date +%s) + 60 ))
    while [ "$(date +%s)" -lt "$pf_deadline" ]; do
      if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
              "http://127.0.0.1:$MCP_SERVER_PORT/kubectl/sse" 2>/dev/null)" = "200" ]; then
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

    # Supervise it. A forward dies on its own: three attempts in
    # run-20260831-1752 stalled with nothing logging a restart. The conductor
    # no longer watches it either (SREGYM_EXTERNAL_PORT_FORWARD below), so
    # ownership without supervision would move the outage, not remove it.
    #
    # Probed, not pinged: the process stays alive while forwarding to a pod
    # that never becomes ready, and requests then cost a full timeout each.
    # ~20s a cycle, not 15: curl blocks the full --max-time against an endless
    # text/event-stream, so a healthy probe spends 5s on top of the sleep.
    # Worst case is roughly two cycles of blindness, so a death still costs
    # one or two dead calls rather than none -- against streaks of 28 to 85
    # measured in run-20260831-1752.
    (
      while true; do
        sleep 15
        pf_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
          "http://127.0.0.1:$MCP_SERVER_PORT/kubectl/sse" 2>/dev/null)
        if [ "$pf_code" = "200" ]; then
          continue
        fi
        echo "[slot $slot] MCP port-forward stopped serving, restarting"
        kill "$(cat "$PF_PID_FILE" 2>/dev/null)" 2>/dev/null || true
        # Let the old process release the port before rebinding, or the new
        # kubectl loses the race, exits, and the port stays dead a full cycle.
        sleep 1
        nohup kubectl port-forward svc/mcp-server "$MCP_SERVER_PORT:9954" \
          -n sregym --address 0.0.0.0 >>"/var/log/sregym-pf-$slot.log" 2>&1 &
        # Atomic, so a concurrent stop_pf reads either the old pid or the
        # new one, never a half-written file.
        echo "$!" >"$PF_PID_FILE.tmp" && mv "$PF_PID_FILE.tmp" "$PF_PID_FILE"
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
    }

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
    if ! eval "$(BRAIN_GO_DIR="$BRAIN_GO_DIR" "$WORKDIR/scripts/benchmark/seed_slot.sh" \
           "sregym-shard-$SHARD_INDEX-slot-$slot" "$MCP_SERVER_PORT" \
           "http://127.0.0.1:8000")"; then
      echo "[slot $slot] tenant seeding failed, slot aborted"
      stop_pf
      return 1
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
      uv run --project "$WORKDIR" python "$WORKDIR/main.py" \
        --agent brain_go \
        --problem "$pid" \
        --model "$AGENT_MODEL" \
        --judge-model "$JUDGE_MODEL" \
        --n-attempts $N_ATTEMPTS \
        --agent-timeout $AGENT_TIMEOUT_SECONDS \
        $NOISE_FLAG $resume_arg || echo "[slot $slot] $pid failed, continuing"
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
terminate "shard complete" success
