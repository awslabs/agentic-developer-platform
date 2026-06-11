###############################################################################
# S3 Files Module — S3 bucket + Mountpoint for Amazon S3 CSI driver
#
# Creates an S3 bucket and installs the Mountpoint for Amazon S3 CSI driver
# so pods can mount the bucket directly as a POSIX filesystem. Replaces the
# previous EFS-over-S3 overlay with a simpler, cheaper architecture.
#
# Mountpoint semantics:
#   - New file creation: supported (sequential writes)
#   - Full-object overwrite: supported (with --allow-overwrite mount option)
#   - Random/partial writes: NOT supported
#   - File locking: NOT supported
#   - Reads: fully supported, high throughput
#
# Writers MUST produce complete artifacts as new files (or full replacements).
# Git clones and in-progress builds belong on emptyDir scratch, not here.
###############################################################################

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  # Extract OIDC provider ID from the URL (strip https://)
  oidc_provider_id = replace(var.oidc_provider_url, "https://", "")
}

# ─── S3 Bucket ──────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "platform_data" {
  bucket = var.bucket_name

  tags = merge(var.tags, {
    Name = var.bucket_name
  })

  # Prevent accidental deletion — data is persistent and valuable
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "platform_data" {
  bucket = aws_s3_bucket.platform_data.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "platform_data" {
  bucket = aws_s3_bucket.platform_data.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "platform_data" {
  bucket = aws_s3_bucket.platform_data.id

  rule {
    id     = "transition-old-versions"
    status = "Enabled"

    noncurrent_version_transition {
      noncurrent_days = var.glacier_transition_days
      storage_class   = "GLACIER"
    }

    noncurrent_version_expiration {
      noncurrent_days = 365
    }
  }

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_public_access_block" "platform_data" {
  bucket = aws_s3_bucket.platform_data.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ─── IAM Role for Mountpoint S3 CSI Driver (IRSA) ────────────────────────────

data "aws_iam_policy_document" "s3_csi_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:oidc-provider/${local.oidc_provider_id}"]
    }

    condition {
      test     = "StringLike"
      variable = "${local.oidc_provider_id}:sub"
      values   = ["system:serviceaccount:kube-system:s3-csi-*"]
    }

    condition {
      test     = "StringLike"
      variable = "${local.oidc_provider_id}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "s3_csi_controller" {
  name               = "${var.cluster_name}-s3-csi-controller"
  assume_role_policy = data.aws_iam_policy_document.s3_csi_assume.json

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-s3-csi-controller"
  })
}

# S3 access policy scoped to the platform-data bucket
data "aws_iam_policy_document" "s3_csi_access" {
  statement {
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = [
      aws_s3_bucket.platform_data.arn,
      "${aws_s3_bucket.platform_data.arn}/*",
    ]
  }
}

resource "aws_iam_policy" "s3_csi_access" {
  name   = "${var.cluster_name}-mountpoint-s3-access"
  policy = data.aws_iam_policy_document.s3_csi_access.json

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-mountpoint-s3-access"
  })
}

resource "aws_iam_role_policy_attachment" "s3_csi_access" {
  role       = aws_iam_role.s3_csi_controller.name
  policy_arn = aws_iam_policy.s3_csi_access.arn
}

# ─── Mountpoint for Amazon S3 CSI Driver EKS Add-on ─────────────────────────

resource "aws_eks_addon" "mountpoint_s3_csi_driver" {
  cluster_name                = var.cluster_name
  addon_name                  = "aws-mountpoint-s3-csi-driver"
  addon_version               = var.mountpoint_s3_csi_driver_version
  service_account_role_arn    = aws_iam_role.s3_csi_controller.arn
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  tags = merge(var.tags, {
    Name = "mountpoint-s3-csi-driver"
  })

  depends_on = [
    aws_iam_role_policy_attachment.s3_csi_access,
  ]
}

# ─── S3 Bucket Prefixes (directory markers for Knowledge Layer) ──────────────

resource "aws_s3_object" "prefix_zoekt_shards" {
  bucket  = aws_s3_bucket.platform_data.id
  key     = "zoekt-shards/"
  content = ""
}

resource "aws_s3_object" "prefix_code_indexes" {
  bucket  = aws_s3_bucket.platform_data.id
  key     = "code-indexes/"
  content = ""
}

resource "aws_s3_object" "prefix_wikis" {
  bucket  = aws_s3_bucket.platform_data.id
  key     = "wikis/"
  content = ""
}

resource "aws_s3_object" "prefix_sbom" {
  bucket  = aws_s3_bucket.platform_data.id
  key     = "sbom/"
  content = ""
}

resource "aws_s3_object" "prefix_learning" {
  bucket  = aws_s3_bucket.platform_data.id
  key     = "learning/"
  content = ""
}
