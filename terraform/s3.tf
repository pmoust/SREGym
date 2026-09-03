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

  # RunId is pinned here, overriding default_tags' RunId for this resource
  # only. default_tags.RunId comes from local.run_id, which falls through to
  # timestamp() in every real launch (launch.sh never sets var.run_id), and
  # Terraform documents timestamp() as unknown until apply. That taints
  # tags_all for every resource carrying default_tags, but it only breaks
  # apply here: instances, the security group and its egress rule all get
  # destroyed and recreated each run, so apply never reconciles a stale known
  # tags_all against a newly unknown one. This bucket is the one resource
  # updated in place instead of recreated -- it is deliberately one shared
  # bucket across every run, not per-run, which is the whole point of the
  # rename fix above -- so apply does that reconciliation, and it failed it
  # outright: "Provider produced inconsistent final plan ... tags_all: new
  # element has appeared", for ManagedBy, Project and RunId together, on
  # every apply.
  #
  # A per-run RunId tag was never the right value for a bucket that holds
  # every run's results, independent of the bug it triggers. Pinning it to
  # "shared" here means this resource's own tags fully override that key, so
  # tags_all for this resource no longer depends on the unknown local.run_id
  # at all.
  # Duplicated rather than left to default_tags, and not just for RunId. The
  # first attempt pinned only RunId and still hit "Provider produced
  # inconsistent final plan ... tags_all: new element has appeared" for
  # ManagedBy and Project, which are static literals with no timestamp()
  # dependency -- so the unknown-value theory above only explains part of
  # this. aws_s3_bucket tags go through a separate tagging API from the
  # resource's main create/update call, and default_tags reaches that API by
  # a different path than the resource's own tags do; reconciling the two
  # against this bucket's pre-existing state is what fails. Giving the
  # resource its own copy of every value removes the reconciliation
  # entirely: nothing here depends on default_tags for this resource.
  tags = {
    Name      = "sregym-bench-results"
    ManagedBy = "terraform"
    Project   = "sregym-benchmark"
    RunId     = "shared"
  }
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
