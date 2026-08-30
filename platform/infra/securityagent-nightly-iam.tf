# =============================================================================
# Security Agent nightly — least-privilege service role (intent #4290, U4)
# =============================================================================
# The service role the AWS Security Agent assumes to run the nightly code
# review and pentest jobs. Consumed by
# `.github/workflows/security-agent-nightly.yml`; applied via
# `platform-infra-apply.yml` (workflow_dispatch — merging does NOT apply).
#
# Held by a job that runs unattended, so the blast radius of an over-broad
# grant here is a nightly-scheduled one. Three scoping decisions follow from
# that, and each is asserted mechanically by
# `.github/scripts/tests/test_securityagent_preflight.py`:
#
#   1. Actions are enumerated, never `*`. The list is exactly the union of
#      `code_review.verbs` and `pentest.verbs` in the U0 validated profile
#      (.github/security/security-agent-profile.json), MINUS
#      CreateTargetDomain and VerifyTargetDomain: decision C-6 puts domain
#      verification in a one-time operator's hands, so the nightly must not
#      hold the ability to verify a domain it did not verify itself. It keeps
#      BatchGetTargetDomains because it must assert verificationStatus
#      == VERIFIED and fail closed.
#
#   2. S3 is scoped to the one staging bucket AND one prefix within it — not
#      `arn:aws:s3:::*`, and not the whole bucket. ListBucket cannot be
#      resource-scoped to a prefix in the Resource element (the bucket is the
#      resource), so the prefix bound is enforced with an s3:prefix
#      condition; without it, a role that may only read one prefix can still
#      enumerate every key in the bucket.
#
#   3. Logs are scoped to one named log group, created here rather than left
#      to service auto-creation, so the grant needs no `logs:CreateLogGroup`
#      and cannot name a group outside its own.
#
# The policy document lives in a standalone JSON file rather than the
# `jsonencode({...})` idiom used elsewhere in this directory. That is a
# deliberate departure: this unit's quality gate parses the policy as JSON and
# asserts the absence of wildcards. Inlining it in HCL would leave the gate
# asserting a hand-maintained copy while a different document actually
# applies — the drift class the gate exists to prevent. `templatefile` keeps
# one source of truth.
# =============================================================================

locals {
  securityagent_role_name = "${local.name_prefix}-securityagent-nightly"

  # Confines the role to the ledger's own prefix within the shared scans
  # bucket (see .github/scripts/security_agent_ledger.py, which writes
  # security-agent/runs/<date>/shard-<stage>.json). The bucket also holds
  # unrelated SARIF/SBOM scan artifacts that this role has no business
  # reading.
  securityagent_staging_prefix = "security-agent"

  securityagent_log_group_name = "/aws/securityagent/${local.name_prefix}-nightly"
}

# Created explicitly so the IAM grant can name one existing group instead of
# carrying logs:CreateLogGroup, which is inherently unscopeable to a name.
resource "aws_cloudwatch_log_group" "securityagent_nightly" {
  name              = local.securityagent_log_group_name
  retention_in_days = var.securityagent_log_retention_days

  tags = merge(local.common_tags, {
    Name    = local.securityagent_log_group_name
    Service = "securityagent"
    Purpose = "nightly-security-agent"
  })
}

data "aws_iam_policy_document" "securityagent_nightly_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["securityagent.amazonaws.com"]
    }

    # Confused-deputy guard. Without these, any other AWS account able to get
    # the Security Agent service to act on its behalf could name this role.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:securityagent:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"]
    }
  }
}

resource "aws_iam_role" "securityagent_nightly" {
  name               = local.securityagent_role_name
  description        = "Least-privilege service role for the nightly Security Agent code review and pentest jobs (#4443)"
  assume_role_policy = data.aws_iam_policy_document.securityagent_nightly_assume.json

  tags = merge(local.common_tags, {
    Name    = local.securityagent_role_name
    Service = "securityagent"
    Purpose = "nightly-security-agent"
  })
}

resource "aws_iam_role_policy" "securityagent_nightly" {
  name = "${local.securityagent_role_name}-policy"
  role = aws_iam_role.securityagent_nightly.id

  policy = templatefile("${path.module}/policies/securityagent-nightly-policy.json", {
    region         = var.aws_region
    account_id     = data.aws_caller_identity.current.account_id
    staging_bucket = module.security_scans.bucket_name
    staging_prefix = local.securityagent_staging_prefix
    log_group_name = aws_cloudwatch_log_group.securityagent_nightly.name
  })
}
