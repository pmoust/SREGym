# The scored run: 21 sregym-lite problems x 3 attempts = 63 problem-runs.
#
# The problem shape is fixed deliberately and has not changed: 21 x 3, the same
# as run-20260831-1752 and run-20260902-0606. Changing n_attempts or the
# problem set costs the paired comparison, which is most of the value.
#
# 63 paired problem-runs resolves about 23pp at 80% power. It is the smallest
# honest run, not a precise one: a 10 to 15pp claim needs 150 to 336, i.e. 7 to
# 16 attempts on Lite or the full 90-problem suite x 3.
#
# problems is left unset, which means the whole set. A narrowed run is not
# comparable with this one.
#
# Two things below do differ from run-20260902-0606, and they are not the same
# kind of change. The slot count is a wall-clock choice: the same 63
# problem-runs, each still one investigation in its own cluster against its own
# tenant, so it does not move a score. agent_timeout_seconds does move scores,
# on purpose, and its own note says why. Read the next run as paired with
# 20260902 on everything except the cap.
#
# This run is also not paired with run-20260908-0739-4fc0249e, the most recent
# scored run at this same problem shape and cap. Three things changed on
# purpose: the step budget (see brain_env below), the incident window (see
# clients/brain_go/driver.py), and brain_go_ref. Accepted deliberately rather
# than deferred, because 18 of that run's 63 attempts never produced a
# reasoned answer at all (#90), so comparability to it was comparability to
# a broken measurement, not something worth preserving.

# Four boxes at three slots: 12 slots for 21 problems, two problems deep.
#
# Not full width, and not by preference. run-20260903-0822 tried three boxes at
# seven slots and lost all 63 problem-runs: 61 deploy failures, 32 in the
# openebs namespace and 27 in observe, 39 of them ImagePullBackOff, and not one
# problem reached its agent. Seven was interpolated from #49's per-slot figures,
# which were taken at THREE slots on an r7a.24xlarge with three times the CPU
# and six times the memory of this box. On m8i.8xlarge the only density with
# evidence behind it is three, and even that is inherited from the larger
# machine (#73 measures it properly).
#
# So three slots per box, and the box count picked from wall clock rather than
# width. Measured per slot on run-20260902-0606: a 24.8 min median attempt
# cycle, and ~20 min of setup on a clean deploy. The deepest slot sets the run:
#
#   boxes  slots  shard split   deepest slot  cycles   wall    ec2
#     3      9    7/7/7         3 problems      9      4.1h   $20.59
#     4     12    6/5/5/5       2 problems      6      2.8h   $19.06
#     7     21    3 each        1 problem       3      1.6h   $18.65
#
# EC2 cost is flat across all three, because box count trades rate against
# duration. So the choice is blast radius against wall clock, and four is 1.3
# hours faster than three for a dollar less and one extra machine. Seven buys
# another 1.2 hours for four more machines and is the shape that just failed.
#
# The wall and ec2 columns are stale. They follow from the 24.8 min median
# above, which was measured under the old 1800s cap with 11 of that run's 60
# attempts censored at it; the censored attempts are most of what put the
# median there. Kept because the ranking of the three shapes still holds and
# four boxes still follows from it.
#
# run-20260908-0739 ran this exact shape at 2400s and finished 63 of 63
# attempts in 78 minutes: first fault 07:48:38, last log line 08:50:38, so
# about 60 min of agent time over the deepest shard's 6 attempts, near 10 min
# each, plus about 18 min of boot and setup. Expect 80 to 90 minutes and
# around $9 of EC2, not 2.8h and $19.
#
# A short attempt is not an efficient one. The step budget, not the wall
# clock, is what ended those attempts: TTM ran 240/436/871s against a 2400s
# cap, and 18 of 63 exhausted CHAT_SERVICE_RCI_RECURSION_LIMIT and submitted
# boilerplate. That budget is raised to 200 in brain_env below, so attempts
# should get longer here, not shorter.
#
# 21 does not divide by 4, and here that costs nothing. Both splits are static
# modulo (problem % shard_count, then index % runs_per_instance), so the shards
# are 6/5/5/5, and across three slots a shard of six goes 2/2/2 while a shard of
# five goes 2/2/1. The deepest slot is two either way: the extra problem lands
# where there was room. Uneven sharding only costs wall clock when
# ceil(max_shard / slots) exceeds ceil(min_shard / slots), which at 4 x 3 it
# does not. A test checks the depths rather than divisibility, because
# divisibility is a proxy that is wrong in exactly this case.
#
# Setup is roughly a third of the projected wall clock, and #53's silently
# retried first deploy is what makes it bimodal. That scales with slot count,
# so twelve slots is twelve chances to draw it against nine.
#
# Pinned rather than left to the variable default, which was us-east-1 until
# recently: omitting it put the results bucket in a region archive_run.sh
# does not look in, and the symptom was "no bucket matching" after the run
# had already been billed. Every run to date has been us-east-2.
region = "us-east-2"

