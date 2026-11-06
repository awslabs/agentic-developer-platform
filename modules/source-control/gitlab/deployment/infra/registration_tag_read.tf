variable "registration_tag_read" {
  description = "Read EC2 tags during the instance provider's post-update launch-template discovery."
  type        = bool
  default     = false
}

resource "aws_iam_role_policy" "registration_tag_read" {
  count = var.registration_tag_read ? 1 : 0
  name  = "adp-${var.environment}-gitlab-registration-tag-read"
  role  = aws_iam_role.deployment.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "ReadRegionalInstanceTags"
      Effect    = "Allow"
      Action    = "ec2:DescribeTags"
      Resource  = "*"
      Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
    }]
  })
}
