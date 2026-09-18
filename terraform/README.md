# Benchmark fleet

Ephemeral EC2 for running the SREGym suite against brain-go. Spawn, run,
terminate. Nothing here is meant to outlive a run.

Tracked by NOFireAI/brain-go#2218, under NOFireAI/engineering#1411.

## Why it lives here

It provisions for this harness and follows its shape, so it sits beside it
rather than in `NOFireAI/terraform`, which holds standing environments. Revisit
if a dedicated benchmark repo appears, or once we run slices of the suite rather
than whole sweeps.

## Prerequisites

The `region` default is `us-east-2`, matching where every run so far has gone
and where `sregym-results/archive_run.sh` in the brain-go checkout looks. It
used to default to `us-east-1`, which meant omitting the var put the bucket in
a region the archiver could not find, and the symptom was "no bucket matching"
after the run had already been billed.

One-time, per account:

```bash
# A token that can clone both this private mirror and NOFireAI/brain-go
aws ssm put-parameter --region us-east-1 \
  --name /sregym/benchmark/github-token \
  --type SecureString --value "ghp_..."
```

Bedrock model access must be enabled in the region for both the agent and judge
models.

By default this config creates and destroys an instance role per run
(`iam.tf`), which needs a wide IAM grant on whoever runs `apply`/`destroy`:
`CreateRole`, `PutRolePolicy`, `AttachRolePolicy`,
`ListInstanceProfilesForRole` (required at delete, not just create), and more
-- see `iam.tf`'s own comment for the full list and why. If that grant isn't
available, set `-var instance_profile_name=<name>` instead: have an admin
create one static role and instance profile by hand, using the policy
document in `iam.tf` (`data.aws_iam_policy_document.bench`) verbatim, and the
only permission the caller then needs is `iam:PassRole` scoped to that one
role's ARN.

## Running

Two named scenarios cover almost every launch:

```bash
./scripts/benchmark/launch.sh smoke     # 2 problems, proves the harness works
./scripts/benchmark/launch.sh run       # 21 x 3 = 63, the scored run
./scripts/benchmark/launch.sh smoke --plan-only
```

`launch.sh` reads `terraform/<name>.tfvars`, pins both refs to the current
`origin/main` of each checkout, prints what it is about to bill you for, checks
the user_data size before planning rather than after applying, and asks before
it applies.

Pinning is the part worth not skipping by hand. `sregym_ref` and `brain_go_ref`
both default to `main`, and `main` means whatever it happens to be when each of
three instances boots. Two runs a week apart, or two instances minutes apart,
can then be running different code with no record of which. The manifest records
what was pinned.

### Running your own branch

Set `sregym_ref` or `brain_go_ref` in a scenario's tfvars to a branch, a tag,
or a full SHA, instead of leaving it unset, to run an engineer's own code
instead of `origin/main`. `launch.sh` resolves whatever is there to a full SHA
before it reaches terraform, and refuses to proceed, before any AWS call, if:

- the ref does not resolve to a commit at all
- the commit resolves locally but was never pushed (the fleet clones from the
  remote, so a local-only commit is invisible to it)
- the checkout that resolved it has an uncommitted change to a tracked file (a
  run whose code exists nowhere on disk in the form it ran is unreproducible).
  Untracked files do not count: the fleet clones the commit from the remote, so
  a file that is in no commit cannot reach it

`scripts/benchmark/experiment.sh` exercises the same resolution and the same
three checks from the command line, `--sregym-ref` / `--brain-go-ref`. It
also takes `--problems` (a comma-separated mix of named sets and bare ids,
resolved through `resolve_problems()`) and `--attempts`, and prints a cost
preview before anything gets deployed: the shape (`instance_count` at
`--runs-per-instance` slots each, depth 1), the expected wall clock (an
order-statistic setup estimate, not a plain average, since a box's own
setup time is set by its slowest slot), the expected EC2 cost, a Bedrock
token estimate (dollar cost printed only once a rate exists in
`terraform/rates.yml`, never guessed), and the statistical resolution that
shape buys at 80% power. The arithmetic lives in
`scripts/benchmark/cost_model.py`, unit-tested in
`tests/benchmark/test_cost_model.py`. Still no terraform invocation; that is
the wrapper still to come.

