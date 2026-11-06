resource "aws_ecr_repository" "tools_facade" {
  name                 = "adp-tools-facade"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "tools_facade" {
  repository = aws_ecr_repository.tools_facade.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire only untagged images after 30 days; preserve tagged rollback digests"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 30
      }
      action = { type = "expire" }
    }]
  })
}

output "tools_facade_repository_url" {
  value = aws_ecr_repository.tools_facade.repository_url
}
