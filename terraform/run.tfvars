# The scored run: 21 sregym-lite problems x 3 attempts = 63 problem-runs.
#
# This shape is fixed deliberately. run-20260831-1752 was 21 x 3, so keeping it
# identical makes the next run a paired comparison against it and isolates what
# changed in between. Changing n_attempts or the problem set costs that
# comparison, which is most of the value.
#
# 63 paired problem-runs resolves about 23pp at 80% power. It is the smallest
# honest run, not a precise one: a 10 to 15pp claim needs 150 to 336, i.e. 7 to
# 16 attempts on Lite or the full 90-problem suite x 3.
#
# problems is left unset, which means the whole set. A narrowed run is not
# comparable with this one.

instance_count        = 3
runs_per_instance     = 3
n_attempts            = 3
agent_timeout_seconds = 1800
noise                 = false

# On demand, not spot. run-20260901-2117 lost two of three shards to spot
# interruption notices minutes after boot: 27 of 63 problem-runs gone, and the
# 13 problems that survived were chosen by which boxes AWS kept rather than at
# random, so the result was not a subset anyone would quote. The on-demand
# premium on a few hours is small against re-running the whole thing, and
# against reading a biased number as if it were a measurement.
use_spot              = false

# See variables.tf for why an empty value fails here. The name comes from
# CloudTrail, which records iamInstanceProfile on every RunInstances: every
# successful launch between 2026-08-28 and 08-31 used this one. Do not take it
# from setup_brain_go.sh's log line, which prints IMDS_ROLE, the role rather
# than the profile, and is named -instance-role; passing that gets
# "Invalid IAM Instance Profile name" part way through an apply.
instance_profile_name = "sregym-benchmark-instance-profile"
