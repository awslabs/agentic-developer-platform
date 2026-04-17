# ECR Repositories (one per name in var.repositories)
resource "aws_ecr_repository" "main" {
  for_each             = toset(var.repositories)
  name                 = each.key
  image_tag_mutability = var.image_tag_mutability

  image_scanning_configuration {
    scan_on_push = var.scan_on_push
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = merge(var.common_tags, {
    Name    = each.key
    Service = "container-registry"
  })
}

# ECR Lifecycle Policy (one per repository)
resource "aws_ecr_lifecycle_policy" "main" {
  for_each   = aws_ecr_repository.main
  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last ${var.lifecycle_policy_rules} production images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["prod", "v"]
          countType     = "imageCountMoreThan"
          countNumber   = var.lifecycle_policy_rules
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep last 5 staging images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["staging", "test"]
          countType     = "imageCountMoreThan"
          countNumber   = 5
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 3
        description  = "Keep last 3 dev images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["dev"]
          countType     = "imageCountMoreThan"
          countNumber   = 3
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 4
        description  = "Delete untagged images older than 1 day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      }
    ]
  })
}

# ECR Repository Policy (for cross-account access if needed)
data "aws_iam_policy_document" "ecr_policy" {
  count = length(var.cross_account_arns) > 0 ? 1 : 0

  statement {
    sid    = "CrossAccountAccess"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = var.cross_account_arns
    }

    actions = [
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetRepositoryPolicy",
      "ecr:DescribeRepositories",
      "ecr:ListImages",
      "ecr:DescribeImages",
      "ecr:BatchDeleteImage",
      "ecr:GetLifecyclePolicy",
      "ecr:GetLifecyclePolicyPreview",
      "ecr:ListTagsForResource",
      "ecr:DescribeImageScanFindings"
    ]
  }

  statement {
    sid    = "LocalAccountFullAccess"
    effect = "Allow"

    principals {
      type = "AWS"
      identifiers = [
        "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
      ]
    }

    actions = ["ecr:*"]
  }
}

resource "aws_ecr_repository_policy" "main" {
  for_each   = length(var.cross_account_arns) > 0 ? aws_ecr_repository.main : {}
  repository = each.value.name
  policy     = data.aws_iam_policy_document.ecr_policy[0].json
}

# Data source for current AWS account
data "aws_caller_identity" "current" {}

# ECR Registry Scanning Configuration (registry-wide, not per-repo)
resource "aws_ecr_registry_scanning_configuration" "main" {
  scan_type = "BASIC"

  rule {
    scan_frequency = "SCAN_ON_PUSH"
    repository_filter {
      filter      = "${var.name_prefix}-*"
      filter_type = "WILDCARD"
    }
  }
}

# ECR Pull Through Cache Rules (for base images)
resource "aws_ecr_pull_through_cache_rule" "dockerhub" {
  count = var.enable_pull_through_cache ? 1 : 0

  ecr_repository_prefix = "dockerhub"
  upstream_registry_url = "registry-1.docker.io"

  credential_arn = var.dockerhub_credentials_arn != "" ? var.dockerhub_credentials_arn : null
}

resource "aws_ecr_pull_through_cache_rule" "public_ecr" {
  count = var.enable_pull_through_cache ? 1 : 0

  ecr_repository_prefix = "ecr-public"
  upstream_registry_url = "public.ecr.aws"
}

# CloudWatch Log Group for ECR (one per repo)
resource "aws_cloudwatch_log_group" "ecr_logs" {
  for_each          = aws_ecr_repository.main
  name              = "/aws/ecr/${each.value.name}"
  retention_in_days = 30

  tags = merge(var.common_tags, {
    Name    = "${each.value.name}-logs"
    Service = "container-registry"
  })
}

# EventBridge rule for ECR image pushes (per repo, if enabled)
resource "aws_cloudwatch_event_rule" "ecr_push" {
  for_each    = var.enable_event_notifications ? aws_ecr_repository.main : {}
  name        = "${each.value.name}-ecr-image-push"
  description = "Capture ECR image push events for ${each.value.name}"

  event_pattern = jsonencode({
    source      = ["aws.ecr"]
    detail-type = ["ECR Image Action"]
    detail = {
      action-type     = ["PUSH"]
      result          = ["SUCCESS"]
      repository-name = [each.value.name]
    }
  })

  tags = merge(var.common_tags, {
    Name    = "${each.value.name}-ecr-push-rule"
    Service = "container-registry"
  })
}

# EventBridge target for ECR image push notifications
resource "aws_cloudwatch_event_target" "ecr_push_sns" {
  for_each  = var.enable_event_notifications && var.sns_topic_arn != "" ? aws_ecr_repository.main : {}
  rule      = aws_cloudwatch_event_rule.ecr_push[each.key].name
  target_id = "SendToSNS"
  arn       = var.sns_topic_arn

  input_transformer {
    input_paths = {
      repository = "$.detail.repository-name"
      tag        = "$.detail.image-tag"
      region     = "$.detail.region"
    }
    input_template = "\"ECR Image pushed to repository <repository> with tag <tag> in region <region>\""
  }
}
