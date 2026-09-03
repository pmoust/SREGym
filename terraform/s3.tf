# Results outlive the instances. This is what makes spot survivable: main.py's
# --resume counts attempts per problem_id and skips any problem that already has
# n_attempts rows, and each problem's CSV is written as it completes. So a killed
# instance costs the in-flight problem, not the shard, provided the CSVs are
# somewhere the replacement can read them. run_artifacts.py publishes to the
# local filesystem only, so that somewhere is here.

# One bucket for every run, named from the account rather than the run.
#
# It used to be "${local.name}-results", and local.name carries local.run_id,
# which derives from timestamp(). So every plan renamed the bucket, and a
# rename is a replace: each apply tried to delete the previous run's bucket
# and failed on BucketNotEmpty, because versioning means a finished run leaves
# object versions behind as well as objects. Guaranteed from the second run
# onwards, and the workaround was a paged version purge by hand before every
# launch. That is also how run-20260829-1047's logs were lost, purged to
# unblock the next apply before they had been pulled down (#48).
#
# Runs stay separated by the <run-id>/ prefix inside it, which is already how
# the layout works, so nothing downstream changes.
#
# Terraform keeps managing it rather than handing the job to var.results_bucket.
# Every resource below is gated on that variable being empty, so setting it
# would silently drop the public-access block, the encryption default, the
# versioning and the lifecycle expiry, leaving all four to whoever created the
# bucket by hand. The bug is the renaming, not the ownership.
#
# force_destroy stays false, so terraform destroy refuses while results exist.
# That is deliberate and the README says so: the instances self-terminate, so
# destroy is housekeeping for the security group rather than the thing that
# ends a run.
resource "aws_s3_bucket" "results" {
  count         = var.results_bucket == "" ? 1 : 0
  bucket        = "sregym-bench-results-${data.aws_caller_identity.current.account_id}"
  force_destroy = false # results are the point of the run

  # The tags argument is load-bearing even though default_tags already supplies
  # every value that matters. With no tags argument the provider puts this
  # resource's tags_all in the plan as a static empty map rather than "known
  # after apply", and default_tags then adds Project, ManagedBy and RunId during
  # apply. Terraform rejects that as "Provider produced inconsistent final plan
  # ... tags_all: new element has appeared" and fails the whole apply.
  #
  # network.tf carries the same note for the egress rule, which hit this first.
  # This bucket hit it later and for a different reason: RunId derives from
  # timestamp(), so default_tags differ on every plan, and unlike the instances
  # this bucket outlives the run, so every apply wants a tag update on it and
  # every apply failed on that update.
  tags = { Name = "sregym-bench-results" }
}

resource "aws_s3_bucket_public_access_block" "results" {
  count                   = var.results_bucket == "" ? 1 : 0
  bucket                  = aws_s3_bucket.results[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "results" {
  count  = var.results_bucket == "" ? 1 : 0
  bucket = aws_s3_bucket.results[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "results" {
  count  = var.results_bucket == "" ? 1 : 0
  bucket = aws_s3_bucket.results[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

# Traces and per-problem artifacts are bulky and only interesting while a run is
# being analysed. Results CSVs are small and stay.
resource "aws_s3_bucket_lifecycle_configuration" "results" {
  count  = var.results_bucket == "" ? 1 : 0
  bucket = aws_s3_bucket.results[0].id

  rule {
    id     = "expire-noncurrent"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}
