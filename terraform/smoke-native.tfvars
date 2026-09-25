# Prove the native arm stands up before paying for a paired run.
#
# One problem, one instance, one slot, one attempt. It answers the questions
# smoke.tfvars answers plus the four this arm adds, and none of them is a
# measurement:
#
#   - does the cluster come up with its API server on the host's private
#     address, and does brain-go's Kubernetes connection pass its own probe
#     against it
#   - does the edge agent install, start, and publish
#   - is brain-go holding entities by the end of the pre-fault window
#   - is there a change event on the mutated component afterwards
#
# The last two land in slots/<n>/.../arm-native.json per attempt, which is
# what to read after the run rather than the score.
#
# wrong_dns_policy_astronomy_shop rather than a cluster-level fault: the
# component it mutates is a named workload in a namespace, so the change-event
# half of the evidence has something to match on. A cluster-level fault would
# leave that half empty and it would not be clear whether that meant "no event"
# or "nothing to look for".

arm = "native"

problems = [
  "wrong_dns_policy_astronomy_shop",
]

instance_count    = 1
runs_per_instance = 1
n_attempts        = 1

# One problem, not two, so an hour is enough: 1800s of agent budget plus the
# 150s pre-fault window plus about 20 minutes of boot, cluster and edge
# install. smoke.tfvars needs two hours because it runs two problems serially.
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

# Pinned rather than floated on main, so a merge during the smoke cannot make
# this a different run than the one that gets read. brain-go a891128ad is main
# after NOFireAI/brain-go#3557, which lets a Kubernetes connection opt in to a
# private API server; without it the native Kubernetes connection cannot reach
# the slot's kind cluster and the slot stops at seeding. sregym 6cf347d2 is
# main after #118 (the arm) and #119, which fixed the two bugs the first
# native smoke found: the api published twice on loopback, and terminate
# aborting under set -u on every one-argument call.
brain_go_ref = "a891128ad3496b8cdf5f677e1f0c88aeaefccbe5"
sregym_ref   = "4d590992ef9a4503d5cb50bd3c14d30466495d73"

# The results bucket already exists and is owned by the default workspace, so
# a scenario that does not name it tries to create it and fails with
# BucketAlreadyOwnedByYou. smoke-pin.tfvars carries the same line for the same
# reason.
results_bucket = "sregym-bench-results-819709950457"
