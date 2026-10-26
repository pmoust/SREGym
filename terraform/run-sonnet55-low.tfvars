# The Sonnet 5.5 low-effort wave of the pin in run.tfvars, as a scenario
# launch.sh can run: ./scripts/benchmark/launch.sh run-sonnet55-low
#
# Every value here is run.tfvars's, so the three waves differ only in
# agent_model and reasoning_effort; tests/benchmark/test_wave_scenarios.py
# fails when the two files drift. The rationale for each value is in
# run.tfvars, not repeated here, with one exception that is a launch rule
# rather than a rationale: results_bucket. Plan this file only from a
# workspace whose state does not hold the results bucket, never from the
# primary checkout's default workspace, where s3.tf would plan five
# destroys and strip the bucket's public-access block, encryption,
# versioning and lifecycle before the delete fails. run.tfvars carries the
# long form of that warning above its results_bucket line.
#
# The model id is the Bedrock inference
# profile brain-go maps claude-sonnet-5-5 to; the effort reaches brain-go as
# LLM_EFFORT through setup_brain_go.sh, and the manifest records the value
# the box started with.
region                = "us-east-2"
results_bucket        = "sregym-bench-results-819709950457"
instance_count        = 4
runs_per_instance     = 3
n_attempts            = 3
agent_model           = "us.anthropic.claude-sonnet-5-5"
reasoning_effort      = "low"
max_runtime_hours     = 6
agent_timeout_seconds = 2400
brain_env = {
  CHAT_SERVICE_RCI_RECURSION_LIMIT = "200"
}
noise                 = false
disk_throughput_mbps  = 500
use_spot              = false
instance_profile_name = "sregym-benchmark-instance-profile"
disk_iops             = 16000
brain_go_ref          = "2f341c4a73c634872724ceac9b39d1a49f524f6c"
sregym_ref            = "c3799eeb7070ab974c957d6cff8c270c3056271c"
