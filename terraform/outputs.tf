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
  description = "Problems completed so far, across all shards."
  value       = "aws s3 ls --recursive s3://${local.bucket}/${local.run_id}/ | grep -c _results.csv"
}

output "estimated_cost_note" {
  description = "Compute is the cheap half. The tokens are the question."
  value       = "${var.instance_count}x ${var.instance_type} spot. Compute is tens of dollars; token spend is the number to watch. See NOFireAI/brain-go#2219 for what to capture."
}
