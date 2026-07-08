# =============================================================================
# GitLab CE Infrastructure — IAM
# =============================================================================
# Instance profile with permissions for:
# - S3 backup writes
# - SSM Session Manager access
# - CloudWatch Logs/Metrics
# =============================================================================

# -----------------------------------------------------------------------------
# Instance Role
# -----------------------------------------------------------------------------

resource "aws_iam_role" "gitlab" {
  name = "${local.name_prefix}-instance"
  path = "/"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-instance"
    Service = "iam"
  })
}

# -----------------------------------------------------------------------------
# Instance Profile
# -----------------------------------------------------------------------------

resource "aws_iam_instance_profile" "gitlab" {
  name = "${local.name_prefix}-instance"
  role = aws_iam_role.gitlab.name

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-instance"
  })
}

# -----------------------------------------------------------------------------
# SSM Managed Instance Core (Session Manager access)
# -----------------------------------------------------------------------------

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.gitlab.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# -----------------------------------------------------------------------------
# CloudWatch Agent (logs + metrics)
# -----------------------------------------------------------------------------

resource "aws_iam_role_policy_attachment" "cloudwatch" {
  role       = aws_iam_role.gitlab.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

# -----------------------------------------------------------------------------
# S3 Backup Write Policy
# -----------------------------------------------------------------------------

resource "aws_iam_role_policy" "s3_backup" {
  count = var.backup_s3_bucket_name != "" ? 1 : 0

  name = "${local.name_prefix}-s3-backup"
  role = aws_iam_role.gitlab.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "GitLabBackupWrite"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:ListBucket",
          "s3:DeleteObject"
        ]
        Resource = [
          "arn:aws:s3:::${var.backup_s3_bucket_name}",
          "arn:aws:s3:::${var.backup_s3_bucket_name}/*"
        ]
      }
    ]
  })
}
