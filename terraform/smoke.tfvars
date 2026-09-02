# Prove the harness runs before paying for a scored run.
#
# Two problems on one instance, one slot, one attempt. It answers one question:
# does a real instance get through boot, cluster creation, brain-go setup, the
# port-forward, tenant seeding, and into a problem? Nothing here is a
# measurement, and nothing from a smoke run should be quoted.
#
# The two problems are deliberately on different apps. A pair on the same app
# would exercise one deploy path twice and leave the other untested.
#
# keep_alive_on_failure leaves the box up when terminate() fires so the failure
# can be inspected. The runtime cap still kills it: hard_stop does not honour
# the flag, on purpose.

problems = [
  "network_policy_block",            # no app prefix, cluster-level fault
  "wrong_dns_policy_astronomy_shop", # astronomy shop
]

instance_count    = 1
runs_per_instance = 1
n_attempts        = 1
# Two hours, not one. One could not finish this scenario: two problems run
# serially in a single slot, each with the 1800s agent cap, is 60 minutes of
# agent budget before any setup or deploy. run-20260902-1556 spent 13 minutes
# on setup, hit the cap on the first problem, diagnosed the second correctly at
# 16:52, and was killed by the runtime backstop at 16:56 before that verdict
# could be graded. It scored 1 of 2 and read like a harness failure.
#
# It only ever passed because agents usually finish early. A test now pins the
# arithmetic so the next scenario cannot get this wrong quietly.
max_runtime_hours     = 2
keep_alive_on_failure = true
use_spot              = false # a reclaim mid-smoke reads like a harness failure

# See variables.tf for why an empty value fails here. The name comes from
# CloudTrail, which records iamInstanceProfile on every RunInstances: every
# successful launch between 2026-08-28 and 08-31 used this one. Do not take it
# from setup_brain_go.sh's log line, which prints IMDS_ROLE, the role rather
# than the profile, and is named -instance-role; passing that gets
# "Invalid IAM Instance Profile name" part way through an apply.
instance_profile_name = "sregym-benchmark-instance-profile"
