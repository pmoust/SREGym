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

  instance_profile_name = var.instance_profile_name != "" ? var.instance_profile_name : aws_iam_instance_profile.bench[0].name

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

# Ubuntu 24.04 LTS x86_64. Canonical's SSM parameter always resolves to the
# current image, so this does not go stale the way a hardcoded AMI id does.
data "aws_ssm_parameter" "ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
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
