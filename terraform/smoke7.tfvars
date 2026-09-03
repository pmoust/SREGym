# Superseded: seven slots does not work. Use density.tfvars.

# This asked whether one brain-go stack holds seven slots, and
# run-20260903-0822 answered it at scale: three boxes at seven slots lost all
# 63 problem-runs to 61 deploy failures, none of which reached an agent, so the
# shared stack was never even the binding constraint. density.tfvars replaces
# this by measuring the number instead of testing one guess at it.
#
# Kept rather than deleted because the reasoning below about problem choice and
# about one box versus three still holds, and density.tfvars inherits it.

# The scored run moved to 3 x 7 slots (#54) on arithmetic, not on a measurement:
# every slot on a box shares one brain-go stack with its Postgres and Temporal,
# and the largest shape ever actually run is three. This scenario is the cheapest
# way to find out before a scored run depends on it.
#
# It cannot be the `smoke` scenario, which names two problems. A problem maps to
# exactly one slot, so two problems start two slots however high
# runs_per_instance goes, and the rest log "drew no problems, skipping".
# Testing seven slots needs at least seven problems.
#
# One box, not three. The question is contention inside a box: one stack, one
# gp3 volume, one NAT address. Three boxes would answer it three times over.
#
# What to look at afterwards, none of which the exit code tells you:
#
#   - Seven "[slot N] MCP port-forward serving" lines, and seven distinct ports.
#   - Setup spread. Boot to each slot's first "[ENV] Injected fault" was 16-21
#     min for four of nine slots and 56-58 for the other five at three slots
#     (#53). If seven slots widens that, the shared stack is the reason to look
#     at first.
#   - The attempt cycle against the 24.8 min median measured at three slots.
#   - deploy_attempts in the result rows (#55). Anything above 1 is a slot that
#     paid for its deploy twice.
#   - gp3 at 500 MB/s with seven kind clusters pulling images at once, which is
#     the other thing #54 changed on arithmetic alone.

# Seven ids spanning all three apps, so all three deploys run concurrently,
# which is the case a two-problem smoke never reaches. duplicate_pvc_mounts is
# in deliberately: it is PVC-heavy, and OpenEBS readiness is the largest known
# cause of a wasted deploy (#56).
#
# finalizer_deadlock_controller is deliberately out. It deadlocks its own
# namespace (#50) and burned all three deploy attempts in the reference run, so
# including it would spend a slot on a known failure instead of on the question
# being asked.
problems = [
  "network_policy_block",                            # cluster-level fault
  "wrong_dns_policy_astronomy_shop",                 # astronomy shop
  "env_variable_shadowing_astronomy_shop",           # astronomy shop
  "duplicate_pvc_mounts_social_network",             # social network, PVC-heavy
  "readiness_probe_misconfiguration_social_network", # social network
  "admission_webhook_outage_hotel_reservation",      # hotel reservation
  "cronjob_sidecar_blocks_completion_hotel_reservation",
]

instance_count    = 1
runs_per_instance = 7
n_attempts        = 1

# Not a measurement, so the cap stays at the paper's value rather than the
# scored run's 2400s. This scenario asks whether seven slots run at all, and a
# longer cap would only make a wedged slot cost more.
agent_timeout_seconds = 1800

# Seven slots one attempt deep is one setup plus one cycle: about 25 minutes on
# a clean setup, about 85 on the worst setup measured. 3 hours is the backstop
# against a wedge, not an expectation. The box self-terminates when its shard
# finishes, so the cap costs nothing on a healthy run.
max_runtime_hours = 3

# Matched to run.tfvars on purpose. This scenario exists to validate the shape
# the scored run uses, and gp3 throughput is one of the two things #54 raised
# specifically because of seven slots, so testing seven slots at the old 125
# MB/s would answer a question nobody is asking. Whether the raise was needed
# at all is a separate ablation.
disk_throughput_mbps = 500

use_spot              = false # a reclaim mid-smoke reads like a harness failure
keep_alive_on_failure = true  # the failure is the output here, so keep the box

# See variables.tf for why an empty value fails here.
instance_profile_name = "sregym-benchmark-instance-profile"
