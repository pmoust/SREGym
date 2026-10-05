# Prove the edge arm stands up before paying for a paired run.
#
# One problem, one instance, one slot, one attempt. Edge is native minus the
# Kubernetes API connection, so this answers the questions smoke-native.tfvars
# answers minus the ones that are specific to that connection, plus one new
# one:
#
#   - does the edge agent install, start, and publish, on the committed kind
#     config (loopback API server, untouched): edge never binds the API
#     server to the host's private address, because brain-go dials no
#     Kubernetes connection that would need to reach it
#   - is brain-go holding entities by the end of the pre-fault window
#   - is there a change event on the mutated component afterwards
#   - does the run come up with no kubernetes connection and no k8s_* tool at
#     all, unlike the native arm
#
# The last three land in slots/<n>/.../arm-native.json per attempt (named for
# the gate family both arms share, not for this arm), which is what to read
# after the run rather than the score. The run manifest is what names the arm
# itself: arm=edge and kubernetes_connection=none.
#
# wrong_dns_policy_astronomy_shop rather than a cluster-level fault, the same
# choice smoke-native.tfvars makes and for the same reason: the component it
# mutates is a named workload in a namespace, so the change-event half of the
# evidence has something to match on.

arm = "edge"

problems = [
  "wrong_dns_policy_astronomy_shop",
]

instance_count    = 1
runs_per_instance = 1
n_attempts        = 1

# One problem, not two, so an hour is enough: 1800s of agent budget plus the
# 150s pre-fault window plus about 20 minutes of boot, cluster and edge
# install. Edge skips the native arm's read-only ServiceAccount and private
# API-server bind, so if anything this arm boots a little faster than native,
# not slower.
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

# Left unset, unlike smoke-native.tfvars: this scenario needs no brain-go fix
# the native arm's private API-server bind required (NOFireAI/brain-go#3557),
# since edge seeds no Kubernetes connection to need one. Unset resolves to
# origin/main at launch time (see launch.sh), main as of whichever sregym
# commit adds the edge arm (NOFireAI/sregym#147) or later.

# The results bucket already exists and is owned by the default workspace, so
# a scenario that does not name it tries to create it and fails with
# BucketAlreadyOwnedByYou. smoke-native.tfvars and smoke-pin.tfvars carry the
# same line for the same reason.
results_bucket = "sregym-bench-results-819709950457"
