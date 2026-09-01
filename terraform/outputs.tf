output "run_id" {
  description = "Names the S3 prefix and tags every resource in this run."
  value       = local.run_id
}

output "results_uri" {
  description = "Where results land. Survives the instances, which is what makes resume work."
  value       = "s3://${local.bucket}/${local.run_id}/"
}

output "instance_ids" {
  description = "One per shard."
  value       = aws_instance.bench[*].id
}

output "watch" {
  description = "Follow the bootstrap and run logs on a shard."
  value       = "aws ssm start-session --region ${var.region} --target ${try(aws_instance.bench[0].id, "<instance-id>")} --document-name AWS-StartInteractiveCommand --parameters command='tail -f /var/log/sregym-bootstrap.log'"
}

output "progress" {
  description = "Problem-runs scored so far, across all shards. Compare against problems x n_attempts."
  # Matches only the per-attempt files under a run_N directory. A bare
  # `grep -c _results.csv` counts six objects per problem: the three run_N
  # files plus a per-problem rollup, a _running_ file and an _ALL_ file. It
  # reported 126 for the 63 real problem-runs of run-20260831-1752, and the
  # figure reads plausibly enough that nobody questioned it mid-run.
  value = "aws s3 ls --recursive s3://${local.bucket}/${local.run_id}/ | grep -cE '/run_[0-9]+/[^/]*_results\\.csv$'"
}

output "estimated_cost_note" {
  description = "Compute is the cheap half. The tokens are the question."
  value       = "${var.instance_count}x ${var.instance_type} spot. Compute is tens of dollars; token spend is the number to watch. See NOFireAI/brain-go#2219 for what to capture."
}
