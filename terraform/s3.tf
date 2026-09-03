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
