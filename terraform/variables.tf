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

    Sizing is measured now, on run-20260902-0606 at three slots: ~0.83 cores,
    ~7.3 GB and ~35 GB of disk per slot. #2218's estimate of 4 to 6 vCPU and
    12 GB was high on both, and disk, which it did not rank, is the only
    binding resource: roughly 13 slots per box against the EBS root, until
    #49 mounts the instance store.

    Slot count is capped by the problem set before it is capped by the box.
    A problem maps to exactly one slot, so slots past the problem count draw
    nothing and skip. sregym-lite is 21, which is what run.tfvars asks for
    across three instances.

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

variable "problems" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Narrow the run to these problem ids, still drawn from problem_set. Empty
    means the whole set.

    This exists so a smoke run costs ten minutes rather than a full suite. It is
    the difference between debugging the harness for the price of two problems
    and doing it for the price of twenty-one.

    Ids are checked against the set on the instance, and an unknown id is a hard
    stop rather than a silent narrowing: a typo would otherwise shard to nothing
    and the instance would terminate looking like a clean short run.

    Sharding applies after the filter, so instance_count above the number of
    problems leaves later shards empty.

    Validated below, and not only for typos. This value is interpolated into a
    double-quoted assignment in the bootstrap template, so terraform splices it
    into the script's source text before bash ever parses it. An entry
    containing a command substitution therefore runs as root at boot, ahead of
    the instance-side id check, which was demonstrated rather than assumed. The
    input is your own -var, so this is self-inflicted rather than a vector, the
    same footing as brain_env above; unlike brain_env it is a live expansion
    context rather than an inert heredoc, so it gets the stricter guard.
  EOT

  validation {
    condition     = alltrue([for p in var.problems : can(regex("^[a-z0-9_]+$", p))])
    error_message = "problems entries must match ^[a-z0-9_]+$, which is what a registered problem id looks like."
  }
}

# The four variables below, plus problems above, are interpolated into
# double-quoted shell assignments in templates/bootstrap.sh.tftpl. Terraform
# substitutes into the script's source text before bash parses it, so a value
# containing a quote or a command substitution runs as root at boot. These are
# your own -var rather than remote input, the same footing as brain_env, but
# brain_env is an inert quoted heredoc and these are live expansion contexts,
# so they get a charset guard. Review found problems validated and these four
# not, which is worse than validating none: it reads as though the class had
# been handled.

variable "problem_set" {
  description = "Suite name passed to main.py --suite. Empty runs the full registry."
  type        = string
  default     = "sregym-lite"

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]+$", var.problem_set))
    error_message = "problem_set must match ^[A-Za-z0-9_-]+$, which is what a set name in problem_sets.py looks like."
  }
}

variable "n_attempts" {
  description = "Runs per problem. The paper's protocol is 3. Use 1 for a first cost sweep."
  type        = number
  default     = 1
}

variable "arm" {
  description = <<-EOT
    Which benchmark arm the run is.

    "mcp" is the default and what every archived run is: brain-go reaches the
    cluster through SREGym's four CUSTOM_MCP endpoints and nothing else. It is
    the anchor an arm comparison is measured against, so it must stay
    reproducible.

    "native" registers brain-go's own Kubernetes, Prometheus, Loki and Jaeger
    connections against the same in-cluster backends and installs the edge
    agent into each slot's cluster, so the entity graph and change events
    exist during an attempt.

    "edge" is native minus the Kubernetes API connection: the edge agent plus
    the Prometheus, Loki and Jaeger connectors, no kubeconfig, and no k8s_*
    tools. This is NOFire's production posture, where brain-go holds no
    Kubernetes API token at all.

    Validated here as well as in the harness, because a plan-time failure
    costs nothing and a boot-time one costs an instance.
  EOT
  type        = string
  default     = "mcp"

  validation {
    condition     = contains(["mcp", "native", "edge"], var.arm)
    error_message = "arm must be mcp, native, or edge."
  }
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

  validation {
    condition     = can(regex("^[A-Za-z0-9._:/-]+$", var.agent_model))
    error_message = "agent_model must match ^[A-Za-z0-9._:/-]+$, which covers a bedrock or litellm model id."
  }
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

  validation {
    condition     = can(regex("^[A-Za-z0-9._:/-]+$", var.judge_model))
    error_message = "judge_model must match ^[A-Za-z0-9._:/-]+$, which covers a bedrock or litellm model id."
  }
}

variable "reasoning_effort" {
  description = <<-EOT
    Effort brain-go answers at. Written by setup_brain_go.sh as LLM_EFFORT into
    brain-go's deploy/.env, which brain-go reads directly (NOFireAI/brain-go#4093).

    Empty (the default) means do not write the key, so brain-go keeps its own
    default of medium. brain-go accepts only low, medium or high and rejects
    anything else, so this is validated here too: a bad value should fail at
    plan, not three minutes into a boot.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = contains(["", "low", "medium", "high"], var.reasoning_effort)
    error_message = "reasoning_effort must be empty, low, medium or high."
  }
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

