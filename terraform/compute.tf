# One instance per shard. Spot, because this is interruptible batch work where a
# killed run resumes from S3: r7a.24xlarge has been running around $1.43 spot
# against $7.30 on-demand in us-east-1.
#
# Deliberately aws_instance with count rather than an Auto Scaling group. An ASG
# would fight the self-termination: it exists to hold capacity, while these
# instances are supposed to die when their shard is done. Replacing a killed
# instance is a `terraform apply`, and the replacement resumes from S3 rather
# than restarting the shard.

resource "aws_instance" "bench" {
  count = var.instance_count

  ami           = data.aws_ssm_parameter.ubuntu.value
  instance_type = var.instance_type
  subnet_id     = element(data.aws_subnets.default.ids, count.index)

  vpc_security_group_ids = [aws_security_group.bench.id]
  iam_instance_profile   = local.instance_profile_name

  instance_market_options {
    market_type = "spot"

    spot_options {
      # "terminate" rather than "stop": a stopped instance keeps billing for its
      # EBS volume and we would rather lose it and resume from S3.
      instance_interruption_behavior = "terminate"
      spot_instance_type             = "one-time"
      max_price                      = var.spot_max_price != "" ? var.spot_max_price : null
    }
  }

  # Belt to the user-data's braces: the shard script terminates the instance
  # when it finishes, and this makes a plain `shutdown -h` fatal too.
  instance_initiated_shutdown_behavior = "terminate"

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

  user_data = templatefile("${path.module}/templates/bootstrap.sh.tftpl", {
    run_id                = local.run_id
    shard_index           = count.index
    shard_count           = var.instance_count
    region                = var.region
    bucket                = local.bucket
    sregym_ref            = var.sregym_ref
    brain_go_ref          = var.brain_go_ref
    github_token_ssm_path = var.github_token_ssm_path
    problem_set           = var.problem_set
    n_attempts            = var.n_attempts
    noise                 = var.noise
    agent_model           = var.agent_model
    judge_model           = var.judge_model
    agent_timeout_seconds = var.agent_timeout_seconds
    runs_per_instance     = var.runs_per_instance
    max_runtime_hours     = var.max_runtime_hours
    keep_alive_on_failure = var.keep_alive_on_failure
  })

  tags = {
    Name       = "${local.name}-${count.index}"
    RunId      = local.run_id
    ShardIndex = tostring(count.index)
  }

  depends_on = [terraform_data.judge_model_guard]
}
