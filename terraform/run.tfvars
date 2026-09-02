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

# Full Lite width: 3 x 7 = 21 slots for 21 problems, so every slot owns exactly
# one problem and runs its 3 attempts back to back. Both splits are static
# index-based (problem % shard_count, then index % runs_per_instance), so an
# even 7/7/7 needs an instance_count that divides 21.
#
# Width is what buys wall clock, because attempts are serial inside a slot
# (main.py loops problems, then attempts, with no gather). Measured per slot on
# run-20260902-0606: a 24.8 min median attempt cycle, and a setup cost before
# the first attempt that is bimodal, four slots at 16 to 21 min and five at 56
# to 58, median 55.7. That run ran nine slots up to nine attempts deep and
# finished in about 4.9 hours, which is when its slowest slot finished, not
# when a typical one did (median slot: 3.2 hours).
#
# At 21 slots the depth is 3, so the same 63 problem-runs land in about 2.2
# hours. Read that as the slowest slot again: the run ends when the last slot
# does, and every slot pays its own setup.
#
# The tail is a first deploy that fails before fault injection and silently
# restarts the whole problem, so the slot pays the deploy twice (#53). Twelve
# deploys in that run produced no attempt, and a restart logs as an ordinary
# "Start testing on problem", so nothing distinguishes a run that lost twelve
# from one that lost none.
#
# It scales the wrong way with width: every slot pays its own setup, so 21
# slots is 21 chances to draw the retry against 9 before. Setup is roughly
# half the projected wall clock above, which makes #53 the next thing worth
# fixing for speed, ahead of anything else in this file.
#
# Going wider than 21 needs more problems, not more slots: a problem maps to
# one slot, so extra slots draw nothing and skip.
# Pinned rather than left to the variable default, which was us-east-1 until
# recently: omitting it put the results bucket in a region archive_run.sh
# does not look in, and the symptom was "no bucket matching" after the run
# had already been billed. Every run to date has been us-east-2.
region = "us-east-2"

instance_count    = 3
runs_per_instance = 7
n_attempts        = 3

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
