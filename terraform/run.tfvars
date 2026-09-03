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

instance_count    = 4
runs_per_instance = 3
n_attempts        = 3

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
agent_timeout_seconds = 2400

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
