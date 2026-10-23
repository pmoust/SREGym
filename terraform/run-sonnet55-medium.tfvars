# The Sonnet 5.5 medium-effort wave of the pin in run.tfvars, as a scenario
# launch.sh can run: ./scripts/benchmark/launch.sh run-sonnet55-medium
#
# Every value here is run.tfvars's, so the three waves differ only in
# agent_model and reasoning_effort; tests/benchmark/test_wave_scenarios.py
# fails when the two files drift. The rationale for each value is in
# run.tfvars, not repeated here. The model id is the Bedrock inference
# profile brain-go maps claude-sonnet-5-5 to; the effort reaches brain-go as
# LLM_EFFORT through setup_brain_go.sh, and the manifest records the value
# the box started with.
region                = "us-east-2"
results_bucket        = "sregym-bench-results-819709950457"
instance_count        = 4
runs_per_instance     = 3
n_attempts            = 3
agent_model           = "us.anthropic.claude-sonnet-5-5"
reasoning_effort      = "medium"
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
sregym_ref            = "2c0992ea8db07d8265796da3af8c52344e569d5e"
