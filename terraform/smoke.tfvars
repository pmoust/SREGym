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

instance_count        = 1
runs_per_instance     = 1
n_attempts            = 1
max_runtime_hours     = 1
keep_alive_on_failure = true
use_spot              = false # a reclaim mid-smoke reads like a harness failure

# See variables.tf for why an empty value fails here. The name comes from
# CloudTrail, which records iamInstanceProfile on every RunInstances: every
# successful launch between 2026-08-28 and 08-31 used this one. Do not take it
# from setup_brain_go.sh's log line, which prints IMDS_ROLE, the role rather
# than the profile, and is named -instance-role; passing that gets
# "Invalid IAM Instance Profile name" part way through an apply.
instance_profile_name = "sregym-benchmark-instance-profile"
