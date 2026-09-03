# Least privilege: read the clone token, read and write this run's results,
# invoke Bedrock, and be reachable over SSM. Nothing else.
#
# All four resources below are skipped when var.instance_profile_name is set
# (the same bring-your-own pattern as results_bucket in s3.tf). Creating and
# destroying this role on every run needs a wide IAM grant on the caller
# (CreateRole, PutRolePolicy, AttachRolePolicy, ListInstanceProfilesForRole
# for delete, ...); passing an existing profile by name needs only a single
# iam:PassRole statement scoped to that one role's ARN, since aws_instance
# makes no IAM API calls of its own -- it hands RunInstances the profile name
# as a plain string. An admin who pre-creates the role once should give it
# exactly the policy document below (data.aws_iam_policy_document.bench).

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
  count              = var.instance_profile_name == "" ? 1 : 0
  name               = "${local.name}-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json

  # Explicit tags for the reason s3.tf spells out. Unreached today, since every
  # scenario sets instance_profile_name and this is count = 0, but it would fail
  # its create the first time someone unset it.
  tags = { Name = "${local.name}-role" }
}

# SSM Session Manager instead of SSH. No keys to distribute, no inbound rules,
# and every session is audited.
resource "aws_iam_role_policy_attachment" "ssm" {
  count      = var.instance_profile_name == "" ? 1 : 0
  role       = aws_iam_role.bench[0].name
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
  count  = var.instance_profile_name == "" ? 1 : 0
  name   = "${local.name}-policy"
  role   = aws_iam_role.bench[0].id
  policy = data.aws_iam_policy_document.bench.json
}

resource "aws_iam_instance_profile" "bench" {
  count = var.instance_profile_name == "" ? 1 : 0
  name  = "${local.name}-profile"
  role  = aws_iam_role.bench[0].name

  tags = { Name = "${local.name}-profile" }
}
