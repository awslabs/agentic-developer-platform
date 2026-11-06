terraform {
  required_version = ">= 1.7"
  backend "s3" {}
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Select the reviewed build account."
  }
}
variable "aws_region" {
  type = string
}

provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.account_id]
}

locals {
  name = "adp-dev-gitlab-runner-image-builder"
  tags = { Project = "adp", Environment = "dev", Module = "gitlab-runner-image-builder", ManagedBy = "terraform" }
}

# This profile belongs only to disposable trusted image builders. The S2 job
# launch templates deliberately have no instance profile.
resource "aws_iam_role" "image_builder" {
  name                 = local.name
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
  tags = local.tags
}

resource "aws_iam_instance_profile" "image_builder" {
  name = local.name
  role = aws_iam_role.image_builder.name
  tags = local.tags
}

resource "aws_iam_role_policy" "image_builder" {
  name = "ssm-and-reviewed-job-images"
  role = aws_iam_role.image_builder.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid = "OwnSessionTransport", Effect = "Allow", Resource = "*"
        Action = [
          "ssm:UpdateInstanceInformation",
          "ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel",
          "ec2messages:AcknowledgeMessage", "ec2messages:DeleteMessage", "ec2messages:FailMessage",
          "ec2messages:GetEndpoint", "ec2messages:GetMessages", "ec2messages:SendReply"
        ]
        Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      },
      {
        Sid       = "RegistryAuthentication", Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      },
      {
        Sid    = "QualifiedJobImagesOnly", Effect = "Allow"
        Action = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = [for name in ["job", "helper"] :
          "arn:aws:ecr:${var.aws_region}:${var.account_id}:repository/adp-gitlab-runner-${name}"
        ]
      }
    ]
  })
}

output "builder_role_arn" {
  value = aws_iam_role.image_builder.arn
}
output "builder_instance_profile_name" {
  value = aws_iam_instance_profile.image_builder.name
}