Run the smoke scenario before any scored run. It costs two problems and answers
the one question the local gates cannot: whether a real instance gets through
boot, cluster creation, brain-go setup, the port-forward, tenant seeding, and
into a problem. Look for these four lines, the first of which is emitted from
inside `run_shard.sh` and so proves the source handoff worked:

```
[bootstrap] shard 0 owns 2 problems
[slot 0] starting with 2 problems
[slot 0] MCP port-forward serving on ...
[slot 0] === network_policy_block ===
```

`run.tfvars` fixes 21 x 3 deliberately. `run-20260831-1752` was that shape, so
keeping it identical makes the next run a paired comparison and isolates what
changed in between; 63 paired problem-runs resolves about 23pp at 80% power.
Changing `n_attempts` or the problem set costs that comparison.

It also runs those 63 at full width: `instance_count x runs_per_instance` is
21, one slot per problem, so each slot is three attempts deep instead of seven.
Width is the only lever on wall clock, because attempts are serial inside a
slot. Going past 21 needs more problems, not more slots: a problem maps to
exactly one slot, and the extras log `drew no problems, skipping`. A test pins
both the width and the even split, since neither fails loudly.

Measured per slot on `run-20260902-0606`: a 24.8 minute median attempt cycle,
and a bimodal setup before the first attempt, four slots at 16 to 21 minutes
and five at 56 to 58, median 55.7. Nine slots up to nine attempts deep finished
in about 4.9 hours, which is the slowest slot; the median slot finished in 3.2.
At 21 slots the depth is 3, which puts the slowest slot near 2.2 hours.

Quote the slowest slot, not the median one. The run ends when the last slot
does, and setup is roughly half of that. The tail is a first deploy that fails
before fault injection and restarts the problem, costing the slot its deploy
twice; #53 has the evidence and the counts.

To narrow any run, set `problems` to a list of ids from the set. Ids are checked
on the instance and an unknown one is a hard stop, because a typo would
otherwise shard to nothing and the instance would terminate looking like a clean
short run.

The raw form still works and is what the scenarios expand to:

```bash
terraform init

# One box, one slot, one attempt per problem, no noise. The first sweep.
terraform apply -var instance_count=1

# The paper's protocol on Lite: 21 problems x 3 attempts x 2 noise conditions.
terraform apply -var instance_count=2 -var n_attempts=3 -var noise=true

# The target shape once sizing is measured: few large instances, many
# concurrent slots each, rather than many instances running one apiece.
# See NOFireAI/brain-go#2218's fleet-shape table for where these numbers
# come from.
terraform apply -var instance_count=3 -var runs_per_instance=12

# Set brain-go's own tuning knobs. Anything it reads from the environment
# works here; the map is merged over the derived defaults, so setting one key
# does not drop CHAT_SERVICE_TURN_TIMEOUT.
#
# One apply is one setting, not a sweep: setup_brain_go.sh runs once per
# instance and every slot on it shares that stack. A sweep is N applies.
terraform apply -var 'brain_env={CHAT_SERVICE_RCI_TEMPERATURE="0.5"}'

# Ablate a behaviour rather than tune a number.
terraform apply -var 'brain_env={CHAT_SERVICE_RCI_INJECT_TIME_WINDOWS="false"}'
```

`CHAT_SERVICE_TURN_TIMEOUT` is set for every run whether you ask for it or
not, at twice `agent_timeout_seconds`. brain-go bounds a chat turn at 15
minutes by default, which is an interactive-chat ceiling with no counterpart
here: when it fires during diagnosis the driver submits an empty answer and
never attempts mitigation, so the problem scores zero on both dimensions
regardless of how the investigation was going. It cost 3 problems in the first
full run and 2 in the second. Deriving it from the harness budget keeps the
harness timeout the only binding cap.

One ceiling that is not derived: brain-go's own HTTP write timeout is 90
minutes and severs the connection whatever the workflow does, so an
`agent_timeout_seconds` above roughly 45m needs that raised too
(`cmd/brain/internal/chatsvc/wiring.go`).

