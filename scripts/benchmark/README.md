# Running an RCI experiment

You have an idea — a prompt change, a model swap, a new node behind a flag —
and want to test it against a chosen set of failures, know the time and
dollar cost before you commit to it, and leave a record of what you ran and
why. This is the workflow for that. It does not require learning terraform.

The short version: **preview, then launch, then watch, then score.**

```bash
./scripts/benchmark/experiment.sh --problems sregym-lite --attempts 3
# ... read the preview, decide if the shape is worth it ...
./scripts/benchmark/launch.sh run
# ... wait ...
./scripts/benchmark/archive_run.sh <run-id>
python3 scripts/benchmark/run_stats.py /path/to/brain-go/sregym-results/<run-id>
```

The rest of this doc walks through each step, what the output means, and
where the gaps are today.

## 0. Iterate locally first, before spending anything on EC2

The EC2 path below is for a real paired comparison — a statistically
meaningful shape costs tens of dollars and an hour or more, and that is the
right price for a claim. It is the wrong price for "does my node produce
sane output at all," which is most of an iteration loop. For that, run one
problem on your own machine against a local `kind` cluster: no terraform,
no EC2, no bill.

This composes the same scripts the EC2 bootstrap calls, run by hand instead
of from `terraform/templates/bootstrap.sh.tftpl`:

```bash
# 1. A local emulated Kubernetes cluster (from this repo's root)
bash kind/setup_kind_cluster.sh x86      # or: arm

# 2. Bring up brain-go locally (runs `make up` in BRAIN_GO_DIR) and seed
#    dev credentials. Needs a brain-go checkout beside this one, and Bedrock
#    or Anthropic credentials already usable by that checkout's deploy/.env
#    -- the same thing any local brain-go dev work needs, nothing extra for
#    this. On a laptop there is no EC2 instance role to read credentials
#    from, so the script logs that and falls back to what deploy/.env
#    already has; it does not fail closed.
BRAIN_GO_DIR=/path/to/brain-go scripts/benchmark/setup_brain_go.sh

# 3. Seed one tenant (account, API key, session, MCP connections) against
#    the running stack
eval "$(scripts/benchmark/seed_slot.sh local-slot-0 9955)"

# 4. Run exactly one problem
uv run main.py --problem <problem_id> --agent brain_go \
  --model us.anthropic.claude-sonnet-4-6 \
  --judge-model bedrock/us.anthropic.claude-sonnet-5
```

`sregym/conductor/problem_sets.py`'s `sregym-lite` set (or
`sregym/conductor/problems/registry.py` for the full list) names valid ids
for `--problem`.

**Caveats, not polish-later gaps:**

- **Not every problem is supported on `kind`.** This repo's own top-level
  README says so; a hardware- or multi-node-class fault in particular may
  need the real cluster path (`kind/README.md` / `scripts/ansible/`), not
  this one.
