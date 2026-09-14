"""Port-forward restart and deep-check events land in the slot's own results tree.

Before this, a forward restart was visible only as a line in
/var/log/sregym-slot-N.log: off the box entirely unless keep_alive_on_failure
held the instance up, and even on the box it carried no timestamp and no link
to which problem was running. record_pf_event() in run_shard.sh writes one
JSON line per restart (and per low-frequency deep check failure) into
slot_dir/mcp_pf_restarts.jsonl instead, which sync_results already ships to
S3 beside that slot's own *_results.csv files, so a later pass can flag which
attempts ran through an outage.

record_pf_event is extracted out of run_shard.sh and run as shell, the same
technique test_terminate_outcome.py uses for terminate(): the function is a
few lines embedded in a much larger one (run_slot, itself sourced rather than
executed), and bash -n cannot exercise its behaviour.
"""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN_SHARD = ROOT / "scripts" / "benchmark" / "run_shard.sh"


def _record_pf_event_fn() -> str:
    """The record_pf_event() function body, verbatim."""
    text = RUN_SHARD.read_text()
    body = text.split("record_pf_event() {", 1)[1].split("\n    }\n", 1)[0]
    return "record_pf_event() {" + body + "\n    }\n"


def _harness(tmp_path) -> Path:
    """A script that calls record_pf_event on its own argv, so a test value
    (a problem id with a quote, a pid with shell metacharacters) is passed
    through the OS argv array rather than interpolated into script text --
    the same way run_shard.sh's own call sites pass args, and the only way to
    test FINDING 2's escaping fix without the test harness itself needing the
    same escaping the fix is supposed to provide."""
    script = tmp_path / "t.sh"
    script.write_text(_record_pf_event_fn() + 'record_pf_event "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8"\n')
    return script


