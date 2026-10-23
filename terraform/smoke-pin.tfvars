# One problem at the run's exact pins, to check before a scored run that the
# pins boot: the box reads every image from the registry cache (a missing
# one fails loudly at boot), brain-go at the pinned commit starts and takes
# the model and effort it is given, and an attempt carries a real
# investigation to a report. To smoke a Sonnet 5.5 wave's configuration,
# copy the agent_model and reasoning_effort lines of its run-sonnet55-*.tfvars
# into a copy of this file.
#
# Not terraform/smoke.tfvars, which deliberately leaves both refs unset so it
# tests whatever origin/main is at the time. A smoke that validates a pin has
# to carry the pin.
#
# service_wrong_pod_selection_hotel_reservation, one attempt. Picked on
# measured outcomes, not on its score: in run-20260917-ctl3 all three of its
# attempts ended on the inconclusive path, the one that emits "Investigation
# inconclusive: none of the evaluated hypotheses could be confirmed", and it
# has scored 0 of 24 across the eight comparable runs. It is the most
# reliably inconclusive problem in the suite.
#
# That is what this smoke needs, because the Observed signals block from
# brain-go #3269 renders only on an inconclusive report and no archive has
# ever held one. The first attempt at this smoke used
# wrong_dns_policy_astronomy_shop on the theory that a problem scoring 2 of 24
# would end inconclusive. It did not: run-20260918-0918-a46958c4 confirmed a
# hypothesis at 95% and scored 0, so the block never rendered. Scoring zero
# and ending inconclusive are different outcomes and only the second one
# exercises the block.
#
# One thing no archive can tell us in advance: #3269 renders the block only
# when the run banked at least one observation, and the observations channel
# did not exist in ctl3. A run that ends inconclusive having banked none
# still shows nothing.
problems = [
  "service_wrong_pod_selection_hotel_reservation",
]

instance_count    = 1
runs_per_instance = 1
n_attempts        = 1

max_runtime_hours     = 2
keep_alive_on_failure = true
use_spot              = false # a reclaim mid-smoke reads like a harness failure
instance_profile_name = "sregym-benchmark-instance-profile"

# The run's values, so the smoke measures the run's configuration and not a
# different one. See terraform/run.tfvars for why each is what it is.
region                = "us-east-2"
agent_model           = "us.anthropic.claude-sonnet-4-6"
agent_timeout_seconds = 2400
noise                 = false

brain_env = {
  CHAT_SERVICE_RCI_RECURSION_LIMIT = "200"
}

# s3.tf gates its five bucket resources on this being empty. In a workspace
# whose state does not hold the bucket, which is this worktree and both arm
# workspaces, empty means CreateBucket and a 409 BucketAlreadyOwnedByYou.
# Setting it is what makes the launch work.
#
# Do not plan with this set from the primary checkout's default workspace,
# whose state does hold all five (terraform.tfstate, serial 320): there it
# plans five destroys, and the public-access block, encryption, versioning
# and lifecycle expiry are destroyed for real before the bucket delete fails
# on BucketNotEmpty. run.tfvars carries the long form of this warning.
#
# variables.tf says to set this only for a bucket that already carries the
# four settings. Checked against the live bucket first: all four
# public-access-block flags true, SSE AES256, versioning Enabled, lifecycle
# rule expire-noncurrent Enabled.
results_bucket = "sregym-bench-results-819709950457"

brain_go_ref = "2f341c4a73c634872724ceac9b39d1a49f524f6c"
sregym_ref   = "2c0992ea8db07d8265796da3af8c52344e569d5e"
