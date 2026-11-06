terraform {
  required_version = ">= 1.9"
  backend "s3" {}
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "An exact reviewed account is required."
  }
}
variable "aws_region" { type = string }

provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.account_id]
}

locals {
  bucket  = "adp-terraform-state-${var.account_id}"
  project = "adp-dev-gitlab-runner-bootstrap"
  role    = "adp-dev-codebuild-gitlab-runner-bootstrap"
  repos   = toset(["bootstrap", "helper", "job"])
  tags    = { Project = "adp", Environment = "dev", Module = "gitlab-runner-build", ManagedBy = "terraform" }
}

data "aws_iam_policy" "existing_boundary" {
  arn = "arn:aws:iam::${var.account_id}:policy/adp-dev-codebuild-boundary"
}

resource "aws_ecr_repository" "runtime" {
  for_each             = local.repos
  name                 = "adp-gitlab-runner-${each.key}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
  lifecycle { prevent_destroy = true }
  tags = local.tags
}

resource "aws_iam_role" "builder" {
  name                 = local.role
  permissions_boundary = data.aws_iam_policy.existing_boundary.arn
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "codebuild.amazonaws.com" },
    Condition = { StringEquals = {
      "aws:SourceAccount" = var.account_id,
      "aws:SourceArn"     = "arn:aws:codebuild:${var.aws_region}:${var.account_id}:project/${local.project}"
    } }
  }] })
  tags = local.tags
}

resource "aws_iam_role_policy" "builder" {
  name = "build-scope"
  role = aws_iam_role.builder.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Sid = "OwnLogs", Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      Resource = [
        "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/codebuild/${local.project}",
        "arn:aws:logs:${var.aws_region}:${var.account_id}:log-group:/aws/codebuild/${local.project}:*"
      ]
    },
    {
      Sid      = "ReviewedSource", Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"],
      Resource = "arn:aws:s3:::${local.bucket}/codebuild/src/${local.project}/*"
    },
    {
      Sid      = "PinnedConnector", Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"],
      Resource = "arn:aws:s3:::${local.bucket}/gitlab-runners/upstream/*"
    },
    {
      Sid      = "BuildReceipt", Effect = "Allow", Action = ["s3:PutObject"],
      Resource = "arn:aws:s3:::${local.bucket}/gitlab-runners/artifacts/*"
    },
    {
      Sid = "RegistryAuth", Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*"
    },
    {
      Sid = "OwnImages", Effect = "Allow",
      Action = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
        "ecr:DescribeImages", "ecr:DescribeRepositories", "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload", "ecr:ListImages", "ecr:PutImage", "ecr:UploadLayerPart"],
      Resource = [for repo in local.repos : "arn:aws:ecr:${var.aws_region}:${var.account_id}:repository/adp-gitlab-runner-${repo}"]
    },
    {
      Sid       = "RegionalAmiInspect", Effect = "Allow", Action = ["ec2:DescribeImages"], Resource = "*",
      Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
    },
    {
      Sid = "CallerIdentity", Effect = "Allow", Action = ["sts:GetCallerIdentity"], Resource = "*"
    }
  ] })
}

resource "aws_codebuild_project" "builder" {
  name          = local.project
  service_role  = aws_iam_role.builder.arn
  build_timeout = 60
  artifacts { type = "NO_ARTIFACTS" }
  source {
    type      = "S3"
    location  = "${local.bucket}/codebuild/src/${local.project}/explicit-source-required.zip"
    buildspec = "codebuild/bs-gitlab-runner-bootstrap.yml"
  }
  environment {
    type                        = "LINUX_CONTAINER"
    image                       = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    compute_type                = "BUILD_GENERAL1_MEDIUM"
    privileged_mode             = true
    image_pull_credentials_type = "CODEBUILD"
  }
  logs_config {
    cloudwatch_logs { group_name = "/aws/codebuild/${local.project}" }
  }
  tags       = local.tags
  depends_on = [aws_iam_role_policy.builder, aws_ecr_repository.runtime]
}

output "project_name" { value = aws_codebuild_project.builder.name }
output "builder_role_arn" { value = aws_iam_role.builder.arn }