def _run(
    tmp_path,
    *,
    current_contents: str | None,
    previous_contents: str | None = None,
    event="restart",
    old_pid="111",
    new_pid="222",
    detail="healthz 000",
    lag_seconds="15",
):
    restarts_file = tmp_path / "mcp_pf_restarts.jsonl"
    current_file = tmp_path / "current_problem"
    previous_file = tmp_path / "previous_problem"
    if current_contents is not None:
        current_file.write_text(current_contents)
    if previous_contents is not None:
        previous_file.write_text(previous_contents)

    script = _harness(tmp_path)
    argv = [
        "bash",
        str(script),
        str(restarts_file),
        str(current_file),
        str(previous_file),
        event,
        old_pid,
        new_pid,
        detail,
        lag_seconds,
    ]
    proc = subprocess.run(argv, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    return restarts_file


def test_a_restart_during_a_problem_records_its_problem_id(tmp_path):
    restarts_file = _run(tmp_path, current_contents="admission_webhook_outage_hotel_reservation\n")

    row = json.loads(restarts_file.read_text().strip())
    assert row["event"] == "restart"
    assert row["problem_id"] == "admission_webhook_outage_hotel_reservation"
    assert row["old_pid"] == "111"
    assert row["new_pid"] == "222"


def test_a_restart_between_problems_records_an_empty_problem_id_but_names_the_previous_one(tmp_path):
    """current_problem is removed between problems (see run_slot's for loop),
    so a restart landing in that gap must read as unattributed rather than
    being silently misattributed to whichever problem happened to run last.
    previous_problem still names the one that just finished, since a restart
    detected shortly into the gap may in fact belong to it (see FINDING 3 /
    detection_lag_seconds below)."""
    restarts_file = _run(tmp_path, current_contents=None, previous_contents="admission_webhook_outage\n")

    row = json.loads(restarts_file.read_text().strip())
    assert row["problem_id"] == ""
    assert row["previous_problem_id"] == "admission_webhook_outage"


def test_detection_lag_seconds_is_passed_through_verbatim(tmp_path):
    """record_pf_event no longer sources detection_lag_seconds from the
    static PF_PROBE_INTERVAL_SECONDS: the caller (the supervisor loop) now
    measures the real elapsed time since its last confirmed /healthz success
    and passes it in, because a cycle that also runs the deep check blocks
    for close to PF_DEEP_CHECK_TIMEOUT_SECONDS longer by the same
    endless-body mechanism healthz() documents -- a reader treating the old
    static 15 as a strict bound was wrong on roughly one restart in
    PF_DEEP_CHECK_EVERY_N_CYCLES. This only pins the passthrough itself; the
    supervisor's own measurement is covered by the dynamic tests below."""
    restarts_file = _run(tmp_path, current_contents="p\n", lag_seconds="42")

    row = json.loads(restarts_file.read_text().strip())
    assert row["detection_lag_seconds"] == 42


def test_the_supervisor_computes_lag_from_measured_elapsed_time_not_a_constant():
    """Static guard on the fix itself: neither call site may pass
    PF_PROBE_INTERVAL_SECONDS straight through. The restart site measures
    against the checkpoint via pf_lag; the deep-check site measures its own,
    shorter deep_lag from the same checkpoint rather than reusing pf_lag,
    which by the time the deep check runs would already be stale (see the
    dynamic tests below for why that distinction matters)."""
    body = RUN_SHARD.read_text()

    assert "pf_lag=$(( pf_checkpoint_epoch - pf_prev_checkpoint_epoch ))" in body
    assert "deep_lag=$(( $(date +%s) - pf_prev_checkpoint_epoch ))" in body
    assert '"$pf_lag"' in body
    assert '"$deep_lag"' in body


def test_the_timestamp_is_utc_iso8601(tmp_path):
    restarts_file = _run(tmp_path, current_contents="x\n")

    row = json.loads(restarts_file.read_text().strip())
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", row["ts"]), row["ts"]


def test_events_append_rather_than_overwrite(tmp_path):
    """Two events in one slot's life must both survive, as separate lines.
    Modeled as two separate invocations, same as two loop iterations of the
    supervisor calling record_pf_event independently in production."""
    restarts_file = tmp_path / "mcp_pf_restarts.jsonl"
    current_file = tmp_path / "current_problem"
    previous_file = tmp_path / "previous_problem"
    current_file.write_text("p1\n")

    script = _harness(tmp_path)
    for old_pid, new_pid, detail in [("111", "222", "d1"), ("222", "333", "d2")]:
        argv = [
            "bash",
            str(script),
            str(restarts_file),
            str(current_file),
            str(previous_file),
            "restart",
            old_pid,
            new_pid,
            detail,
            "15",
        ]
        proc = subprocess.run(argv, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr

    lines = restarts_file.read_text().strip().splitlines()
    assert len(lines) == 2
    assert [json.loads(row)["old_pid"] for row in lines] == ["111", "222"]


def test_a_problem_id_containing_a_quote_and_backslash_does_not_corrupt_the_line(tmp_path):
    """FINDING 2's regression. Before this, the line was a printf literal with
    $problem interpolated unescaped: one embedded double quote broke that
    line's own field boundaries and, since JSON Lines has no line-continuation,
    corrupted every line after it for a naive by-line reader. jq -nc with
    --arg escapes the value instead of splicing it, so this must round-trip
    exactly regardless of content, not merely for the closed set of problem
    ids the registry happens to contain today."""
    nasty = 'admission"webhook\\outage\nwith-a-newline'
    restarts_file = _run(tmp_path, current_contents=nasty)

    lines = restarts_file.read_text().strip().splitlines()
    assert len(lines) == 1, "an embedded quote/newline must not split this into multiple lines"
    row = json.loads(lines[0])
    assert row["problem_id"] == nasty
    assert row["old_pid"] == "111"


def test_a_pid_containing_a_quote_does_not_corrupt_the_line(tmp_path):
    """Same regression, the other interpolated field. pids are numeric in
    practice, but the fix is generic, not a special case for problem_id."""
    restarts_file = _run(tmp_path, current_contents="p", old_pid='1"><script>2')

    row = json.loads(restarts_file.read_text().strip())
    assert row["old_pid"] == '1"><script>2'


def test_the_restarts_file_is_never_truncated_at_slot_start():
    """The regression this guards: a resumed slot on a replacement instance
    pulls its restart history down from S3 with everything else in
    SLOTS_DIR, and clearing the file here would discard it right before the
    next periodic sync overwrote S3 with the empty file."""
    body = RUN_SHARD.read_text()

    assert 'PF_RESTARTS_FILE="$slot_dir/mcp_pf_restarts.jsonl"' in body
    assert ': >"$PF_RESTARTS_FILE"' not in body
    assert ": >$PF_RESTARTS_FILE" not in body


def test_the_current_problem_marker_is_snapshotted_before_being_removed():
    """FINDING 3's fix in the problem loop: previous_problem is written from
    current_problem's own content before current_problem is cleared, so a
    restart detected shortly after a problem ends can still name it via
    previous_problem_id, instead of the marker simply vanishing."""
    body = RUN_SHARD.read_text()
    loop = body.split("for pid in $problems; do", 1)[1].split("\n    done\n", 1)[0]

    assert 'echo "$pid" >"$CURRENT_PROBLEM_FILE"' in loop
    assert 'cp "$CURRENT_PROBLEM_FILE" "$PREVIOUS_PROBLEM_FILE"' in loop
    assert 'rm -f "$CURRENT_PROBLEM_FILE"' in loop

    # Written before the run; snapshotted into previous before being removed,
    # not the other way round -- a swap here would always snapshot an empty
    # current_problem instead of this problem's own id.
    write_ix = loop.index('echo "$pid" >"$CURRENT_PROBLEM_FILE"')
    main_ix = loop.index("main.py")
    snapshot_ix = loop.index('cp "$CURRENT_PROBLEM_FILE" "$PREVIOUS_PROBLEM_FILE"')
    remove_ix = loop.index('rm -f "$CURRENT_PROBLEM_FILE"')
    assert write_ix < main_ix < snapshot_ix < remove_ix


def _pf_cycle_gate_snippet() -> str:
    """The `if pf_cycle % PF_DEEP_CHECK_EVERY_N_CYCLES == 0` block, verbatim,
    including its nested deep-check if. Bounded on the outer if's own
    indentation (8 spaces) so the split does not stop at the nested fi (10
    spaces) first."""
    text = RUN_SHARD.read_text()
    marker = "if [ $(( pf_cycle % PF_DEEP_CHECK_EVERY_N_CYCLES )) -eq 0 ]; then"
    body = text.split(marker, 1)[1].split("\n        fi\n", 1)[0]
    return marker + body + "\n        fi\n"


def test_deep_check_fires_only_every_nth_cycle(tmp_path):
    """FINDING 4's low-frequency check. A fake curl always reports a failing
    code, so every fire is observable as one record_pf_event call; the count
    across 40 cycles at N=20 must be exactly 2, at cycles 20 and 40, not on
    every healthy cycle the way the removed always-SSE probe would have."""
    restarts_file = tmp_path / "mcp_pf_restarts.jsonl"
    current_file = tmp_path / "current_problem"
    previous_file = tmp_path / "previous_problem"
    pid_file = tmp_path / "mcp-pf.pid"
    pid_file.write_text("999")

    script = tmp_path / "t.sh"
    script.write_text(
        "PF_DEEP_CHECK_EVERY_N_CYCLES=20\n"
        f'MCP_SERVER_PORT=9999\nPF_PID_FILE="{pid_file}"\n'
        f'PF_RESTARTS_FILE="{restarts_file}"\nCURRENT_PROBLEM_FILE="{current_file}"\n'
        f'PREVIOUS_PROBLEM_FILE="{previous_file}"\n'
        # Fakes curl reporting a non-200 on every deep check, so a fire is
        # unconditional whenever the gate lets the block run at all.
        'curl() { echo -n "500"; }\n'
        'record_pf_event() { echo fired >> "$PF_RESTARTS_FILE"; }\n'
        "for pf_cycle in $(seq 1 40); do\n"
        f"  {_pf_cycle_gate_snippet()}\n"
        "done\n"
    )
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    fires = restarts_file.read_text().count("fired")
    assert fires == 2, f"expected exactly 2 fires (cycles 20 and 40) at N=20 over 40 cycles, got {fires}"


def test_deep_check_failure_never_restarts_the_forward():
    """A broken SSE mount is a server-side application bug (sregym#95); the
    local kubectl port-forward process is not what is broken, so restarting
    it cannot fix it and would only add restart noise with no effect. The
    deep-check block must log via record_pf_event and nothing more -- no
    kill, no new kubectl port-forward, no write to PF_PID_FILE."""
    snippet = _pf_cycle_gate_snippet()

    assert "deep_check_failed" in snippet
    assert "kubectl port-forward" not in snippet
    assert "kill " not in snippet
    assert "PF_PID_FILE.tmp" not in snippet


def _supervisor_loop_body() -> str:
    """The whole supervisor while-loop, verbatim, from its pf_cycle=0 init
    through its closing `done` -- everything the background subshell in
    run_shard.sh runs, minus the `( ... ) &` wrapper itself. Extracted so a
    test can drive it with sleep/date/curl faked, rather than reimplementing
    its lag arithmetic separately and only proving the reimplementation
    agrees with itself.

    Carries the two probe functions the loop calls, which live at file scope
    in run_shard.sh rather than inside the loop. The real subshell inherits
    them; a harness that pastes only the loop body does not, and the loop
    would then read every cycle as a dead forward and restart on all of them.
    They are extracted rather than stubbed so a caller's fake curl still
    drives the real predicate."""
    text = RUN_SHARD.read_text()
    fns = []
    for name in ("mcp_probe_code", "mcp_probe_answered"):
        opener = f"{name}() {{"
        fns.append(opener + text.split(opener, 1)[1].split("\n}\n", 1)[0] + "\n}\n")
    loop = "pf_cycle=0" + text.split("pf_cycle=0", 1)[1].split("\n    ) &", 1)[0]
    return "\n".join(fns) + "\n" + loop


def test_a_deep_check_failure_reports_its_own_short_lag(tmp_path):
    """A deep check runs after this cycle's own /healthz already succeeded,
    so a deep_check_failed event's lag should be close to
    PF_DEEP_CHECK_TIMEOUT_SECONDS alone (measured from that same-cycle
    success), not padded by PF_PROBE_INTERVAL_SECONDS on top -- confirming
    deep_lag is not simply reusing the (by then stale) pf_lag.

    Drives the real loop body through 20 cycles with sleep, date and curl
    faked: sleep advances a shared fake clock, healthz always answers 200
    without advancing the clock, and the deep check (only reached once, when
    the cycle-20 gate fires) advances the clock by
    PF_DEEP_CHECK_TIMEOUT_SECONDS and reports a failing code.
    record_pf_event is stubbed to capture its own $8 (lag_seconds) argument.

    The clock lives in a file, not a shell variable: `pf_code=$(curl ...)`
    and `pf_checkpoint_epoch=$(date +%s)` are command substitutions, which
    bash always runs in a subshell, so an assignment a faked curl or date
    makes to a plain variable is invisible once that subshell exits. A file
    write is a real filesystem side effect and survives it.
    """
    loop_body = _supervisor_loop_body().replace("while true; do", 'while [ "$pf_cycle" -lt "$MAX_CYCLES" ]; do')

    pid_file = tmp_path / "mcp-pf.pid"
    pid_file.write_text("999")
    lag_capture = tmp_path / "lag.txt"
    clock_file = tmp_path / "clock"
    clock_file.write_text("1000")

    script = tmp_path / "t.sh"
    script.write_text(
        "PF_PROBE_INTERVAL_SECONDS=15\n"
        "PF_DEEP_CHECK_EVERY_N_CYCLES=20\n"
        "PF_DEEP_CHECK_TIMEOUT_SECONDS=5\n"
        "MCP_SERVER_PORT=9999\n"
        f'PF_PID_FILE="{pid_file}"\n'
        f'PF_RESTARTS_FILE="{tmp_path}/restarts.jsonl"\n'
        f'CURRENT_PROBLEM_FILE="{tmp_path}/current_problem"\n'
        f'PREVIOUS_PROBLEM_FILE="{tmp_path}/previous_problem"\n'
        "slot=test\n"
        "MAX_CYCLES=20\n"
        f'CLOCK_FILE="{clock_file}"\n'
        'sleep() { echo $(( $(cat "$CLOCK_FILE") + $1 )) > "$CLOCK_FILE"; }\n'
        'date() { cat "$CLOCK_FILE"; }\n'
        # healthz always answers 200 instantly; the deep check (identified by
        # its URL) is the one call this test is about, so it alone advances
        # the clock, and always fails.
        "curl() {\n"
        '  for a in "$@"; do\n'
        '    case "$a" in\n'
        "      *kubectl/sse*)\n"
        '        echo $(( $(cat "$CLOCK_FILE") + PF_DEEP_CHECK_TIMEOUT_SECONDS )) > "$CLOCK_FILE"\n'
        '        echo -n "500"; return ;;\n'
        "    esac\n"
        "  done\n"
        '  echo -n "200"\n'
        "}\n"
        f'record_pf_event() {{ echo "$8" >> "{lag_capture}"; }}\n' + loop_body + "\n"
    )
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    lags = lag_capture.read_text().split()
    assert lags == ["5"], f"expected one deep_check_failed event with lag 5 (its own deep check alone), got {lags}"


def test_a_restart_right_after_a_deep_check_cycle_reports_the_inflated_lag(tmp_path):
    """The coordinator's fix request. The deep check on cycle 20 blocks for
    close to PF_DEEP_CHECK_TIMEOUT_SECONDS against a live SSE stream whether
    or not it fails (the same endless-body mechanism healthz() documents),
    which delays how soon cycle 21's own sleep begins. So a restart detected
    on cycle 21 -- right after that deep check, even though the deep check
    itself reported healthy -- must report roughly
    PF_PROBE_INTERVAL_SECONDS + PF_DEEP_CHECK_TIMEOUT_SECONDS (20), not
    PF_PROBE_INTERVAL_SECONDS alone (15): the outage it reports could have
    started any time in that whole window, not just the last 15s of it.

    Drives the real loop body through 21 cycles: healthz answers 200 for
    every cycle except the last, where it fails and triggers a restart; the
    deep check on cycle 20 answers 200 (healthy) but still advances the fake
    clock by PF_DEEP_CHECK_TIMEOUT_SECONDS, by the same mechanism as above.
    record_pf_event is stubbed to capture its own $8 (lag_seconds) argument.

    The clock lives in a file, not a shell variable -- see the docstring on
    test_a_deep_check_failure_reports_its_own_short_lag for why a plain
    variable a faked curl or date assigns cannot survive being called
    through `$(...)`, which always forks a subshell.
    """
    loop_body = _supervisor_loop_body().replace("while true; do", 'while [ "$pf_cycle" -lt "$MAX_CYCLES" ]; do')

    pid_file = tmp_path / "mcp-pf.pid"
    pid_file.write_text("999")
    lag_capture = tmp_path / "lag.txt"
    clock_file = tmp_path / "clock"
    clock_file.write_text("1000")

    script = tmp_path / "t.sh"
    script.write_text(
        "PF_PROBE_INTERVAL_SECONDS=15\n"
        "PF_DEEP_CHECK_EVERY_N_CYCLES=20\n"
        "PF_DEEP_CHECK_TIMEOUT_SECONDS=5\n"
        "MCP_SERVER_PORT=9999\n"
        f'PF_PID_FILE="{pid_file}"\n'
        f'PF_RESTARTS_FILE="{tmp_path}/restarts.jsonl"\n'
        f'CURRENT_PROBLEM_FILE="{tmp_path}/current_problem"\n'
        f'PREVIOUS_PROBLEM_FILE="{tmp_path}/previous_problem"\n'
        "slot=test\n"
        "MAX_CYCLES=21\n"
        f'CLOCK_FILE="{clock_file}"\n'
        'sleep() { echo $(( $(cat "$CLOCK_FILE") + $1 )) > "$CLOCK_FILE"; }\n'
        'date() { cat "$CLOCK_FILE"; }\n'
        # kubectl/sse (the deep check, cycle 20 only) always advances the
        # clock and reports healthy. healthz reports healthy through cycle
        # 20 and fails on cycle 21 -- pf_cycle is a real variable in the loop
        # body's own scope by the time curl runs, so this fake reads it
        # directly rather than needing its own counter.
        "curl() {\n"
        '  for a in "$@"; do\n'
        '    case "$a" in\n'
        "      *kubectl/sse*)\n"
        '        echo $(( $(cat "$CLOCK_FILE") + PF_DEEP_CHECK_TIMEOUT_SECONDS )) > "$CLOCK_FILE"\n'
        '        echo -n "200"; return ;;\n'
        "    esac\n"
        "  done\n"
        '  if [ "$pf_cycle" = "21" ]; then echo -n "000"; else echo -n "200"; fi\n'
        "}\n"
        f'record_pf_event() {{ echo "$8" >> "{lag_capture}"; }}\n' + loop_body + "\n"
    )
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    lags = lag_capture.read_text().split()
    assert lags == ["20"], (
        f"expected one restart event with lag 20 (15s sleep + the prior cycle's 5s deep check), got {lags}"
    )


def test_a_crash_loop_reports_growing_lag_not_a_flat_interval(tmp_path):
    """The bug a later delta review caught: pf_prev_checkpoint_epoch used to
    advance unconditionally, including right after a failing healthz, so
    every restart in a crash loop reported the same one-interval lag no
    matter how long the forward had actually been down. That is exactly the
    run-20260908-0739 pattern this field exists to characterize -- bursts of
    up to 31 restarts in one slot -- so understating it there is the one
    case this feature cannot afford to get wrong.

    Drives the real loop through 3 cycles with healthz failing every time
    (curl always reports 500) and record_pf_event stubbed to capture each
    call's own $8 (lag_seconds) argument, in order. The checkpoint must stay
    pinned at the pre-loop seed throughout, since a failing healthz must
    never advance it (see the comment above pf_prev_checkpoint_epoch's
    declaration), so lag grows across the three cycles rather than resetting
    to ~15 every time. It grows by 16, not 15, after the first: the restart
    branch's own `sleep 1` ("let the old process release the port before
    rebinding") is real wall-clock time a crash loop pays on every failed
    cycle, and this fake sleep() advances the same clock file that call
    uses, so the correct expected sequence is 15, 31, 47 -- not the naive
    15, 30, 45 a reader ignoring that pause would expect.
    """
    loop_body = _supervisor_loop_body().replace("while true; do", 'while [ "$pf_cycle" -lt "$MAX_CYCLES" ]; do')

    pid_file = tmp_path / "mcp-pf.pid"
    pid_file.write_text("999")
    lag_capture = tmp_path / "lag.txt"
    clock_file = tmp_path / "clock"
    clock_file.write_text("1000")

    script = tmp_path / "t.sh"
    script.write_text(
        "PF_PROBE_INTERVAL_SECONDS=15\n"
        "PF_DEEP_CHECK_EVERY_N_CYCLES=20\n"
        "PF_DEEP_CHECK_TIMEOUT_SECONDS=5\n"
        "MCP_SERVER_PORT=9999\n"
        f'PF_PID_FILE="{pid_file}"\n'
        f'PF_RESTARTS_FILE="{tmp_path}/restarts.jsonl"\n'
        f'CURRENT_PROBLEM_FILE="{tmp_path}/current_problem"\n'
        f'PREVIOUS_PROBLEM_FILE="{tmp_path}/previous_problem"\n'
        "slot=test\n"
        "MAX_CYCLES=3\n"
        f'CLOCK_FILE="{clock_file}"\n'
        'sleep() { echo $(( $(cat "$CLOCK_FILE") + $1 )) > "$CLOCK_FILE"; }\n'
        'date() { cat "$CLOCK_FILE"; }\n'
        # healthz fails on every cycle, so PF_DEEP_CHECK_EVERY_N_CYCLES's
        # gate is never reached (each failing cycle continues before it) and
        # this never needs to distinguish it from the deep check's own URL.
        #
        # 000, curl's code for no HTTP response at all, because that is what a
        # dead forward actually produces: refused, nothing listening, or
        # accepted and never answered. A status the app chose would mean the
        # forward is carrying traffic and must not be restarted.
        'curl() { echo -n "000"; }\n'
        f'record_pf_event() {{ echo "$8" >> "{lag_capture}"; }}\n' + loop_body + "\n"
    )
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    lags = lag_capture.read_text().split()
    assert lags == ["15", "31", "47"], (
        "expected growing lag (15, then +16 per cycle for the interval plus the restart "
        f"branch's own 1s port-release pause), got {lags}"
    )