# READ THE WORKSPACE NOTE BELOW BEFORE PLANNING WITH THIS SET.
#
# s3.tf gates all five of its bucket resources on this variable being empty.
# Which behaviour that gives depends entirely on whether the workspace you
# plan in already manages the bucket, and the two cases are opposite:
#
#   - A workspace whose state does NOT hold the bucket (arm-control,
#     arm-treat, any fresh worktree): leaving this empty means CreateBucket,
#     which fails with 409 BucketAlreadyOwnedByYou after the security group
#     has already been created. Setting it is what makes the launch work.
#   - The primary checkout's default workspace, whose state DOES hold all
#     five resources (terraform.tfstate, serial 320, verified 2026-09-18):
#     setting this takes every one of them to count = 0, which plans five
#     destroys. force_destroy is false so the bucket itself survives on
#     BucketNotEmpty, but the public-access block, the AES256 default, the
#     versioning and the lifecycle expiry are destroyed for real before that
#     failure. Removing a public-access block is a security regression, not a
#     bookkeeping one.
#
# So: plan the scored run from an arm workspace or a fresh worktree, never
# from the default workspace with this line present. Every scored run to date
# did exactly that, passing this value as -var from arm-control or arm-treat,
# which is why the committed scenario never carried it and has never been
# launchable on its own.
#
# variables.tf says to set this only for a bucket that already carries the
# four settings s3.tf would otherwise apply. Checked against the live bucket
# on 2026-09-18: BlockPublicAcls, IgnorePublicAcls, BlockPublicPolicy and
# RestrictPublicBuckets all true, SSE AES256, versioning Enabled, lifecycle
# rule expire-noncurrent Enabled. All four are present.
#
# The name is derived from the account id the same way s3.tf derives it, so
# this is the same bucket, not a second one.
results_bucket = "sregym-bench-results-819709950457"

instance_count    = 4
runs_per_instance = 3
n_attempts        = 3

# Pinned to what production actually runs, rather than left to
# variables.tf's default (sonnet-4-5): production moved to Sonnet 4.6, and
# a benchmark measuring a model nobody is serving answers the wrong
# question. This is a deliberate break from run-20260829-1047 and
# run-20260902-0606, both scored on 4.5 -- read this run as a new baseline
# on the model dimension, not as paired with either of them on that axis.
# judge_model is left at variables.tf's default (bedrock/us.anthropic.
# claude-sonnet-5): already the intended judge, nothing to override.
agent_model = "us.anthropic.claude-sonnet-4-6"

# Explicit, rather than inheriting the variables.tf default of 12. The deepest
# slot is two problems at three attempts, so six cycles at the 2400s cap plus
# setup needs about 3.3 hours; six leaves room for a slow run without leaving a
# 12-hour tail. At four boxes that default would expose $81 of idle billing if
# self-termination ever failed to fire, for no reason anyone chose.
max_runtime_hours = 6

# 2400s, up from the paper's 1800s, because 1800s was censoring the run rather
# than bounding it. 11 of the 60 scored attempts in run-20260902-0606 were
# killed at the cap, and those 11 are the entire reason 11 mitigation verdicts
# and 2 diagnosis verdicts are missing: 9 died mid-mitigation and 2 mid-
# diagnosis. A missing verdict is not a failed one, but strict scoring counts
# it as a failure, so the cap was reading as a reasoning error.
#
# This does move what the run measures, which the paired-shape note above
# otherwise argues against. It is the point: an uncensored 63 is the
# comparable number, and the censored one was never the paper's protocol in
# the sense that mattered.
#
# 2400 and not more because the bootstrap derives
# CHAT_SERVICE_TURN_TIMEOUT = 2 x this, and brain-go severs the connection at
# its 90 minute HTTP write timeout whatever the turn timeout says
# (cmd/brain/internal/chatsvc/wiring.go). 2400s puts the derived turn timeout
# at 80 minutes, under that ceiling. Above 2700s the ceiling binds first.
#
# Left unchanged for #90 despite the step-budget increase below: measured
# across run-20260908-0739-4fc0249e's 63 attempts, TTL (time to diagnosis) was
# min 114s / median 268s / max 420s, and TTM (time to mitigation) was min
# 240s / median 436s / max 871s. No attempt came within 1,500s of this cap, so
# it was never the binding constraint and raising it further buys nothing.
agent_timeout_seconds = 2400

