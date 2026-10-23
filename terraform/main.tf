terraform {
  required_version = ">= 1.5"

  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.0" }
    random = { source = "hashicorp/random", version = "~> 3.0" }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = merge({
      Project   = "sregym-benchmark"
      ManagedBy = "terraform"
      RunId     = local.run_id
    }, var.tags)
  }
}

resource "random_id" "run" {
  byte_length = 4
}

locals {
  run_id = var.run_id != "" ? var.run_id : "run-${formatdate("YYYYMMDD-hhmm", timestamp())}-${random_id.run.hex}"
  name   = "sregym-bench-${local.run_id}"

  bucket = var.results_bucket != "" ? var.results_bucket : aws_s3_bucket.results[0].id

  # Always var.registry_cache_bucket, never a resource this config creates:
  # see s3.tf for why the cache bucket moved to NOFireAI/terraform's standing
  # infrastructure instead of being declared per-run here.
  registry_cache_bucket = var.registry_cache_bucket

  instance_profile_name = var.instance_profile_name != "" ? var.instance_profile_name : aws_iam_instance_profile.bench[0].name

  # brain-go bounds one chat turn at CHAT_SERVICE_TURN_TIMEOUT, defaulting to
  # 15 minutes (NOFireAI/brain-go#2376). That is an interactive-chat ceiling
  # with no counterpart in the benchmark, and it is well below the harness
  # budget: when it fires during diagnosis the driver catches TurnFailed,
  # submits an empty answer and never attempts mitigation, so the problem
  # scores zero on both dimensions whatever the investigation was doing. It
  # cost 3 problems in the first full run and 2 in the second.
  #
  # Derived from the harness budget rather than pinned, so the harness timeout
  # stays the only binding cap at any agent_timeout_seconds. Note brain-go's
  # own HTTP write timeout is 90 minutes and severs the connection regardless,
  # so an agent_timeout_seconds above roughly 45m needs that raised too.
  brain_env_derived = {
    CHAT_SERVICE_TURN_TIMEOUT = "${var.agent_timeout_seconds * 2}s"
  }

  # var.brain_env last, so an explicit override wins while omitted keys keep
  # their derived value. A plain default on the variable would not do: a caller
  # setting one knob would replace the whole map and silently reinstate the
  # 15-minute cap.
  brain_env = merge(local.brain_env_derived, var.brain_env)

  # Rendered here rather than in the template so the template stays a shell
  # script. One KEY=VALUE per line, consumed by setup_brain_go.sh.
  brain_env_rendered = join("\n", [for k, v in local.brain_env : "${k}=${v}"])

  # The judge grading output from the same model that produced it is not a
  # measurement. Two judges agreeing is also weaker evidence than one good
  # judge: SREGym's own inter-rater table has GPT-5.4 and K2.5 agreeing with
  # each other at kappa 0.94 while agreeing with a human expert at only 0.70
  # and 0.76.
  judge_differs = var.agent_model != var.judge_model
}

resource "terraform_data" "judge_model_guard" {
  lifecycle {
    precondition {
      condition     = local.judge_differs
      error_message = "judge_model must differ from agent_model. A model grading its own output is not a measurement."
    }
  }
}

data "aws_vpc" "default" {
  default = true
}

# One subnet per AZ. Spot capacity and price vary by AZ, so spreading the fleet
# across all of them beats pinning one.
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
