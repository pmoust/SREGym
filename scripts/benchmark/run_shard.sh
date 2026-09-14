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
    #
    # Probed on /healthz, a plain route the app answers directly, not one of
    # the /kubectl, /jaeger, /loki, /prometheus, /submit SSE mounts: curl
    # against one of those holds the connection open for the whole --max-time
    # even on a healthy, instantly-answering endpoint, because the body is an
    # endless stream and only --max-time ends it. See the healthz() docstring
    # in mcp_server/sregym_mcp_server.py for what that costs and why it is not
    # a tool-session leak.
    pf_ok=0
    pf_deadline=$(( $(date +%s) + 60 ))
    while [ "$(date +%s)" -lt "$pf_deadline" ]; do
      if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
              "http://127.0.0.1:$MCP_SERVER_PORT/healthz" 2>/dev/null)" = "200" ]; then
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

    # Every restart the supervisor below performs, and every periodic deep
    # check (see PF_DEEP_CHECK_EVERY_N_CYCLES below), is appended here as one
    # JSON line. The file lives under slot_dir, inside SLOTS_DIR, so the
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
        '{ts: $ts, event: $event, old_pid: $old_pid, new_pid: $new_pid,
          problem_id: $problem_id, previous_problem_id: $previous_problem_id,
          detection_lag_seconds: $detection_lag_seconds, detail: $detail}' \
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
    # /kubectl/sse. What is left of that is visible in this file as restarts
    # whose gap from the previous one is well under
    # PF_PROBE_INTERVAL_SECONDS: this loop cannot distinguish "the pod is
    # actually down" from "the pod is too busy to answer for a moment", and
    # fixing that distinction is out of scope here.
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
    (
      pf_cycle=0
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
        pf_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
          "http://127.0.0.1:$MCP_SERVER_PORT/healthz" 2>/dev/null)
        pf_checkpoint_epoch=$(date +%s)
        pf_lag=$(( pf_checkpoint_epoch - pf_prev_checkpoint_epoch ))
        if [ "$pf_code" != "200" ]; then
          # pf_prev_checkpoint_epoch is deliberately NOT advanced here. It
          # still names the last confirmed-good probe, so a second
          # consecutive failure's pf_lag accumulates on top of the first
          # instead of resetting to one interval every cycle -- a crash loop
          # of N restarts must report growing lag, not N copies of ~15.
          old_pid="$(cat "$PF_PID_FILE" 2>/dev/null || true)"
          echo "[slot $slot] MCP port-forward stopped serving, restarting"
          kill "$old_pid" 2>/dev/null || true
          # Let the old process release the port before rebinding, or the new
          # kubectl loses the race, exits, and the port stays dead a full cycle.
          sleep 1
          nohup kubectl port-forward svc/mcp-server "$MCP_SERVER_PORT:9954" \
            -n sregym --address 0.0.0.0 >>"/var/log/sregym-pf-$slot.log" 2>&1 &
          new_pid="$!"
          # Atomic, so a concurrent stop_pf reads either the old pid or the
          # new one, never a half-written file.
          echo "$new_pid" >"$PF_PID_FILE.tmp" && mv "$PF_PID_FILE.tmp" "$PF_PID_FILE"
          record_pf_event "$PF_RESTARTS_FILE" "$CURRENT_PROBLEM_FILE" "$PREVIOUS_PROBLEM_FILE" \
            "restart" "$old_pid" "$new_pid" "healthz $pf_code" "$pf_lag"
          continue
        fi
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
              "deep_check_failed" "$cur_pid" "$cur_pid" "kubectl/sse $deep_code" "$deep_lag"
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