Raising the turn timeout also lengthens how long an abandoned turn survives.
`main.py` kills the agent at `agent_timeout` and that budget covers both phases
of an attempt, but brain-go does not stop: it logs that the workflow continues
when the client disconnects. Under the old 15-minute cap an orphaned turn died
within 15 minutes; at twice the harness budget it can keep spending Bedrock
tokens and tworker capacity for up to that long while the next problem is
already running, on a stack all slots on the box share. Still a clear
improvement, since problems that used to be cut off now reach mitigation, but
it is a real cost of the change rather than a free one.

The applied keys are logged by `setup_brain_go.sh` as a single
`run-configured keys:` line, so the configuration is recoverable from the
bootstrap log after the instance self-terminates. Values are not logged: the
mechanism is generic and a secret passed through it must not ship to S3.

Watch it, and check progress:

```bash
terraform output -raw watch      # an SSM command that tails the bootstrap log
terraform output -raw progress   # an S3 command that counts scored problem-runs
```

`-raw` is not optional for `progress`. Without it terraform prints the value as
a quoted string and doubles the backslash in the regex, so `\.` becomes `\\.`:
in ERE that means a literal backslash followed by any character, no S3 key
contains a backslash, and the command reports 0 forever. It fails silently,
with a plausible-looking number, which is the same way the count it replaced
was wrong.

## Scoring a run

```bash
scripts/benchmark/score_run.py sregym-results/<run-id>        # the six metrics
scripts/benchmark/score_run.py sregym-results/<run-id> --json
```

It prints Diag, Mitig and E2E each under two denominators, TTD and TTM each
under two timing conventions, and tokens. The reasoning for each pair lives in
the script's own module docstring rather than here, so there is one copy to keep
true: run `python3 -c "import runpy; runpy.run_path('scripts/benchmark/score_run.py')" --help`
or just read the file.

The short version, because it decides what you are allowed to quote: a missing
`Mitigation.success` means the harness killed the agent before the oracle ran,
not that the agent failed to mitigate, and on `run-20260831-1752` the 18 absent
verdicts are exactly the 18 timed-out attempts. That is 23/63 = 36.5% or
23/45 = 51.1% for the same run. The script prints both and declines to choose;
say which one you are quoting. `visualizer/queries.py` maps an empty string to
`False`, so it applies the strict reading silently. See #39.

Instances terminate themselves when their shard finishes. `terraform destroy`
then removes the security group and the role (unless `instance_profile_name`
pointed at a pre-existing one, which this never touches). It does not need to
remove the instances, and if you run it early it will.