- **One local attempt has no statistical power.** RCI's own measured
  discordant-verdict rate, within one run's 63 paired attempts, is 31.7%
  (see `scripts/benchmark/cost_model.py`'s `MCNEMAR_DISCORDANCE_RATE`). A
  local run answers "is this obviously broken," never "is this better" --
  that question needs the paired EC2 shape below, full stop.
- **This section documents composing existing building blocks, not a new
  script.** Every command above is verified against its own source and is
  exactly what the EC2 bootstrap already runs, just invoked by hand instead
  of from terraform; the specific sequencing here has not been run
  end-to-end as its own checked workflow. If you hit a step that does not
  behave as described, that is more likely a gap in this doc than in the
  scripts -- fix the doc in the same change you find it wrong in.

The next two rungs up, in order of cost: `./scripts/benchmark/launch.sh
smoke` (2 problems, one EC2 box, proves the harness against a real cluster
for the price of a coffee), then the full preview-and-launch flow below
(the paired comparison that actually resolves a claim).

## 1. Preview the cost, before anything deploys

```bash
./scripts/benchmark/experiment.sh \
  --sregym-ref my-branch \
  --brain-go-ref my/new-convergence-node \
  --problems sregym-lite \
  --attempts 3
```

Every flag is optional. `--sregym-ref`/`--brain-go-ref` default to
`origin/main`; `--problems` defaults to `sregym-lite`; `--attempts` defaults
to 1. Output looks like this:

```
experiment: sregym     563dd29b6b153d559f5622168792b73c8688e452
experiment: brain-go   f1270a67cf2f8650543c4dc78829ad34fea1091c
experiment: problems   21  (from 'sregym-lite')
experiment: shape        instance_count = ceil(21/3) = 7, depth 1, 3 attempt(s) each (runs_per_instance: 3)
experiment: resolution   63 paired attempts, resolves ~20pp at 80% power
experiment: wall clock   ~0.9 h  (setup ~30 min expected [9-68 min range, 14% per-slot retry chance], then 3 x 8.4 min)
experiment: ec2          ~$10.84  (7 instance(s) @ $1.6934/h x 0.9h, m8i.8xlarge, us-east-2)
experiment: bedrock      tokens ~199.8M, ~$166.00 (us.anthropic.claude-sonnet-4-5-20250929-v1:0)
```

(`terraform/variables.tf`'s `agent_model` default above is stale -- the model
this exact preview shape actually ran with was `us.anthropic.claude-sonnet-4-6`,
not the Sonnet 4.5 default shown here. Both now have a `terraform/rates.yml`
entry and print a real dollar figure; a model with no entry there still
prints "dollar cost unknown", which is what every id in this file read before
sregym#149 priced Sonnet 4.5 and 5.5.)

**Reading it:**

- `sregym`/`brain-go` — the exact commit each checkout resolved to. If you
  passed a ref, this is what gets pinned; if you did not, this is what
  `origin/main` happened to be at the moment you ran the preview, which can
  differ by the time you actually launch. Re-run the preview if time has
  passed.
- `problems` — how many ids your `--problems` spec resolved to, and the
  spec you gave it back so you can confirm it parsed what you meant.
- `shape` — how many EC2 instances this buys, at what concurrency
  (`runs_per_instance`, i.e. slots per box), and how many attempts each
  slot runs. `depth 1` means every problem gets its own slot: this is the
  *fastest* shape for the given concurrency, not necessarily the cheapest.
  `run.tfvars` deliberately trades wall clock for cost (4 boxes at depth 2
  instead of 7 at depth 1, for the same 21 problems) — this preview does
  not yet make that tradeoff for you; see "Known gaps" below.
- `resolution` — how many percentage points of verdict-flip-rate
  difference this many paired attempts could actually detect, at 80%
  statistical power. A cheap run tells you whether you broke something; it
  does not tell you whether you improved it unless this number is small
  enough to matter for the effect you expect. RCI is flaky by measurement,
  not by reputation (31.7% of one run's own 63 paired attempts disagreed on
  the diagnosis verdict), so this line exists to stop a 2-attempt smoke test
  from being read as a real comparison.
- `wall clock` and `ec2` — an *expected* time and cost, not a best case.
  Setup time is genuinely bimodal (a slot either deploys clean or hits a
  deploy-retry cycle, at a measured ~14% probability per slot post-#56-fix —
  down from ~50% pre-fix, see `scripts/benchmark/cost_model.py`'s module
  docstring for the full reasoning), so the number already accounts for the
  chance that at least one slot on a box retries.
- `bedrock` — a token point estimate (one clean run's own measurement, not a
  range), and a dollar cost. The cost stays "unknown" for a model with no
  entry in `terraform/rates.yml` — this is deliberate, never guess a rate
  that would then get quoted as a real cost — but a model that *is* priced
  there prints a real figure, keyed off `terraform/variables.tf`'s
  `agent_model` default.

Rejecting a bad `--problems` spec happens before any of this prints:

```bash
$ ./scripts/benchmark/experiment.sh --problems sregym-lite,not_a_real_problem --attempts 3
experiment: sregym     ...
experiment: brain-go   ...
experiment: unknown problem set or id: not_a_real_problem
```
(exit 1, nothing deployed)

## 2. Launch it

**This is the part that is not fully wired up yet.** `experiment.sh` only
previews today; it does not write terraform config or call `terraform apply`
itself (that wrapper is planned but not built — see "Known gaps"). To
actually run what you previewed:

**If your `--problems` spec is a single named set** (the common case:
`sregym-lite`, or a subset of it), copy the closest existing scenario and
adjust it:

```bash
cp terraform/run.tfvars terraform/my-experiment.local.tfvars
```

(`terraform/*.local.tfvars` is gitignored — this file is yours, not a
committed scenario.) Edit it to match your preview's inputs:

```hcl
sregym_ref        = "my-branch"                    # or leave unset for origin/main
brain_go_ref      = "my/new-convergence-node"      # or leave unset for origin/main
problem_set       = "sregym-lite"
n_attempts        = 3
runs_per_instance = 3                              # match --runs-per-instance if you set it
brain_env         = { CHAT_SERVICE_RCI_TEMPERATURE = "0.2" }  # your knob, if any
```

Then launch it the normal way:

```bash
./scripts/benchmark/launch.sh my-experiment.local
```

`launch.sh` re-resolves and re-validates the refs (the same three checks the
preview ran: does it resolve, was it pushed, is the worktree clean), prints
what it is about to bill you for, and asks before it applies. It also
writes a manifest recording every setting — including which ref actually
ran and whether the checkout that resolved it had uncommitted tracked
changes — so a run's
provenance never depends on remembering what you typed.

**If your `--problems` spec unions a named set with extra bare ids**
(e.g. `sregym-lite,kafka_producer_leak`), there is currently no terraform
equivalent: `problem_set` only accepts a single set name
(`^[A-Za-z0-9_-]+$`, no comma), and the separate `problems` list variable
only *narrows within* whatever `problem_set` already names — it cannot add
an id from outside that set. Widening `problem_set`'s validation to accept
`resolve_problems()`'s comma syntax, and wiring `run_shard.sh`'s existing
support for it through to a real launch, is tracked as follow-up work, not
done yet. For now, pick a single named set (or a subset within one via
`problems`) that already covers what you want to test.

## 3. Watch it

```bash
cd terraform
terraform output -raw watch      # an SSM command that tails one box's bootstrap log
terraform output -raw progress   # an S3 command that counts scored problem-runs so far
```

Look for these four lines early on — the first is emitted from inside
`run_shard.sh`, so seeing it proves the ref you pinned actually reached the
instance:

```
[bootstrap] shard 0 owns N problems
[slot 0] starting with N problems
[slot 0] MCP port-forward serving on ...
[slot 0] === <problem_id> ===
```

Each box self-terminates once its shard finishes, or at `max_runtime_hours`,
whichever comes first — nothing here is meant to outlive a run.

## 4. Archive and score

Results live in S3 only as long as the bucket does. Pull them down before
looking at anything:

```bash
./scripts/benchmark/archive_run.sh <run-id>
```

Safe to run repeatedly, including while a run is still going — it only
fetches what changed.

It writes to `sregym-results/<run-id>` and `sregym-run-logs/<run-id>` under a
sibling `brain-go` checkout when one exists, which is where every run so far
has been archived and what the `score_run.py`/`run_stats.py` paths below
assume. Set `SREGYM_ARCHIVE_ROOT` to put them somewhere else. It prints the
resolved path, so check that line rather than assuming.

**Headline numbers** (Table 3-style: diagnosis/mitigation/E2E, TTD/TTM,
tokens):

```bash
python3 scripts/benchmark/score_run.py /path/to/brain-go/sregym-results/<run-id>
```

This prints two versions of every rate: **strict** counts an attempt with no
verdict (because the harness killed it before the oracle ran) as a failure;
**graded** excludes it instead of penalizing it. Neither is "the" correct
number — say which one you're quoting, since they answer different
questions (strict: "how often did this system deliver a working result,
counting infrastructure failures against it"; graded: "of the attempts we
actually observed, how often did the agent reason correctly"). The gap
between them is usually small, but it can be large when timeouts are
common — the tool prints both specifically so nobody has to pick blind.

It also prints the judge's **continuous score** beside the binary rate.
`Diagnosis.success` is `composite >= 0.70`, where the composite is
`0.33*D1 + 0.33*D2 + 0.34*D3` from `rca_checklists.yaml`, so the binary
verdict says nothing about how far off a failed attempt was or which of the
three dimensions it failed on. A paired A/B of two brain-go refs, 63 attempts
per arm on the same 21 problems, scored 22/63 on the binary in both arms while
the continuous score moved: over the judge-scored attempts the mean composite
went 0.4195 to 0.5141, the zero-composite count 27/61 to 14/58, D1 0.443 to
0.575 and D3 0.432 to 0.575, with D2 about flat at 0.383 to 0.391. Use the
continuous numbers to see where a change acted; keep quoting the binary rate
for comparisons against earlier runs, which are all on record that way.

**Say which population you are quoting.** Those are the judge-scored figures,
the row the tool tells you to quote, and the tool prints them. The
all-attempts population includes the judge's own parse failures forced to 0.0
on every dimension, and the two arms had 2 and 5 of those, so the asymmetry
alone is enough to flip a dimension: D2 reads 0.370 to 0.360 there, a fall
instead of a rise. Only `--json` carries the all-attempts dimension means.

**More resolution is not significance.** Paired on problem over the
judge-scored attempts, that same A/B gives a mean per-problem difference of
+0.0795, sd 0.3497, t(20) = 1.042, 95% interval -0.0796 to +0.2387. Zero is
inside it. The continuous score showed where the change acted; it did not show
that the change worked.

The 95% interval the tool prints is a **one-run** interval, and putting two of
them side by side is not a comparison in either direction: overlapping
intervals do not show there is no difference, and separated ones do not show
there is. Comparing two runs is a separate job and `score_run.py` does not do
it, since every entry point here takes one run directory. The per-problem mean
vector a paired comparison needs is in `--json` under
`diagnosis_score.*.per_problem`.

That interval takes the **problem** as the unit, not the attempt, because three
attempts on one problem face the same fault and the same ground truth; it
assumes the per-problem means are independent and roughly normal, which the
attempt scores themselves are not. Read the histogram it prints, not only the
mean: each dimension is (yes answers)/3, so it takes four values and the
composite at most 64, and in every archived run checked 52% to 66% of
judge-scored attempts sit on exactly 0.0 or exactly 1.0.

**Per-category statistics, with honest uncertainty**:

```bash
python3 scripts/benchmark/run_stats.py /path/to/brain-go/sregym-results/<run-id>
```

Same numbers, but split by target application (hotel_reservation /
social_network / astronomy_shop — resolved from the actual registry code,
not guessed from problem-id spelling, since a third of ids carry no app
name in the string at all), and every number comes with a plain-language
range: "43.9% (likely 31.8-56.7)" instead of a bare percentage, and "too
few attempts to say" instead of a silent absence. A narrow range means the
number is solid; a wide one means there is not yet enough data to trust it
on its own — treat it as a rough estimate, not a precise score. That is a
fact about how much data exists, not a sign the run went wrong. Add
`--json` to either script for machine-readable output.

**The RCI readout** (inconclusive rate, the Observed signals section, the
new RCI counters, contamination, and the judge score restricted to the
inconclusive subset):

```bash
python3 scripts/benchmark/readout_run.py /path/to/brain-go/sregym-results/<run-id>
```

Reads `Diagnosis.submission` for the exact inconclusive lead sentence and
for an `## Observed signals` section within it, and aggregates
`custom_mcp_breaker_trips_total`, `rci_signal_agent_dispatched_total` and
`rci_symptom_improved_downgrade_total` from the same `*tokens*.prom` files
`score_run.py`'s token total reads, grouped by shard: within one shard the
counter only grows, so the max across everything scraped from that box is
exact, and shards sum because that is where one brain-go process ends and
the next begins. A counter absent from every file in the run prints as not
present rather than as zero, so a run that predates a counter is never
mistaken for one where it legitimately never fired.

It does not print a replan count: brain-go has no counter for a replan
round (`internal/chat/rci/aggregator.go`'s `State.ReplanCount` is in-memory
only), and it says so rather than guessing one from a proxy.

The port-forward restart half of the contamination line reads
`mcp_pf_restarts.jsonl`. `archive_run.sh` pulls it into
`sregym-run-logs/<run-id>/`; it needs its own `--include` there because the
file carries no `logs/` path segment and a `.jsonl` extension, so neither of
that sync's other two patterns reaches it. An archive taken before that
include was added will not have it, and the readout then reports the restart
count as absent rather than as zero. Fetch it into an older archive with:

```bash
aws s3 cp s3://<bucket>/<run-id>/ /path/to/brain-go/sregym-run-logs/<run-id>/ \
  --recursive --exclude "*" --include "*mcp_pf_restarts.jsonl"
```

**What the token scrape archives.** `scrape_tokens.sh` writes
`sregym-tokens.prom` into a shard's log tree from brain-go's two metrics
endpoints (chat on `:9091`, tworker on `:9092`). It runs from `sync_logs`, so
it fires on the 60s watcher and on every exit path, and the last write wins. It
keeps a prefix allowlist, today `llm_`, `custom_mcp_` and `rci_`, and drops
the rest of the payload, including the `go_*` and `process_*` runtime series
that would otherwise be repeated in every per-attempt archive. A brain-go
counter whose name falls outside those prefixes never reaches the archive, so
widen `KEEP` in that script in the same change that adds the counter. Every
run archived so far took all of its samples from tworker; the chat endpoint
answered each time and contributed none.

## Picking which problems to test

`--problems` (in `experiment.sh`) takes a comma-separated mix of named sets
and bare problem ids — union of everything named, deduplicated, and a hard
stop on any token it does not recognize (never a silent narrowing: a typo
must not shard to nothing and read like a clean short run):

```bash
--problems sregym-lite                                    # the standard 21
--problems network_policy_block,kafka_producer_leak        # two specific ids
--problems sregym-lite,kafka_producer_leak                 # the 21, plus one more
```

`sregym-lite` is the only named set that exists today. The full registry
(123 ids as of this writing — check `sregym/conductor/problem_sets.py`'s
`ALL_PROBLEM_IDS` for the current, test-pinned count) has no default set of
its own: nobody has reconstructed which 90 the SREGym paper actually used
(NOFireAI/brain-go#2221), so there is no "run everything" default and never
will be until that's resolved.

## Which stages a run grades

Diagnosis only, by default. brain-go does not do mitigation as a product and
no analysis reads the mitigation columns, while the stage was about half of a
run's tokens: 21.4M of run-20260921-1250's 60.15M.

`run_shard.sh` writes `sregym/conductor/tasklist.yml` at shard start, listing
every problem the shard owns with the stages from `SREGYM_STAGES`. Set it to
`diagnosis,mitigation` before sourcing `run_shard.sh` to get both back; any
other value hard-stops the shard. The file is generated rather than checked
in, because the conductor grades both stages for any problem the file does
not list, so it has to be derived from the same list the shard runs.

The run manifest records `stages`, `stages_source` and `tasklist_sha256`, so
an archived run says which stages it graded. Diagnosis figures from a
diagnosis-only run are comparable with earlier runs; the run total and
time-to-done are not, and `score_run.py` refuses to report a mitigation or
E2E figure when the stage did not run.

## Which arm a run is

`SREGYM_ARM`, set from a scenario's `arm` tfvars value. Three values, and the
default is what every archived run is.

`mcp` seeds four CUSTOM_MCP connections at SREGym's own MCP endpoints and
nothing else, so brain-go reaches the cluster only through them. Its native
connectors, its entity graph, change events and the knowledge path never run.

`native` seeds brain-go's own Kubernetes, Prometheus, Loki and Jaeger
connections against the same in-cluster backends, and installs the edge agent
into the slot's kind cluster so the entity graph populates and change events
are produced. Concretely, per slot:

- The kind cluster is created with its API server on the host's private
  address instead of loopback, because brain-go dials it from inside a
  container. kind puts that address in the API server certificate's SANs.
- A read-only ServiceAccount is created with get/list on exactly the objects
  brain-go's Kubernetes client reaches (core configmaps, events, namespaces,
  nodes, persistentvolumeclaims, pods, services; apps controllerrevisions,
  daemonsets, deployments, replicasets, statefulsets; batch jobs) and nothing
  on chaos-mesh's CRDs, and its token goes into the connection as a
  kubeconfig. The grant is derived from `git grep 'clientset\.' --
  internal/clients/kubernetes` in brain-go, not hand-picked; `native_arm.sh`
  carries the derivation and how to redo it. `batch/jobs` is the one whose
  absence is not survivable: `ListWorkloads` returns an error for the whole
  call if listing Jobs is refused, so `k8s_list_workloads` would fail every
  time.
- The kubernetes connection's config sets `configmap_values: "screened"`
  (brain-go#4052). Screened is what lets `k8s_describe` show a ConfigMap's
  data with secrets masked, rather than only key names and byte sizes, so the
  arm actually exercises that code path. A brain-go predating #4052 ignores
  the key: `New()` reads a fixed set of named config keys and never rejects
  an unrecognised one. `SREGYM_K8S_CONFIGMAP_VALUES`
  (`seed_native_connections.py`'s `resolve_configmap_values`) overrides the
  default the same way `SREGYM_BASELINE_SYNC_SECONDS` does: read only from
  the environment of the process that runs `seed_slot.sh` on the instance.
  `launch.sh` and `terraform/templates/bootstrap.sh.tftpl` do not forward it,
  so there is no tfvars field for it; changing it means exporting it by hand
  on the box before that slot's `seed_slot.sh` runs.
- Host port-forwards are kept alive to `prometheus-server`, `loki` and
  `jaeger-out` in the `observe` namespace. They are supervised rather than
  started once, because those services belong to the application and the
  conductor deploys it per problem.
- The edge agent is installed from the `nofire-edge` chart 0.5.2 with image
  `nofireai/edge:v3.6.0` (`SREGYM_EDGE_IMAGE` overrides it), publishing to
  `http://<private ip>:8000/api/edge` every 60s under a key of type `edge`
  minted through `POST /api/account/api-keys`. 60s is the agent's floor: it
  exits at startup on a shorter publish interval. Causal analysis, network
  observability and Sentry are off. The chart is fetched once per box and
  handed to each slot, since slots run concurrently and would otherwise race
  on one download path. The image is not: each kind node pulls it, because
  kind imports all platforms of a manifest list and a pulled image holds only
  one platform's blobs, so neither of kind's load paths can take it.
- The chart's informer list (`config.kube.resources`) replaces the agent's
  compiled default and leaves out `endpoints` and `networkpolicies`.
  `install-edge` passes a values file that repeats the chart's 15 entries and
  adds those two, and adds a get/list/watch rule on core `endpoints` through
  `rbac.extraRules`, which the chart's ClusterRole does not grant. Without
  that rule the agent skips the endpoints informer and `has_endpoints` is
  never set on a Service. The chart already grants `networkpolicies`.
- `helm --wait` is not the install's check. Chart 0.5.2 runs one replica with
  `maxUnavailable: 1`, and Helm 3 counts that ready with zero ready pods, so
  a crash-looping agent passes it. `install-edge` then waits for the pod to
  be Ready with no restarts (180s) and for brain-go to hold an entity for the
  slot's account (150s). On failure it writes the pods, the last 30 events
  and the agent's logs to the slot log and aborts the slot.
- The edge agent reaches brain-go's api at the host's private address without
  the harness arranging anything. brain-go's own `scripts/dev-env.sh` writes
  `BIND_ADDR=0.0.0.0` when it creates `deploy/.env`, so the whole stack is
  published on every interface on every box, in both arms. An earlier version
  of the setup script published the api a second time through a compose
  override; compose merges a service's ports rather than replacing them, so
  that bound the port twice and the api container would not start. Nothing
  outside the instance reaches those ports: `terraform/network.tf` has no
  inbound rule.
- `SREGYM_BASELINE_SYNC_SECONDS` is 150, the window the conductor already
  sleeps before injecting a fault.

`edge` is native minus the Kubernetes API connection: NOFire's production
posture, where brain-go holds no Kubernetes API token at all. It seeds the
same Prometheus, Loki and Jaeger connections against the same backends and
installs the same edge agent the same way, so the entity graph and change
events are produced exactly as they are under native. What it does not do:

- No kubeconfig is built, so no read-only ServiceAccount is created and the
  kind cluster keeps its committed config, API server on loopback. Native's
  private-address bind exists only so brain-go's Kubernetes connector can
  dial the API server; with no such connector there is nothing to dial it for.
- No kubernetes connection is seeded, so `configmap_values` is never set and
  `seed_native_connections.py` seeds three connections, not four.
- `setup_brain_go.sh` leaves `FEATURE_KUBERNETES_TOOLS_ENABLED` unset, the
  product default, so brain-go's `k8s_*` tool family never binds. No tool in
  that family resolves for this arm's investigations.

Both arms' pre-fault window, gate and post-fault wait are identical: the gate
reads `runs_edge_agent`, which is true for native and edge alike, because
neither telemetry reachability nor the entity graph depends on the Kubernetes
connection (`is_native` is a deprecated alias for the same function, kept so
an older import does not break). `arm-native.json`'s own `arm` key is always
"native" under either arm too -- it names the gate family that file belongs
to, not the resolved arm -- but its `run_arm` key carries the real value, so
the file says edge on an edge run even read on its own. The run manifest is
what records the arm itself the same way: `arm=edge` and
`kubernetes_connection=none`, against `arm=native`/`kubernetes_connection=native`
and `arm=mcp`/`kubernetes_connection=mcp`. The manifest's `kubernetes_tools_enabled`
is read back from the box's own `deploy/.env` rather than derived from the
arm, so a run that overrode it through `brain_env` still has it recorded
correctly; `setup_brain_go.sh` refuses to start a box where `brain_env` set it
true under the edge arm, since that is not what edge measures.

At the end of that window, and before the fault, the attempt is gated: the
three telemetry backends must answer and brain-go must hold at least one
entity. An attempt that fails either is aborted rather than scored, because an
attempt with an empty graph is the MCP arm's information set under this arm's
label.

Right after the fault is injected, the conductor holds the diagnosis stage
until brain-go has a publish from after the fault, capped at 120s
(`SREGYM_POST_FAULT_WAIT_SECONDS`, polled every
`SREGYM_POST_FAULT_POLL_SECONDS`, default 5). The wait ends on the first poll
that finds either a change event on the fault's component, or a change event
anywhere in the slot's cluster, both timestamped at least 5s after injection.
brain-go stamps every change event from one ingest with one time, taken when
it finishes the ingest (`emitChangeEvents` in
`internal/features/entitygraph/derive/reconcile/reconcile.go`), so the second
means an ingest finished after the fault. The 5s margin keeps out a publish
that left the agent just before the fault and finished its ingest just after.
brain-go exposes no last-publish time, so a publish that changed nothing
cannot be seen. Each request's timeout is the budget left, so a hung brain-go
cannot stretch the wait much past the cap, and a failed read is recorded with
its HTTP status (`last_error`, for example `HTTPError 401`). If
neither appears inside the cap, the attempt is not failed: diagnosis proceeds
and `arm-native.json` records `ended_by: "cap"`. Some faults change no object
the agent watches (chaos-mesh CPU or network stress touches no Kubernetes
object), so an
empty read at the cap does not show the agent is broken, and `install-edge`
has already proved it publishes. Failing there would spend all three deploy
attempts on such a problem and record it as `deploy_failed`.

### Recorded differences between the arms

Carried from sregym#118's description, with the post-fault wait added. Read a
paired result with these in mind rather than gating on them.

1. Cluster access is not equal. The MCP arm's kubectl ClusterRole grants
   secrets, `pods/exec`, `pods/log` and writes across most groups; the native
   arm's ServiceAccount grants get and list on 13 resource types and no
   secrets. Neither can read chaos-mesh.
2. The native arm moves which brain-go agent inspects the cluster:
   `signalSelectors` routes the Kubernetes tool family to the metrics node,
   where the MCP arm's `sregym-kubectl` sits in the state bucket.
3. Arm B gets 150s of settle before the fault (`SREGYM_BASELINE_SYNC_SECONDS`)
   that arm A does not. A flakier app at t=0 than at t=150 is a small
   confound in arm B's favour.
4. Arm B waits up to 120s after the fault, until brain-go holds a publish from
   after it, before diagnosis starts; arm A starts at once. The first publish
   after the fault lands 0s to 90s after it, because injection falls at a
   random point in the agent's cycle. Without the wait that much of every
   native investigation reads the pre-fault graph, and this arm is graded on
   whether the graph and its change events help. In production the agent
   publishes every 5m and an investigation starts when an alert fires,
   minutes after the fault, so the wait is closer to the product.
   Like item 3 it gives arm B extra time for the fault to develop, which
   favours arm B. The cap does not fail the attempt. `post_fault_wait` in
   `arm-native.json` records each wait, and the attempts with
   `ended_by: "cap"` count how often the graph showed nothing after a fault.
5. The edge arm shares items 3 and 4 with the native arm: same pre-fault
   settle, same post-fault wait. It does not share item 1, for the same
   reason it does not share item 2: there is no Kubernetes connection, so
   there is no cluster access to compare and no Kubernetes tool family for
   `signalSelectors` to route anywhere. Neither the MCP arm's kubectl
   ClusterRole nor the native arm's read-only ServiceAccount exists under
   edge, and no `k8s_*` tool binds to any node. The only cluster access
   brain-go has is whatever the edge agent's own entity graph carries.

The manifest records `arm`, `kubernetes_connection`, `kubernetes_tools_enabled`,
`edge_image` and `edge_chart_version`, and
`seeded_connection_buckets`/`seeded_connection_signals` describe whichever
set the run actually seeded: three entries under edge (prometheus, jaeger,
loki), four under native (those three plus kubernetes), and the four
CUSTOM_MCP endpoints under mcp. `kubernetes_connection` is `mcp`, `native`,
or `none` (edge), naming which connection, if any, gives brain-go cluster
access. `kubernetes_tools_enabled` is `true` or `false`, read back from the
box's own `deploy/.env` rather than derived from the arm, so an override
through `brain_env` is reflected rather than guessed at; `setup_brain_go.sh`
refuses to boot a box where that override is true under the edge arm.
`seeded_configmap_values` (native arm only, empty under mcp
and edge) records the kubernetes connection's `configmap_values` the same
way, so an archived run says what it actually seeded even when
`SREGYM_K8S_CONFIGMAP_VALUES` was overridden by hand on the box. Per attempt,
`arm-native.json` (the gate family's name, shared by native and edge; its own
`run_arm` key carries the resolved arm) lands beside `fault-spec.json` with
the entity counts by type, whether a change event exists on the mutated
component, and `post_fault_wait`: how long the
post-fault wait took (`waited_seconds`), injection to the poll that ended it
(`lag_seconds`, null at the cap), and what ended it (`ended_by`:
`change_event`, `publish` or `cap`). `archive_run.sh` already ships `*.json`, so
it comes back with everything else.

Smoke it on one problem before a paired run:

```bash
./scripts/benchmark/launch.sh smoke-native
./scripts/benchmark/launch.sh smoke-edge
```

## Running your own branch

`--sregym-ref`/`--brain-go-ref` (or `sregym_ref`/`brain_go_ref` directly in
a scenario's tfvars) accept a branch, a tag, or a full SHA. Left unset,
both default to `origin/main`, refetched fresh on every launch. Three things
have to be true before a ref gets pinned, checked in this order, each named
because each fails silently otherwise:

1. It has to resolve to a commit at all.
2. That commit has to be reachable on `origin` — the fleet clones from the
   remote, so a commit that only exists in your local worktree is invisible
   to it, and pinning it anyway would boot three instances onto code nobody
   can actually fetch.
3. The worktree that resolved it has to be clean — a run whose code exists
   nowhere in the form it ran is unreproducible.

See `terraform/README.md`'s "Running your own branch" section for the full
detail on how `launch.sh` implements this; `experiment.sh`'s preview runs
the identical three checks from the command line.

## Known gaps

- **The terraform wrapper.** `experiment.sh` computes and prints; it does
  not write `terraform/*.local.tfvars` or call `launch.sh` for you yet.
  Step 2 above is the manual bridge until that lands.
- **Composed `--problems` specs don't reach terraform.** `resolve_problems()`
  supports unioning a named set with extra bare ids; `problem_set`'s
  terraform variable does not accept the comma syntax that would carry that
  union through to a real launch (see step 2). Single named sets, and
  subsets within one via the `problems` list variable, work fully today.
- **The preview always assumes depth 1** (every problem gets its own
  slot — the fastest shape for a given concurrency). Trading wall clock for
  fewer, deeper boxes, the way `run.tfvars` does deliberately, is a manual
  override today, not something the preview computes for you.
- **The Bedrock rate is unknown for a model with no entry in
  `terraform/rates.yml`.** The file exists now, with real, sourced rates for
  the models this repo has actually run plus a few added by inference (see
  the file's own header on which is which). A preview or scored run using an
  unpriced model still shows tokens only, and says the cost is unknown rather
  than guessing.

For the deeper why behind any of these numbers — the retry-rate
measurement, the order-statistic reasoning, the phased plan this all comes
from — see `experiment-bench-plan.md` in the brain-go checkout.
