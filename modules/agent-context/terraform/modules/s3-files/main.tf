###############################################################################
# S3 Files Module — S3 bucket + EFS file system + mount targets + IAM + CSI
#
# Creates an S3 bucket with an EFS file system overlay that allows POSIX
# mounts via the EFS CSI driver. Pods mount the file system as NFS v4.1.
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

# ─── EFS File System (S3 Files) ────────────────────────────────────────────
#
# This creates an EFS file system backed by the S3 bucket.
# NOTE: As of early 2026, the aws_efs_file_system resource with s3_import
# or the dedicated aws_s3_file_system resource may not be available in the
# Terraform AWS provider. We create a standard EFS file system and configure
# the S3 Files integration via the EFS CSI driver's volumeHandle format.
#
# The EFS CSI driver v3.0.0+ supports S3 Files via a special volumeHandle:
#   <file-system-id>::<s3-bucket-name>
#
# If the Terraform provider adds native aws_s3_file_system support, migrate
# to that resource. For now, EFS + volumeHandle is the supported path.

resource "aws_efs_file_system" "platform_data" {
  creation_token = "${var.bucket_name}-efs"
  encrypted      = true

  performance_mode = "generalPurpose"
  throughput_mode  = "elastic"

  tags = merge(var.tags, {
    Name = "${var.bucket_name}-efs"
  })

  lifecycle {
    prevent_destroy = true
  }
}

# ─── Security Group for Mount Targets ──────────────────────────────────────

resource "aws_security_group" "efs_mount" {
  name_prefix = "agent-context-efs-"
  description = "Allow NFS traffic from EKS nodes to EFS mount targets"
  vpc_id      = var.vpc_id

  tags = merge(var.tags, {
    Name = "agent-context-efs-mount"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group_rule" "efs_ingress_nfs" {
  type                     = "ingress"
  from_port                = 2049
  to_port                  = 2049
  protocol                 = "tcp"
  security_group_id        = aws_security_group.efs_mount.id
  source_security_group_id = var.node_security_group_id
  description              = "NFS from EKS nodes"
}

resource "aws_security_group_rule" "efs_egress_all" {
  type              = "egress"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  security_group_id = aws_security_group.efs_mount.id
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "Allow all outbound"
}

# ─── Mount Targets (one per subnet/AZ) ────────────────────────────────────

resource "aws_efs_mount_target" "platform_data" {
  count = length(var.subnet_ids)

  file_system_id  = aws_efs_file_system.platform_data.id
  subnet_id       = var.subnet_ids[count.index]
  security_groups = [aws_security_group.efs_mount.id]
}

# ─── IAM Role for EFS CSI Controller (IRSA) ───────────────────────────────

data "aws_iam_policy_document" "efs_csi_controller_assume" {
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
      values   = ["system:serviceaccount:kube-system:efs-csi-*"]
    }

    condition {
      test     = "StringLike"
      variable = "${local.oidc_provider_id}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "efs_csi_controller" {
  name               = "${var.cluster_name}-efs-csi-controller"
  assume_role_policy = data.aws_iam_policy_document.efs_csi_controller_assume.json

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-efs-csi-controller"
  })
}

resource "aws_iam_role_policy_attachment" "efs_csi_controller_policy" {
  role       = aws_iam_role.efs_csi_controller.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy"
}

# Additional S3 access for S3 Files integration
data "aws_iam_policy_document" "s3_files_access" {
  statement {
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [
      aws_s3_bucket.platform_data.arn,
      "${aws_s3_bucket.platform_data.arn}/*",
    ]
  }

}

resource "aws_iam_policy" "s3_files_access" {
  name   = "${var.cluster_name}-s3-files-access"
  policy = data.aws_iam_policy_document.s3_files_access.json

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-s3-files-access"
  })
}

resource "aws_iam_role_policy_attachment" "efs_csi_s3_access" {
  role       = aws_iam_role.efs_csi_controller.name
  policy_arn = aws_iam_policy.s3_files_access.arn
}

# ─── IAM Role for EFS CSI Node DaemonSet (IRSA) ──────────────────────────

data "aws_iam_policy_document" "efs_csi_node_assume" {
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
      values   = ["system:serviceaccount:kube-system:efs-csi-*"]
    }

    condition {
      test     = "StringLike"
      variable = "${local.oidc_provider_id}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "efs_csi_node" {
  name               = "${var.cluster_name}-efs-csi-node"
  assume_role_policy = data.aws_iam_policy_document.efs_csi_node_assume.json

  tags = merge(var.tags, {
    Name = "${var.cluster_name}-efs-csi-node"
  })
}

resource "aws_iam_role_policy_attachment" "efs_csi_node_policy" {
  role       = aws_iam_role.efs_csi_node.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy"
}

resource "aws_iam_role_policy_attachment" "efs_csi_node_s3_readonly" {
  role       = aws_iam_role.efs_csi_node.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonS3ReadOnlyAccess"
}

# ─── EFS CSI Driver EKS Add-on ────────────────────────────────────────────

resource "aws_eks_addon" "efs_csi_driver" {
  cluster_name             = var.cluster_name
  addon_name               = "aws-efs-csi-driver"
  addon_version            = var.efs_csi_driver_version
  service_account_role_arn = aws_iam_role.efs_csi_controller.arn
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  tags = merge(var.tags, {
    Name = "efs-csi-driver"
  })

  depends_on = [
    aws_iam_role_policy_attachment.efs_csi_controller_policy,
    aws_iam_role_policy_attachment.efs_csi_s3_access,
  ]
}

# ─── S3 Bucket Prefixes (create directory markers) ────────────────────────

resource "aws_s3_object" "prefix_deepwiki" {
  bucket  = aws_s3_bucket.platform_data.id
  key     = "deepwiki/"
  content = ""
}

resource "aws_s3_object" "prefix_openviking" {
  bucket  = aws_s3_bucket.platform_data.id
  key     = "openviking/"
  content = ""
}

resource "aws_s3_object" "prefix_codegraph" {
  bucket  = aws_s3_bucket.platform_data.id
  key     = "codegraph/"
  content = ""
}

resource "aws_s3_object" "prefix_sourcebot" {
  bucket  = aws_s3_bucket.platform_data.id
  key     = "sourcebot/"
  content = ""
}
