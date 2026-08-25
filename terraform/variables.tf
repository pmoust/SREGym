# ---------------------------------------------------------------------------
# Fleet shape
# ---------------------------------------------------------------------------

variable "instance_count" {
  description = <<-EOT
    How many instances share the suite. Problems are sharded statically by
    index % instance_count, so this is the only parallelism dial until the
    harness host ports are parameterised (NOFireAI/brain-go#2219).
  EOT
  type        = number
  default     = 1

  validation {
    condition     = var.instance_count >= 1 && var.instance_count <= 32
    error_message = "instance_count must be between 1 and 32."
  }
}

variable "runs_per_instance" {
  description = <<-EOT
    Concurrent problems per instance. Must stay at 1 until
    NOFireAI/brain-go#2219 parameterises the harness host ports: the MCP server
    pins 9954, the observe port-forwards pin 9090 and 3100, the k8s proxy pins
    16443, and the conductor API pins 8000, so two harness processes cannot
    share a host. Raise this once that lands and lower instance_count instead,
    since one cluster per run is a hard constraint but one host per run is not.
  EOT
  type        = number
  default     = 1

  validation {
    condition     = var.runs_per_instance == 1
    error_message = "runs_per_instance must be 1 until NOFireAI/brain-go#2219 parameterises the harness host ports."
  }
}

# ---------------------------------------------------------------------------
# What to run
# ---------------------------------------------------------------------------

variable "problem_set" {
  description = "Suite name passed to main.py --suite. Empty runs the full registry."
  type        = string
  default     = "sregym-lite"
}

variable "n_attempts" {
  description = "Runs per problem. The paper's protocol is 3. Use 1 for a first cost sweep."
  type        = number
  default     = 1
}

variable "noise" {
  description = "Enable Chaos Mesh noise injection. The paper reports with and without."
  type        = bool
  default     = false
}

variable "agent_model" {
  description = <<-EOT
    Model brain-go answers with. Start cheap: the first run exists to produce a
    cost projection, not a score. Bedrock ids take an anthropic. prefix.
  EOT
  type        = string
  default     = "anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "judge_model" {
  description = <<-EOT
    Model the checklist judge uses. Must differ from agent_model: two judges
    agreeing is weaker evidence than one good judge, and a model grading its own
    output is not a measurement. Enforced below.
  EOT
  type        = string
  default     = "anthropic.claude-sonnet-5"
}

variable "agent_timeout_seconds" {
  description = "Per-run agent timeout. The paper uses 1800 and counts timed-out runs at the cap."
  type        = number
  default     = 1800
}

# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------

variable "instance_type" {
  description = <<-EOT
    Sized for concurrency, not for one run. Each instance carries kind clusters
    plus a full brain-go stack, since BRAIN_API_URL defaults to localhost:8001.
    Re-measure on the first deploy and tune.
  EOT
  type        = string
  default     = "r7a.24xlarge"
}

variable "disk_gb" {
  description = <<-EOT
    Deliberately generous. Every kind node keeps its own image cache, so the
    demo app images multiply. ENOSPC is what kills the smoke-test CI today, and
    it surfaces as ErrImagePull rather than as a disk error.
  EOT
  type        = number
  default     = 500
}

variable "disk_iops" {
  description = "gp3 baseline is 3000, free. Raise before raising disk_gb if image pulls throttle."
  type        = number
  default     = 3000
}

variable "disk_throughput_mbps" {
  description = "gp3 baseline is 125 MB/s, free."
  type        = number
  default     = 125
}

variable "spot_max_price" {
  description = <<-EOT
    Empty means the on-demand price as the cap, which is the safe default: you
    pay the spot rate, never more than on-demand. r7a.24xlarge spot has been
    running around $1.43 against $7.30 on-demand in us-east-1.
  EOT
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

variable "max_runtime_hours" {
  description = <<-EOT
    Hard cap. The instance terminates itself when this elapses regardless of
    progress, so a wedged run cannot bill indefinitely. This is the backstop;
    the normal path is self-termination on shard completion.
  EOT
  type        = number
  default     = 12
}

variable "keep_alive_on_failure" {
  description = <<-EOT
    Skip self-termination so the box survives for debugging. Costs real money
    per hour, so it is off by default and the max_runtime_hours cap still applies.
  EOT
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

variable "region" {
  description = "us-east-1: brain-go's account is there and Bedrock quota is the real constraint, not latency."
  type        = string
  default     = "us-east-1"
}

variable "run_id" {
  description = "Names the S3 prefix and tags. Leave empty for a timestamped one."
  type        = string
  default     = ""
}

variable "sregym_ref" {
  description = "Git ref of the sregym mirror to check out."
  type        = string
  default     = "main"
}

variable "brain_go_ref" {
  description = <<-EOT
    Git ref of brain-go to run. It is checked out on the instance because
    BRAIN_API_URL defaults to localhost:8001, so brain-go runs beside the
    harness rather than centrally.
  EOT
  type        = string
  default     = "main"
}

variable "github_token_ssm_path" {
  description = <<-EOT
    SSM SecureString holding a token that can clone both this private mirror
    and NOFireAI/brain-go (the bootstrap checks out brain_go_ref with the same
    token). The instance role is scoped to read only this parameter.
  EOT
  type        = string
  default     = "/sregym/benchmark/github-token"
}

variable "results_bucket" {
  description = "Existing bucket for results. Empty creates one owned by this config."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Extra tags merged onto everything."
  type        = map(string)
  default     = {}
}
