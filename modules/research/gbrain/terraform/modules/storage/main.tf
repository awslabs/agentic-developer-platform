# S3 bucket for gbrain brain repository
resource "aws_s3_bucket" "brain_repo" {
  bucket = "${var.name_prefix}-repo-${var.account_id}"

  tags = {
    Name = "${var.name_prefix}-repo"
  }
}

resource "aws_s3_bucket_versioning" "brain_repo" {
  bucket = aws_s3_bucket.brain_repo.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "brain_repo" {
  bucket = aws_s3_bucket.brain_repo.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "brain_repo" {
  bucket = aws_s3_bucket.brain_repo.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# ECR repository for gbrain container image
resource "aws_ecr_repository" "gbrain" {
  name                 = var.name_prefix
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "gbrain" {
  repository = aws_ecr_repository.gbrain.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 5 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = {
        type = "expire"
      }
    }]
  })
}