# What actually ends an investigation is brain-go's own
# CHAT_SERVICE_RCI_RECURSION_LIMIT, default 100, which no run has overridden
# before this one. 200 is the issue's suggested figure (#90): the attempts
# that spend the whole default budget in the planner's tool loop are the
# ones that end with no diagnosis at all (NOFireAI/brain-go#2848), and this
# alone only delays that collapse rather than fixing it, so it is worth
# measuring in the same run as #2848 lands, not before.
#
# This is a brain-go knob, not a harness one, so it goes through brain_env
# rather than a tfvars value of its own; agent_timeout_seconds above is the
# harness's separate wall-clock cap and stays put.
brain_env = {
  CHAT_SERVICE_RCI_RECURSION_LIMIT = "200"
}

# The incident window's own width no longer derives from agent_timeout_seconds
# (clients/brain_go/driver.py, BRAIN_INCIDENT_WINDOW_SECONDS, default 480s).
# It used to: a 2400s agent_timeout_seconds produced a 40-minute window, and
# brain-go derives baselineStart = start - (end-start)*3, landing the baseline
# 2 hours before the fault, before the kind cluster existed, so every baseline
# comparison came back empty. The two settings were never independently
# adjustable through tfvars alone -- driver.py read the same
# AGENT_TIMEOUT_SECONDS env var for both the harness cap and the window --
# so shortening the window without also shrinking the cap (and reintroducing
# the censoring agent_timeout_seconds was raised to avoid, see above) needed
# a driver.py change, not a tfvars value. No line here as a result.
#
# 480s narrows the baseline to 24 minutes before the fault, which is a large
# improvement and not a fix. On the first problem a slot runs there are only
# about 229 seconds of cluster-up time before the fault (measured on
# run-20260908-0739-4fc0249e shard-0 slot-0: process start 07:44:47, fault
# 07:48:35), so a baseline three widths back lands inside the cluster's
# lifetime only for a window under 76 seconds, too narrow to hold a usable
# incident period. For the first problem in each slot, roughly 12 of this
# run's 21, the baseline still predates the cluster. Second and later
# problems in a slot are fine: the cluster is already built by then.
# Closing the remainder needs a settle period before the first fault, or
# brain-go clamping baselineStart to when it first saw the cluster, and
# neither is this value's job.
#
# BRAIN_INCIDENT_WINDOW_SECONDS reads from the process environment, so a
# future run that needs a different width can set it in the shell that runs
# the driver; it is not yet threaded through terraform, since nothing has
# needed a per-run value yet.
noise = false

# gp3's 125 MB/s baseline is shared by every slot on the box, and image pulls
# are most of the ~35 GB each slot writes. Three slots did not flatten it;
# seven building kind clusters at the same time is the case it was never
# tested against. Raising throughput is free of the disk-size question and
# cheaper than the instance-store work in #49.
disk_throughput_mbps = 500

# On demand, not spot. run-20260901-2117 lost two of three shards to spot
# interruption notices minutes after boot: 27 of 63 problem-runs gone, and the
# 13 problems that survived were chosen by which boxes AWS kept rather than at
# random, so the result was not a subset anyone would quote. The on-demand
# premium on a few hours is small against re-running the whole thing, and
# against reading a biased number as if it were a measurement.
use_spot = false

# See variables.tf for why an empty value fails here. The name comes from
# CloudTrail, which records iamInstanceProfile on every RunInstances: every
# successful launch between 2026-08-28 and 08-31 used this one. Do not take it
# from setup_brain_go.sh's log line, which prints IMDS_ROLE, the role rather
# than the profile, and is named -instance-role; passing that gets
# "Invalid IAM Instance Profile name" part way through an apply.
instance_profile_name = "sregym-benchmark-instance-profile"

# gp3 IOPS, not throughput, is what image pulls hit. At the 3000 baseline
# a 500 MB/s setting is unreachable: gp3 caps throughput at 0.25 MB/s per
# provisioned IOPS, so small-block layer writes throttle near 48 MB/s across
# 12 containerd stores. 16000 is the gp3 max and costs ~$1.25 for this run.
disk_iops = 16000

