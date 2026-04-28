# =============================================================================
# ECR Repository — Cyber Worker Image
# =============================================================================
# Issue #231: Shared Docker image for triage + static-analysis ScaledJobs.
# Standalone resource (not using the shared ECR module) because the cyber
# module has its own Terraform state and lifecycle rules differ (7-day
# untagged expiry, keep last 10 tagged).
# =============================================================================

resource "aws_ecr_repository" "cyber_worker" {
  name                 = "adp-cyber-worker"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = {
    Component = "cyber-sandbox"
  }
}

resource "aws_ecr_lifecycle_policy" "cyber_worker" {
  repository = aws_ecr_repository.cyber_worker.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images older than 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep last 10 tagged images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = { type = "expire" }
      }
    ]
  })
}

output "cyber_worker_ecr_arn" {
  description = "ARN of the adp-cyber-worker ECR repository"
  value       = aws_ecr_repository.cyber_worker.arn
}

output "cyber_worker_ecr_url" {
  description = "URL of the adp-cyber-worker ECR repository"
  value       = aws_ecr_repository.cyber_worker.repository_url
}
