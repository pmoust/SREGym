# Results outlive the instances. This is what makes spot survivable: main.py's
# --resume counts attempts per problem_id and skips any problem that already has
# n_attempts rows, and each problem's CSV is written as it completes. So a killed
# instance costs the in-flight problem, not the shard, provided the CSVs are
# somewhere the replacement can read them. run_artifacts.py publishes to the
# local filesystem only, so that somewhere is here.

resource "aws_s3_bucket" "results" {
  count         = var.results_bucket == "" ? 1 : 0
  bucket        = "${local.name}-results"
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
