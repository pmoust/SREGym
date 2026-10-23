# The Sonnet 5.5 low-effort wave. Applied on top of run.tfvars, never
# alone, so the run's pins, fleet shape and bucket are the same as the 4.6
# wave's and only these two values differ:
#
#   terraform apply -var-file=run.tfvars -var-file=wave-sonnet55-low.tfvars
#
# The model id is the Bedrock inference profile brain-go maps claude-sonnet-5-5
# to; the effort reaches brain-go as LLM_EFFORT through setup_brain_go.sh and
# the manifest records the value the box started with.
agent_model      = "us.anthropic.claude-sonnet-5-5"
reasoning_effort = "low"
