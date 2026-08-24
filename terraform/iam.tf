# Least privilege: read the clone token, read and write this run's results,
# invoke Bedrock, and be reachable over SSM. Nothing else.

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "bench" {
  name               = "${local.name}-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

# SSM Session Manager instead of SSH. No keys to distribute, no inbound rules,
# and every session is audited.
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.bench.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "bench" {
  statement {
    sid       = "ReadCloneToken"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = ["arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter${var.github_token_ssm_path}"]
  }

  statement {
    sid       = "DecryptCloneToken"
    actions   = ["kms:Decrypt"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${data.aws_region.current.name}.amazonaws.com"]
    }
  }

  statement {
    sid       = "ResultsBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:aws:s3:::${local.bucket}"]
  }

  statement {
    sid       = "ResultsObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${local.bucket}/${local.run_id}/*"]
  }

  statement {
    sid = "Bedrock"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = ["*"]
  }

  # Self-termination on shard completion, and the runtime cap. This is what
  # keeps a forgotten run from billing indefinitely, so the instance has to be
  # able to kill itself.
  statement {
    sid       = "SelfTerminate"
    actions   = ["ec2:TerminateInstances"]
    resources = ["arn:aws:ec2:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:instance/*"]

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/RunId"
      values   = [local.run_id]
    }
  }
}

resource "aws_iam_role_policy" "bench" {
  name   = "${local.name}-policy"
  role   = aws_iam_role.bench.id
  policy = data.aws_iam_policy_document.bench.json
}

resource "aws_iam_instance_profile" "bench" {
  name = "${local.name}-profile"
  role = aws_iam_role.bench.name
}
