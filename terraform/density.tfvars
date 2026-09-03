# How many slots does one box actually hold?

# run-20260903-0822 answered "not seven". Three boxes at seven slots lost all
# 63 problem-runs: 61 deploy failures, 32 in the openebs namespace and 27 in
# observe, 39 of them ImagePullBackOff. Not one problem reached its agent.
#
# The number was never measured on this instance type. It was interpolated
# from NOFireAI/sregym#49's per-slot figures (~0.83 cores, ~7.3 GB, ~35 GB of
# disk), which were taken at THREE slots on an r7a.24xlarge: 96 vCPU and
# 768 GB, three times the CPU and six times the memory of an m8i.8xlarge. On
# m8i the only density ever proven is one, twice, on two-problem smokes.
#
# So measure it rather than interpolate again. One box, the load a real shard
# puts on it, and the slot count as the only variable. Run this at
# runs_per_instance = 3, then 5, then 7, and take the highest that gets every
# slot past deploy.
#
# What to read afterwards, in the slot logs:
#
#   grep -c "Injected fault" shard-0/slots/*/logs/*.log
#     One per slot means every slot deployed. Fewer means the density failed.
#
#   grep "start_problem failed" -A6 shard-0/logs/sregym-slot-*.log
#     Now carries the container's message as well as its reason, so a pull
#     failure says whether it was disk, network, a rate limit or a credential
#     (#72). That is the number this probe exists to produce.
#
# The first deploy wave is the whole test and lands inside ~30 minutes. Killing
# the box once it has landed is fine and saves the agent time; the runtime cap
# below is only a backstop.

# Seven, a real shard's worth, spanning all three apps so all three deploy
# paths run at once. Kept at seven for every density so the load is identical
# and only the concurrency changes. finalizer_deadlock_controller is out: it
# burned all three deploy attempts on its own account before #50, and a probe
# should not spend a slot on a known failure.
problems = [
  "network_policy_block",
  "wrong_dns_policy_astronomy_shop",
  "env_variable_shadowing_astronomy_shop",
  "duplicate_pvc_mounts_social_network",
  "readiness_probe_misconfiguration_social_network",
  "admission_webhook_outage_hotel_reservation",
  "cronjob_sidecar_blocks_completion_hotel_reservation",
]

instance_count = 1
n_attempts     = 1

# The variable under test. 3 first, since it is the only density with any
# evidence behind it, then 5, then 7.
runs_per_instance = 3

# Backstop only. Depth is ceil(7/slots), so at three slots the agent phase
# alone could run three cycles; the probe's answer arrives long before that.
max_runtime_hours = 3

use_spot              = false
keep_alive_on_failure = true # the failure is the output here, so keep the box

# See variables.tf for why an empty value fails here.
instance_profile_name = "sregym-benchmark-instance-profile"