It will **not** remove the results bucket, and will fail rather than try, since
`force_destroy = false`. That is deliberate: one bucket holds every run under
its own `<run-id>/` prefix (#48), so destroying it would take every previous
run's results with it. Nothing in the bucket bills meaningfully and the
lifecycle rule expires noncurrent versions after 30 days, so leaving it is the
normal state. Destroy is housekeeping for the security group, not the thing
that ends a run.

To get a clean destroy, empty the bucket first and deliberately, having
archived what you want (`sregym-results/archive_run.sh` in the brain-go
checkout).

## How the pieces fit

**Sharding is static, twice over**: problem index modulo `instance_count`
picks this instance's problems, then that list is split again by index modulo
`runs_per_instance` across the instance's own concurrent slots. No queue and
no coordinator at either level, which is the right trade for 90 to 540 runs
where the only failure mode that matters is losing an instance.

**Spot, and losing one is cheap.** Each slot writes its own results tree
(`slots/<slot>/results/`, since two slots sharing one directory would
interleave into the same SQLite trace store) and its own `main.py --resume`
counts attempts per `problem_id` there, skipping any problem that already has
`n_attempts` rows. The bootstrap syncs the whole `slots/` tree to S3 after
every problem and pulls it all back on boot, so a replacement instance
resumes every slot where the dead instance stopped. Losing a box costs each
slot its own in-flight problem, not the whole shard.

To replace a killed instance, `terraform apply` again.

**It terminates itself twice over.** Once when the shard completes, and again if
`max_runtime_hours` elapses. Both matter: an `m8i.8xlarge` is about $1.69/hour
on demand in us-east-2, and the failure that actually costs money is a
forgotten box, not a failed run.

**Access is SSM Session Manager**, so the security group has no inbound rules at
all. No keys, no bastion, and sessions are audited.

## Choices worth knowing

**gp3, 500 GB, for the IOPS rather than the capacity.** gp2 scales IOPS with size
at 3/GB, so 200 GB of gp2 is 600 IOPS and throttles concurrent image pulls. gp3
gives 3000 IOPS and 125 MB/s regardless of size, tunable independently. Sized
generously because every kind node keeps its own image cache: ENOSPC is what
kills the smoke-test CI today, and it surfaces as `ErrImagePull` rather than as a
disk error.

`run.tfvars` raises `disk_throughput_mbps` to 500 for the same reason it raised
the slot count. Every slot on a box shares that baseline, image pulls are most
of the ~35 GB each one writes, and three slots never tested it. Capacity is not
the constraint at seven slots: ~245 GB of a 484 GB root, against a measured
ceiling of roughly 13 slots per box (#49).

**The judge model must differ from the agent model**, enforced by a
precondition. A model grading its own output is not a measurement. Note that two
*cheap* judges agreeing is also weak evidence: SREGym's own inter-rater table has
GPT-5.4 and K2.5 agreeing with each other at kappa 0.94 while agreeing with a
human expert at only 0.70 and 0.76.

**Every `SREGYM_*` intervention is off.** They exist to work around a 24 GB ARM
laptop and they are why engineering#799's numbers cannot be read. `SETTLE_SECONDS`
defaults to 180, so the bootstrap sets it to `0` explicitly rather than leaving it
unset.

**`runs_per_instance` can now be raised past 1.** Each slot gets its own kind
cluster, its own API/MCP/k8s-proxy host ports
(`scripts/benchmark/run_slot_env.sh`), and its own brain-go tenant
(`scripts/benchmark/seed_slot.sh`) seeded against the one brain-go stack the
instance shares across all its slots. One cluster per run is still a hard
constraint; one host per run is not.

Sizing is now measured rather than estimated. On `r7a.24xlarge` at three slots:
2.6% of 96 vCPU, 3.0% of 739 GB, 105 GB of a 484 GB root, which is ~0.83 cores,
~7.3 GB and ~35 GB per slot. Disk was the only binding resource. That is what
moved the default to `m8i.8xlarge` and the scored run to seven slots; see
`variables.tf` for why `m8i` and not `m8id`.

Those are point-in-time readings taken on a live box, not time averages. No
CloudWatch agent is installed, so CPU is the only one recoverable after a run
and memory and disk are not recoverable at all.

## brain-go

`scripts/benchmark/setup_brain_go.sh` stands up brain-go on the box, seeds the
account and the four CUSTOM_MCP connection rows, and mints the CLI key
(NOFireAI/brain-go#2219). The bootstrap clones brain-go at `brain_go_ref`
(default `main`) with the same token as the sregym checkout, then runs the
script; a missing or failed clone exits loudly rather than burning an hour
looking healthy. `BRAIN_API_URL` defaults to `localhost:8001`, so brain-go runs
on the box beside the harness rather than centrally.

The MCP server is addressed by the instance's RFC1918 address with
`allow_private_endpoint` set per connection row, because brain-go's SSRF guard
blocks loopback with no flag that lifts it, and that allowance is honoured
only on the in-process CUSTOM_MCP path, not the Temporal MCP activity path.

## Cost

Compute is the cheap half, and cheaper since the default was resized.
`m8i.8xlarge` is about $1.69/hour on demand in us-east-2, against $7.30 for the
`r7a.24xlarge` it replaced. Three boxes for a ~2.2 hour scored run is about $11,
where the same run at the old size and width was nearer $110. A full 540-run
protocol is still a few tens of dollars of compute.

The token spend is the number to watch: `run-20260902-0606` billed 96.4M tokens
across the 60 attempts it actually scored, most of it cache reads. `scripts/benchmark/score_run.py`
reports the breakdown, and what to capture is in NOFireAI/brain-go#2219.