variable "ami_id" {
  description = <<-EOT
    The image every benchmark instance boots. Pinned, not looked up: this
    stack used to read Canonical's "stable/current" Ubuntu 24.04 parameter,
    and every run applies fresh state, so two waves days apart could boot
    different images and their numbers would differ for a reason that is
    not the change under test. The default is the same image the sandbox
    fleet is pinned to (NOFireAI/terraform, aws/sandbox/sregym). Move it
    on purpose, with a run on each side, never as a side effect of a run.
  EOT
  type        = string
  default     = "ami-02ad619be5bc165df" # ubuntu-noble-24.04-amd64-server-20261017, us-east-2

  validation {
    condition     = can(regex("^ami-[0-9a-f]{8,17}$", var.ami_id))
    error_message = "ami_id must be an AMI id (ami- followed by hex)."
  }
}

variable "instance_type" {
  description = <<-EOT
    Sized for concurrency, not for one run. Each instance carries kind clusters
    plus a full brain-go stack, since BRAIN_API_URL defaults to localhost:8001.

    Measured on r7a.24xlarge during run-20260902-0606 at three slots: 2.6% of
    96 vCPU, 3.0% of 739 GB, and 105 GB of a 484 GB root. Per slot that is
    ~0.83 cores, ~7.3 GB and ~35 GB of disk, so that box was oversized by
    roughly 30x on CPU and memory.

    run.tfvars asks for three slots per box, the same width those figures were
    taken at, so they carry over directly: ~2.5 cores of 32 vCPU, ~22 of 128
    GiB, and ~105 GB of the 500 GB root. The boot-time peak, when every slot
    builds its kind cluster at once, measured 13.7% of 96 vCPU at three slots,
    which is ~13 vCPU, or 41% here. That figure is a floor rather than the
    peak: CloudWatch averages over five minutes, so a shorter burst reads
    lower than it was.

    Seven slots on this box lost every run. run-20260903-0822 failed 63 of 63
    at deploy, 39 reporting ImagePullBackOff, and no problem reached its agent.
    Seven slots put 28 containerd stores on one EBS volume against the 12 that
    0606 proved, and disk_gb's own note records that ENOSPC surfaces as
    ErrImagePull rather than as a disk error. Four boxes at three slots holds
    12 stores per volume, because each box carries its own. Widen a box only
    against a density measurement taken on this instance type, which is what
    the density scenario is for; do not scale a figure taken on a larger one,
    which is the mistake that produced the seven-slot shape.

    m8i and not m8id. The d families carry a local NVMe device that nothing
    mounts today (#49), so the premium buys an idle disk while the run still
    fills the EBS root. Revisit once #49 lands, which is also what lifts the
    disk ceiling. That ceiling was put at ~13 slots from capacity alone, 500 GB
    over ~35 GB per slot. Seven slots failing well under 245 GB says contention
    binds first, so treat 13 as arithmetic rather than as a measured limit.
  EOT
  type        = string
  default     = "m8i.8xlarge"
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
  description = "Where the fleet and its results bucket go. Bedrock quota is the real constraint here, not latency."
  type        = string
  default     = "us-east-2"

  # The default was us-east-1 and its description asserted a reason for that
  # value. Every run has in fact been in us-east-2, and
  # sregym-results/archive_run.sh in the brain-go checkout defaults there too,
  # so omitting this var put the bucket in a region the archiver does not look
  # in. The symptom was "no bucket matching" after the run was already billed.
  #
  # scripts/benchmark/setup_brain_go.sh keeps its own us-east-1 fallback for a
  # direct invocation. The bootstrap always passes AWS_DEFAULT_REGION
  # explicitly, so that fallback is unreachable on the real path.
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

  validation {
    condition     = can(regex("^[A-Za-z0-9._/-]+$", var.sregym_ref))
    error_message = "sregym_ref must match ^[A-Za-z0-9._/-]+$, which covers a branch, a tag and a full SHA."
  }
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
  description = <<-EOT
    Existing bucket for results. Empty creates one owned by this config, named
    from the account so it is stable across runs, holding every run under its
    own <run-id>/ prefix.

    Setting this is not the way to fix a bucket problem. Every resource in
    s3.tf is gated on this being empty, so a value here drops the
    public-access block, the AES256 default, the versioning and the
    noncurrent-version expiry along with the bucket itself, leaving all four to
    whoever created it. Use it only for a bucket that already has them.
  EOT
  type        = string
  default     = ""
}

variable "registry_cache_bucket" {
  description = <<-EOT
    The S3 bucket every benchmark box's registry mirror reads the pre-seeded
    image cache from (NOFireAI/sregym#150). Unlike results_bucket above, this
    is not a bring-your-own override of an otherwise-managed default: this
    config never creates this bucket itself. It is permanent, account-wide
    infrastructure managed in NOFireAI/terraform's aws/sandbox/sregym, seeded
    once by scripts/benchmark/seed_image_cache.sh from a developer machine,
    and only ever read by a box, never written to. See terraform/README.md
    for the apply and seed order.
  EOT
  type        = string
  default     = "sregym-bench-registry-cache-819709950457"

  validation {
    condition     = length(var.registry_cache_bucket) > 0
    error_message = "registry_cache_bucket must not be empty: setup_brain_go.sh's host-image gate treats an empty value as \"no bucket was ever configured\" and fails the box rather than silently falling back to Docker Hub."
  }
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
