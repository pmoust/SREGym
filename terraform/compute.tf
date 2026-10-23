# One instance per shard. Spot, because this is interruptible batch work where a
# killed run resumes from S3. Both scenarios still set use_spot = false, for the
# reason run.tfvars gives. The default instance is m8i.8xlarge at $1.69/hour
# on demand in us-east-2, down from r7a.24xlarge at $7.30.
#
# Deliberately aws_instance with count rather than an Auto Scaling group. An ASG
# would fight the self-termination: it exists to hold capacity, while these
# instances are supposed to die when their shard is done. Replacing a killed
# instance is a `terraform apply`, and the replacement resumes from S3 rather
# than restarting the shard.

resource "aws_instance" "bench" {
  count = var.instance_count

  ami           = var.ami_id
  instance_type = var.instance_type

  lifecycle {
    # An AMI id is valid in one region. Caught at plan, not at RunInstances
    # after the bucket, security group and role exist.
    precondition {
      condition     = var.region == var.ami_region
      error_message = "ami_id is an image in ${var.ami_region}, not ${var.region}; set ami_id and ami_region to that region's image."
    }
  }
  subnet_id = element(data.aws_subnets.default.ids, count.index)

  vpc_security_group_ids = [aws_security_group.bench.id]
  iam_instance_profile   = local.instance_profile_name

  # Present only when use_spot is true. The block cannot be set at all for an
  # on-demand instance, so it is a dynamic block rather than a market_type
  # switch: EC2 rejects instance_market_options with anything else.
  dynamic "instance_market_options" {
    for_each = var.use_spot ? [1] : []

    content {
      market_type = "spot"

      spot_options {
        # "terminate" rather than "stop": a stopped instance keeps billing for
        # its EBS volume and we would rather lose it and resume from S3.
        instance_interruption_behavior = "terminate"
        spot_instance_type             = "one-time"
        max_price                      = var.spot_max_price != "" ? var.spot_max_price : null
      }
    }
  }

  # Set for on-demand, unset for spot, and the asymmetry matters both ways.
  #
  # Spot rejects it outright, which failed an apply after the instance had
  # already launched:
  #
  #   UnsupportedOperation: Modifying 'instanceInitiatedShutdownBehavior' is
  #   not supported for spot instances
  #
  # Nothing is lost there, because spot forces "terminate" anyway and cannot be
  # anything else. Confirmed on two of this fleet's own instances:
  # describe-instance-attribute reported "terminate" on both, neither of which
  # ever had it set.
  #
  # On-demand is the opposite: it defaults to "stop". terminate() falls back to
  # `shutdown -h now` when the API call fails, so on the default an instance
  # that took that path would stop rather than terminate and keep billing its
  # root volume, which for this fleet is 1500 GB, until someone noticed.
  instance_initiated_shutdown_behavior = var.use_spot ? null : "terminate"

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.disk_gb
    iops                  = var.disk_iops
    throughput            = var.disk_throughput_mbps
    delete_on_termination = true
    encrypted             = true
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required" # IMDSv2, and the spot interruption poller needs it
  }

  user_data_replace_on_change = true

  # Gzipped, not plain. EC2 caps user data at 16384 bytes and the bootstrap
  # crossed it: 16905 bytes rendered, 521 over, and the apply failed with
  # "expected length of user_data to be in the range (0 - 16384)" after the
  # bucket and security group had already been created. cloud-init detects the
  # gzip magic bytes and decompresses before running it, so this changes
  # nothing about what executes.
  #
  # Preferred over deleting 521 bytes of comments: the script is mostly
  # comments recording bugs that cost a fleet each to find, and trimming to
  # just under the cap would put the next addition back over it.
  #
  # Headroom is not stated here. It was, four times, and it was wrong every
  # time: someone measured, the template grew in the next commit, and the
  # figure stayed. The last stale number sat in the same comment that told the
  # reader to re-measure. So the number lives in a script instead:
  #
  #   scripts/benchmark/measure_user_data.py            # report
  #   scripts/benchmark/measure_user_data.py --check    # exit 1 if over
  #
  # It reports two scenarios, because the size depends on bucket name,
  # brain_env and SSM path length. Budget against the pessimistic row, and
  # treat any figure written in this file as hearsay.
  #
  # The margin is comfortable now only because the shard runner moved into
  # scripts/benchmark/run_shard.sh. Put new bootstrap logic there, not here.
  #
  # base64 is the conservative reading of the cap and the one the script
  # reports. The recorded failure above was on plain user_data, so it says
  # nothing about which form binds under base64gzip.
  #
  #
  # It will NOT fail at plan on a first apply. user_data_base64 depends on
  # local.run_id, which derives from random_id and timestamp(), and on a bucket
  # id that may not exist yet, so the value is unknown until apply. That is how
  # the original overflow went: the bucket and security group existed first.
  #
  user_data_base64 = base64gzip(templatefile("${path.module}/templates/bootstrap.sh.tftpl", {
    run_id                = local.run_id
    shard_index           = count.index
    shard_count           = var.instance_count
    region                = var.region
    bucket                = local.bucket
    registry_cache_bucket = local.registry_cache_bucket
    sregym_ref            = var.sregym_ref
    brain_go_ref          = var.brain_go_ref
    github_token_ssm_path = var.github_token_ssm_path
    problem_set           = var.problem_set
    problems              = join(" ", var.problems)
    n_attempts            = var.n_attempts
    noise                 = var.noise
    arm                   = var.arm
    agent_model           = var.agent_model
    judge_model           = var.judge_model
    reasoning_effort      = var.reasoning_effort
    agent_timeout_seconds = var.agent_timeout_seconds
    runs_per_instance     = var.runs_per_instance
    instance_type         = var.instance_type
    max_runtime_hours     = var.max_runtime_hours
    keep_alive_on_failure = var.keep_alive_on_failure
    brain_env             = local.brain_env_rendered
  }))

  tags = {
    Name       = "${local.name}-${count.index}"
    RunId      = local.run_id
    ShardIndex = tostring(count.index)
  }

  depends_on = [terraform_data.judge_model_guard]
}
