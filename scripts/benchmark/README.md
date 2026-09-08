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
/path/to/brain-go/sregym-results/archive_run.sh <run-id>
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
  flip rate between two runs of the *same* 21 problems is 48% (see Day 3's
  cost model reasoning in `experiment-bench-plan.md`). A local run answers
  "is this obviously broken," never "is this better" -- that question needs
  the paired EC2 shape below, full stop.
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
experiment: resolution   63 paired attempts, resolves ~24pp at 80% power
experiment: wall clock   ~2.2 h  (setup ~56 min expected [9-68 min range, 50% per-slot retry chance], then 3 x 24.8 min)
experiment: ec2          ~$25.79  (7 instance(s) @ $1.6934/h x 2.2h, m8i.8xlarge, us-east-2)
experiment: bedrock      tokens ~100.8-138.6M (63 attempts x ~1.6-2.2M), dollar cost unknown (no rate configured; see terraform/rates.yml)
```

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
  not by reputation (48% verdict flip between two runs of the same 21
  problems), so this line exists to stop a 2-attempt smoke test from being
  read as a real comparison.
- `wall clock` and `ec2` — an *expected* time and cost, not a best case.
  Setup time is genuinely bimodal (a slot either deploys clean or hits a
  deploy-retry cycle, at a measured ~50% probability per slot, holding
  steady across two very different box sizes — see
  `scripts/benchmark/cost_model.py`'s module docstring for the full
  reasoning), so the number already accounts for the chance that at least
  one slot on a box retries.
- `bedrock` — a token range, and a dollar cost that stays "unknown" until
  someone puts a real committed rate in `terraform/rates.yml`. This is
  deliberate: never guess a rate that would then get quoted as a real cost.

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
ran and whether the checkout that resolved it was dirty — so a run's
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
/path/to/brain-go/sregym-results/archive_run.sh <run-id>
```

Safe to run repeatedly, including while a run is still going — it only
fetches what changed.

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
- **The Bedrock rate is unknown** until `terraform/rates.yml` exists with a
  real, sourced number in it. Until then, every cost preview shows tokens
  and EC2 dollars only, and says so.

For the deeper why behind any of these numbers — the retry-rate
measurement, the order-statistic reasoning, the phased plan this all comes
from — see `experiment-bench-plan.md` in the brain-go checkout.