# Pinned, where run-20260908-0739-4fc0249e left brain_go_ref unset and floated
# on "main" resolved at each instance's boot. At that run's sha, a signal
# agent whose every tool call failed still asked the model for an evidence
# report, and the aggregator folded the result into the run-wide citable
# evidence list; about 3.5% of that run's MCP calls failed, so some of its 45
# scored attempts may cite fabricated evidence. brain-go commit 063119c2
# gates that (NOFireAI/brain-go#2549).
#
# Pinned to a sha rather than left on "main" so the run records what it
# measured, but deliberately not to 063119c2 itself. That commit predates two
# changes this run exists to measure: b81d2918 stops the secret-redaction
# marker reading as broken config, which accounted for 4 attempts across 3
# problems in the last run, and 5ff00330 adds the counter that makes a gated
# evidence report countable at all. Pinning behind them would measure neither.
#
# The recursion-budget confound that argues for the earlier pin does not exist
# on this path. main's "agentflow: increase recursion limit (#2815) (#2820)"
# raises AgentRecursionLimit, which chatflow/limits.go documents as capping an
# agent child workflow's super-steps. The RCI graph has its own separate
# budget, Limits.RCI from CHAT_SERVICE_RCI_RECURSION_LIMIT, which is the one
# raised above. FEATURE_FORCE_RCI_FOR_CLI_ENABLED routes every CLI turn
# straight to RCI, so the agent branch #2815 touches is never taken here.
#
# The second re-anchor. run-20260921-1250 (brain-go ae48c4fc4, 21/63) is the
# previous anchor. Between that pin and 97f466975, 97 commits landed, 47 of
# them on the RCI path, and they change what every attempt does here:
#
#   - The planner sees a log-message count table before it proposes a cause
#     (brain-go#3369) and a baseline-window count beside it (brain-go#3437),
#     rendered from state the workflow seeds before the first turn. Every
#     attempt here is chat-sourced with no alert rule, so the baseline is
#     three times the incident window.
#   - The evaluator refuses a baseline_noise verdict the rate comparison
#     contradicts (brain-go#3444) and gates confirmation on alert coverage,
#     with both sides of the ratio now counted rather than self-reported: the
#     denominator grounded in the counted table (#3444), refused when
#     counted on another connection (#3449), the numerator checked
#     against counted rows (#3453), and the ask scoped to the agent
#     whose tools can count (#3450). A hypothesis confirms less easily.
#   - Custom MCP tools are scoped by signal (brain-go#3435). With sregym#116
#     tagging prometheus, jaeger and loki, the metrics, logs and traces
#     workers hold one server each instead of the same nine tools. The
#     planner's union is unchanged.
#   - Inconclusive cards show the log breakdown and the ruled-out theories
#     (#3433).
#
# On the harness side (sregym 6cbb3db8 to f42ead3a): the MCP pod runs an
# image built from this checkout instead of upstream's 2026-09-06 build, so
# its event loop no longer blocks on kubectl and the supervisor restarts a
# forward only when it is dead. Runs grade diagnosis only, so the archived
# Temporal history is the diagnosis turn for the first time. The prompt
# names the real application; every earlier run said "Application: unknown".
#
# What this run answers:
#
#   1. Whether the coverage gate moves the inconclusive count off 21 of 63,
#      and in which direction. A confirmed verdict now needs a counted
#      denominator this account may not supply; the readout counts
#      rci_alert_coverage_gate_total and the denominator counters.
#   2. Per-worker tool calls by signal now that each holds one server:
#      dispatch against logs 89 / state 82 / metrics 25 / traces 4, and
#      whether traces is called at all.
#   3. Restarts and breaker trips against 43 and 4. Both should be near
#      zero with the fixed MCP image; if not, the image did not load.
#   4. Tokens per attempt and TTD against 0.95M and 209 s, with the run now
#      one turn instead of two, so the run total is not comparable with any
#      earlier run and the per-attempt diagnosis figures are.
#   5. The archive: 63 diagnosis histories readable by brain-go's
#      cmd/replaycorpus batch, which reports the turn it read.
#
# Do not read the headline rate as a result. At 63 attempts the interval
# covers zero for anything under about 23pp. The nine-run band is 14 to 22.
#
# This run is the gate anchor. The regression rule (a drop of more than 10
# of 63, or any of the counts above outside the band of the last three
# clean runs) applies from here, and every quality change is measured
# against this archive by replay before it lands.
#
# The entity-resolution risk noted on the previous pin is still not
# exercised: the account has no entity graph, because the harness installs
# no edge agent, so the resolver finds nothing whatever the prompt names.
brain_go_ref = "97f4669755904fae14cb31a5820cd00192bf6a73"

# Pinned for the same reason brain_go_ref is. sregym_ref defaults to "main"
# and resolves at each instance's boot, so the four hosts in a run can check
# out different commits if something merges while they are booting, and a
# rerun weeks later is not the same run.
#
# f42ead3a is main after sregym#112 to #116. #112 builds the MCP image from
# this checkout and records it in the manifest and per slot; #113 restarts a
# forward only when its process is gone or its port refuses or resets;
# #114 grades diagnosis only by default and archives the diagnosis turn's
# history; #115 runs the benchmark suite in CI; #116 tags the observation
# connections with their signal and names the application in the prompt.
# Together they make this the first run whose archive is a diagnosis corpus
# and whose signal workers hold distinct toolsets.
sregym_ref = "f42ead3ad823e36e3696196c882e3ccc86511f7d"
