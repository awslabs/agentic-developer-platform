# Crew checkpoint storage (#7902).
#
# src/crew/checkpoint_routes.py serves checkpoint save/restore only when
# ADP_CREW_CHECKPOINTS_ENABLED=true and ADP_CREW_CHECKPOINT_BUCKET names a
# bucket; src/crew/checkpoint_objects.py refuses a bucket whose versioning is
# not Enabled, issues presigned PUT/GET URLs with the gateway's own credentials
# and deletes recorded object versions. Until now no bucket existed in
# Terraform. This file declares it, grants the gateway IRSA role the exact
# operations the code performs, and publishes the parameters the deployment
# renderers read (the renderers themselves are a separate change; see the PR).
#
# Encryption is SSE-KMS with the AWS-managed `aws/s3` key by default — the
# gateway's own CMKs in kms.tf are purpose-bound (DynamoDB, CloudWatch, Secrets)
# and no S3 key is referenced in main.tf. Supply crew_checkpoint_kms_key_arn to
# use a CMK; the grant then also covers the key.

variable "crew_checkpoints_enabled" {
  type        = bool
  description = "Create the Crew checkpoint bucket, the gateway grant and the SSM parameters (#7902)."
  default     = false
}

variable "crew_checkpoint_retention_days" {
  type        = number
  description = "Days before a checkpoint object (and each noncurrent version) expires. Rendered as ADP_CREW_CHECKPOINT_RETENTION_SECONDS for the gateway."
  default     = 30

  validation {
    condition     = var.crew_checkpoint_retention_days >= 1 && floor(var.crew_checkpoint_retention_days) == var.crew_checkpoint_retention_days
    error_message = "crew_checkpoint_retention_days must be a whole number of days, at least 1."
  }
}

variable "crew_checkpoint_kms_key_arn" {
  type        = string
  description = "Optional KMS CMK ARN for the checkpoint bucket. Empty uses SSE-KMS with the AWS-managed aws/s3 key."
  default     = ""
}

locals {
  crew_checkpoint_bucket_name = "adp-${var.environment}-crew-checkpoints-${data.aws_caller_identity.current.account_id}"
  crew_checkpoint_prefix      = "crew-checkpoints/"
}

resource "aws_s3_bucket" "crew_checkpoints" {
  count  = var.crew_checkpoints_enabled ? 1 : 0
  bucket = local.crew_checkpoint_bucket_name

  tags = merge(local.common_tags, {
    Name    = local.crew_checkpoint_bucket_name
    Service = "s3"
    Purpose = "crew-checkpoints"
  })
}

# checkpoint_objects.py requires Status == "Enabled" before granting a save.
resource "aws_s3_bucket_versioning" "crew_checkpoints" {
  count  = var.crew_checkpoints_enabled ? 1 : 0
  bucket = aws_s3_bucket.crew_checkpoints[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "crew_checkpoints" {
  count  = var.crew_checkpoints_enabled ? 1 : 0
  bucket = aws_s3_bucket.crew_checkpoints[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "crew_checkpoints" {
  count  = var.crew_checkpoints_enabled ? 1 : 0
  bucket = aws_s3_bucket.crew_checkpoints[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.crew_checkpoint_kms_key_arn != "" ? var.crew_checkpoint_kms_key_arn : null
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "crew_checkpoints" {
  count  = var.crew_checkpoints_enabled ? 1 : 0
  bucket = aws_s3_bucket.crew_checkpoints[0].id

  rule {
    id     = "crew-checkpoints-retention"
    status = "Enabled"

    filter {}

    expiration {
      days = var.crew_checkpoint_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = var.crew_checkpoint_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  depends_on = [aws_s3_bucket_versioning.crew_checkpoints]
}

resource "aws_s3_bucket_policy" "crew_checkpoints" {
  count  = var.crew_checkpoints_enabled ? 1 : 0
  bucket = aws_s3_bucket.crew_checkpoints[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnforceTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.crew_checkpoints[0].arn,
          "${aws_s3_bucket.crew_checkpoints[0].arn}/*",
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.crew_checkpoints]
}

# Exactly the operations checkpoint_objects.py performs, on the prefix it uses:
# get_bucket_versioning; presigned put_object (with SHA-256 checksum) and
# get_object by VersionId; head_object / delete_object by VersionId. Granted to
# the gateway service role through the consolidated agents policy in
# gateway-role-policies.tf (aws_iam_policy.gateway_role["agents"]).
locals {
  gateway_crew_checkpoint_statements = var.crew_checkpoints_enabled ? concat(
    [
      {
        Sid      = "CrewCheckpointBucket"
        Effect   = "Allow"
        Action   = ["s3:GetBucketVersioning", "s3:GetBucketLocation"]
        Resource = aws_s3_bucket.crew_checkpoints[0].arn
      },
      {
        Sid      = "CrewCheckpointObjects"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion", "s3:DeleteObjectVersion"]
        Resource = "${aws_s3_bucket.crew_checkpoints[0].arn}/${local.crew_checkpoint_prefix}*"
      },
    ],
    var.crew_checkpoint_kms_key_arn != "" ? [
      {
        Sid      = "CrewCheckpointKms"
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource = var.crew_checkpoint_kms_key_arn
      },
    ] : []
  ) : []
}

# Read by the deployment renderers (gateway-deploy.yml / deploy-all.sh) into
# ADP_CREW_CHECKPOINTS_ENABLED, ADP_CREW_CHECKPOINT_BUCKET and
# ADP_CREW_CHECKPOINT_RETENTION_SECONDS; an absent bucket parameter renders "".
resource "aws_ssm_parameter" "crew_checkpoints_enabled" {
  name  = "/adp/${var.environment}/gateway/crew-checkpoints-enabled"
  type  = "String"
  value = tostring(var.crew_checkpoints_enabled)
}

resource "aws_ssm_parameter" "crew_checkpoint_retention_seconds" {
  name  = "/adp/${var.environment}/gateway/crew-checkpoint-retention-seconds"
  type  = "String"
  value = tostring(var.crew_checkpoint_retention_days * 86400)
}

resource "aws_ssm_parameter" "crew_checkpoint_bucket" {
  count = var.crew_checkpoints_enabled ? 1 : 0

  name  = "/adp/${var.environment}/gateway/crew-checkpoint-bucket"
  type  = "String"
  value = aws_s3_bucket.crew_checkpoints[0].bucket

  depends_on = [aws_s3_bucket_versioning.crew_checkpoints, aws_iam_role_policy_attachment.gateway_role]
}
