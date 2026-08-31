# ---------------------------------------------------------------------------
# Fleet shape
# ---------------------------------------------------------------------------

variable "instance_count" {
  description = <<-EOT
    How many instances share the suite. Problems are sharded statically by
    index % instance_count, then split again across each instance's slots by
    runs_per_instance. Prefer raising runs_per_instance and keeping this small:
    the target shape is 3 to 4 large instances running many slots each, not many
    instances running one apiece, which costs roughly ten times as much for a
    similar wall clock. See NOFireAI/brain-go#2218's fleet-shape table.
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
    Concurrent problems per instance. Each slot gets its own kind cluster, its
    own brain-go tenant, its own results tree, and its own API/MCP/k8s-proxy
    host ports (scripts/benchmark/run_slot_env.sh). This is the dial that
    matters for cost: the target shape is a handful of large instances each
    running many slots, not many instances running one apiece. See
    NOFireAI/brain-go#2218's fleet-shape table.

    Sizing is not yet measured on a real instance. #2218 estimates roughly 12 GB
    and 4 to 6 vCPU per concurrent run and flags that figure as known-low, since
    each instance also carries the shared brain-go stack with its Postgres and
    Temporal. On an r7a.24xlarge (96 vCPU, 768 GB) that suggests somewhere
    around 12 to 19, but the first real deploy is what replaces the estimate.
    The cap below is a guard against a typo costing a fleet, not a measured
    ceiling.
  EOT
  type        = number
  default     = 1

  validation {
    condition     = var.runs_per_instance >= 1 && var.runs_per_instance <= 32
    error_message = "runs_per_instance must be between 1 and 32."
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
    Model brain-go answers with. Read by setup_brain_go.sh as LLM_MODEL for
    brain-go's own Go bedrockProvider, which calls bedrockruntime.Converse
    directly: a raw Bedrock model/inference-profile id, no provider prefix.
    Also passed to main.py's --model, which main.py does not use for brain_go's
    own answering (cosmetic logging and the --judge-model fallback only), so
    its format there does not matter.
  EOT
  type        = string
  default     = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
}

variable "judge_model" {
  description = <<-EOT
    Model the checklist judge uses. Must differ from agent_model: two judges
    agreeing is weaker evidence than one good judge, and a model grading its own
    output is not a measurement. Enforced below.

    Passed to main.py's --judge-model, which hands it to LiteLLM directly: needs
    the bedrock/ provider prefix (unlike agent_model above) or LiteLLM routes to
    a direct Anthropic API key this fleet does not have.
  EOT
  type        = string
  default     = "bedrock/us.anthropic.claude-sonnet-5"
}

variable "brain_env" {
  description = <<-EOT
    Extra key/value pairs written into brain-go's deploy/.env before its stack
    starts, for sweeping its tuning knobs from a run's configuration rather
    than by editing a script (NOFireAI/brain-go#2282).

    Anything brain-go reads from the environment works here, for example
    CHAT_SERVICE_RCI_TEMPERATURE, CHAT_SERVICE_RCI_MAX_HYPOTHESES or
    CHAT_SERVICE_RCI_INJECT_TIME_WINDOWS.

    Entries here are merged over the derived defaults in locals.brain_env, so
    setting one key does not drop the others.

    Do not put secrets here. A map(string) variable is not sensitive, so values
    reach terraform state, plan and apply output, and user_data (readable via
    ec2:DescribeInstanceAttribute), as well as a plaintext .env on the
    instance. Key names are also logged; values are not.

    A value containing a line equal to the bootstrap's heredoc terminator would
    end that heredoc early and run the rest as root. The input is your own
    -var, so this is self-inflicted rather than a vector, but it is the one
    thing the heredoc does not defend against.
  EOT
  type        = map(string)
  default     = {}

  # Checked at plan. Without this a bad key reaches the instance, dies inside
  # setup_brain_go.sh and terminates a launched box: the one late failure in
  # this path, and the expensive kind. The rules match what that script
  # enforces on arrival, so the two agree rather than drifting apart.
  #
  # A newline in a value would split into a second .env line, and the
  # instance-side parser reads line by line, so it would write the first half
  # and then die on the remainder having no "=".
  validation {
    condition = alltrue([
      for k, v in var.brain_env :
      can(regex("^[A-Za-z_][A-Za-z0-9_]*$", k)) && !strcontains(v, "\n")
    ])
    error_message = "brain_env keys must be shell names ([A-Za-z_][A-Za-z0-9_]*) and values must not contain newlines."
  }
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

variable "use_spot" {
  description = <<-EOT
    Spot when true, on-demand when false. Spot stays the default: it is roughly
    a quarter of the price and a run that resumes from S3 survives losing a box.

    Turn it off while the pipeline itself is being debugged. A reclaim arrives
    without warning and costs the whole run: two runs died that way, one of
    them three hours in, and that one took its logs with it because the
    interruption handler reached the box before terminate() did. While each
    iteration is still finding a new bug, the price difference is worth less
    than the lost iteration.
  EOT
  type        = bool
  default     = true
}

variable "spot_max_price" {
  description = <<-EOT
    Ignored when use_spot is false. Empty means the on-demand price as the cap,
    which is the safe default: you pay the spot rate, never more than
    on-demand. r7a.24xlarge spot has been running around $1.43 against $7.30
    on-demand in us-east-1.
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
    Skip self-termination so the box survives for interactive debugging. Costs
    real money per hour, so it is off by default. The max_runtime_hours cap is
    unaffected and still terminates the instance: it runs through hard_stop,
    which this flag deliberately does not suppress.

    Usually unnecessary. Both the bootstrap log and the per-slot logs are copied
    to S3 under <results>/logs/ on every exit, including failures, so a run that
    terminates itself can still be debugged after the fact.
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

    A branch name, a tag, or a full 40-character commit SHA. Prefer a SHA for
    anything whose numbers will be compared later: "main" is resolved on each
    instance at boot, so three shards launching minutes apart can pick up
    different commits, and a rerun weeks later gets whatever main is then.

    Full, not abbreviated. `git fetch origin <ref>` treats a ref as an object
    name only when it is 40 hex characters; anything shorter is looked up as a
    ref name and does not resolve, which fails the boot exactly as `clone
    --branch <sha>` used to. The validation below rejects the short form rather
    than letting it terminate a launched fleet.

    The commit must also still be reachable from a branch, a tag or a PR head.
    GitHub serves reachable objects only, so a commit that was force-pushed
    away, or that sat on a branch deleted without a PR, cannot be fetched.
  EOT
  type        = string
  default     = "main"

  # Either a full SHA, or something that does not look like a truncated one. A
  # 7-to-39 character hex string is almost always an abbreviated commit, and
  # accepting it means a plan and an apply that both succeed followed by three
  # instances terminating eighty seconds in. brain_env's validation above exists
  # for the same reason: this path's failures are late and expensive.
  validation {
    condition = (
      can(regex("^[0-9a-f]{40}$", var.brain_go_ref)) ||
      !can(regex("^[0-9a-f]{7,39}$", var.brain_go_ref))
    )
    error_message = "brain_go_ref looks like an abbreviated commit SHA. Use the full 40-character SHA: git fetch cannot resolve a short one and the instance will terminate at boot."
  }
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

variable "instance_profile_name" {
  description = <<-EOT
    Existing instance profile name to launch instances with. Empty creates a
    fresh role, inline policy and profile per run (iam.tf), which needs a wide
    IAM grant on whoever runs terraform apply/destroy. Setting this skips all
    of that: an admin creates one static role/profile by hand, using the
    policy document in iam.tf (data.aws_iam_policy_document.bench) verbatim,
    and the caller only needs iam:PassRole scoped to that one role's ARN.
  EOT
  type        = string
  default     = ""
}

variable "tags" {
  description = "Extra tags merged onto everything."
  type        = map(string)
  default     = {}
}
