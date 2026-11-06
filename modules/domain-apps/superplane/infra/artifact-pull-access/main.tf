terraform {
  required_version = ">= 1.9"
  backend "s3" {}
  required_providers {
    aws = { source = "hashicorp/aws", version = "6.65.0" }
  }
}

variable "source_registry" {
  description = "Independently verified source account and selected policy operator."
  type = object({
    account_id        = string
    region            = string
    operator_role_arn = string
    operator_role_id  = string
  })
  validation {
    condition = (
      can(regex("^[0-9]{12}$", var.source_registry.account_id)) &&
      can(regex("^[a-z]{2}-[a-z]+-[0-9]+$", var.source_registry.region)) &&
      can(regex("^arn:aws:iam::${var.source_registry.account_id}:role/[A-Za-z0-9+=,.@_/-]+$", var.source_registry.operator_role_arn)) &&
      can(regex("^AROA[A-Z0-9]{17}$", var.source_registry.operator_role_id))
    )
    error_message = "Select an exact source account, region, IAM role ARN and immutable RoleId."
  }
}

variable "consumer" {
  description = "Verified consumer operator for reads and the sole node role receiving pull access."
  type = object({
    account_id        = string
    operator_role_arn = string
    operator_role_id  = string
    node_role_arn     = string
    node_role_id      = string
  })
  validation {
    condition = (
      can(regex("^[0-9]{12}$", var.consumer.account_id)) &&
      alltrue([for arn in [var.consumer.operator_role_arn, var.consumer.node_role_arn] :
        can(regex("^arn:aws:iam::${var.consumer.account_id}:role/[A-Za-z0-9+=,.@_/-]+$", arn))
      ]) &&
      alltrue([for id in [var.consumer.operator_role_id, var.consumer.node_role_id] :
        can(regex("^AROA[A-Z0-9]{17}$", id))
      ]) &&
      var.consumer.operator_role_arn != var.consumer.node_role_arn
    )
    error_message = "Select distinct exact consumer operator/node role ARNs and immutable RoleIds in one account."
  }
}

provider "aws" {
  region              = var.source_registry.region
  allowed_account_ids = [var.source_registry.account_id]
}

# This identity only verifies the already existing consumer role. It receives no
# repository grant and this provider owns no resources.
provider "aws" {
  alias               = "consumer"
  region              = var.source_registry.region
  allowed_account_ids = [var.consumer.account_id]
  assume_role {
    role_arn = var.consumer.operator_role_arn
  }
}

data "aws_caller_identity" "source" {}
data "aws_iam_role" "source_operator" {
  name = element(reverse(split("/", var.source_registry.operator_role_arn)), 0)
}
data "aws_ecr_repository" "skypilot" {
  name = "adp-superplane-skypilot"
}
data "aws_caller_identity" "consumer" {
  provider = aws.consumer
}
data "aws_iam_role" "consumer_operator" {
  provider = aws.consumer
  name     = element(reverse(split("/", var.consumer.operator_role_arn)), 0)
}
data "aws_iam_role" "node" {
  provider = aws.consumer
  name     = element(reverse(split("/", var.consumer.node_role_arn)), 0)
}

locals {
  repository_arn = "arn:aws:ecr:${var.source_registry.region}:${var.source_registry.account_id}:repository/adp-superplane-skypilot"
}

# The existing control-plane state retains the repository, images, lifecycle
# rules and encryption. This separate state owns only its previously absent
# repository policy; never import or overwrite another policy owner.
resource "aws_ecr_repository_policy" "skypilot_pull" {
  repository = data.aws_ecr_repository.skypilot.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "SelectedConsumerNodePull"
      Effect    = "Allow"
      Principal = { AWS = var.consumer.node_role_arn }
      Action = [
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
      ]
      # ECR scopes this document to the repository argument. An explicit ARN
      # Resource is not accepted by its repository-policy API.
    }]
  })
  lifecycle {
    prevent_destroy = true
    precondition {
      condition = (
        data.aws_caller_identity.source.account_id == var.source_registry.account_id &&
        data.aws_iam_role.source_operator.arn == var.source_registry.operator_role_arn &&
        data.aws_iam_role.source_operator.unique_id == var.source_registry.operator_role_id &&
        startswith(data.aws_caller_identity.source.user_id, "${var.source_registry.operator_role_id}:")
      )
      error_message = "Source execution identity differs from the selected operator."
    }
    precondition {
      condition = (
        var.consumer.account_id != var.source_registry.account_id &&
        data.aws_caller_identity.consumer.account_id == var.consumer.account_id &&
        data.aws_iam_role.consumer_operator.arn == var.consumer.operator_role_arn &&
        data.aws_iam_role.consumer_operator.unique_id == var.consumer.operator_role_id &&
        startswith(data.aws_caller_identity.consumer.user_id, "${var.consumer.operator_role_id}:") &&
        data.aws_iam_role.node.arn == var.consumer.node_role_arn &&
        data.aws_iam_role.node.unique_id == var.consumer.node_role_id
      )
      error_message = "Consumer operator or node role differs from the reviewed account and immutable identity."
    }
    precondition {
      condition = (
        data.aws_ecr_repository.skypilot.arn == local.repository_arn &&
        data.aws_ecr_repository.skypilot.registry_id == var.source_registry.account_id &&
        data.aws_ecr_repository.skypilot.image_tag_mutability == "IMMUTABLE"
      )
      error_message = "The existing immutable SkyPilot repository differs from the selected source."
    }
  }
}

output "pull_access" {
  value = {
    repository_arn = local.repository_arn
    node_role_arn  = var.consumer.node_role_arn
    node_role_id   = data.aws_iam_role.node.unique_id
    policy_sha256  = sha256(aws_ecr_repository_policy.skypilot_pull.policy)
  }
}
