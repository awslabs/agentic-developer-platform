# Additive grants for the retained dev evaluator; this state never owns its roles or trust.
terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  backend "s3" {}
}

variable "config" {
  type = object({
    account             = string
    region              = string
    cluster             = string
    namespace           = string
    orchestrator_role   = string
    instance_role       = string
    registry_table      = string
    internal_invoke_url = string
    repository_id       = optional(string)
  })
  validation {
    condition     = var.config.orchestrator_role == "adp-cli-uplift-eval-orchestrator" && var.config.instance_role == "adp-cli-uplift-eval-instance" && var.config.namespace == "adp-gateway"
    error_message = "This additive rollout applies only to the retained dev evaluator roles and Gateway namespace."
  }
}

provider "aws" {
  region              = var.config.region
  allowed_account_ids = [var.config.account]
}

data "aws_iam_role" "orchestrator" { name = var.config.orchestrator_role }
data "aws_iam_role" "instance" { name = var.config.instance_role }

locals {
  api = regexall("^https://([a-z0-9]+)\\.execute-api\\.${var.config.region}\\.amazonaws\\.com/([A-Za-z0-9_-]+)$", var.config.internal_invoke_url)
  role_tags = {
    Project = "adp", Environment = "dev", Purpose = "cli-uplift-eval", Issue = "5248"
  }
  instance_verified = alltrue([
    for key, value in local.role_tags : lookup(data.aws_iam_role.instance.tags, key, "") == value
  ])
  orchestrator_verified = lookup(data.aws_iam_role.orchestrator.tags, "Purpose", "") == "cli-uplift-eval"
}

resource "aws_iam_role_policy" "instance_retire" {
  name = "protected-product-fixture-retire"
  role = data.aws_iam_role.instance.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow"
    Action   = ["execute-api:Invoke"]
    Resource = "arn:aws:execute-api:${var.config.region}:${var.config.account}:${one(local.api)[0]}/${one(local.api)[1]}/POST/internal/v1/product-evaluation/retire"
  }] })
  lifecycle {
    precondition {
      condition     = local.instance_verified && length(local.api) == 1
      error_message = "Retained instance identity, tags or reviewed internal invoke URL differ."
    }
  }
}

resource "aws_iam_role_policy" "orchestrator_retire" {
  name = "protected-product-recovery-retire"
  role = data.aws_iam_role.orchestrator.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow"
    Action   = ["execute-api:Invoke"]
    Resource = "arn:aws:execute-api:${var.config.region}:${var.config.account}:${one(local.api)[0]}/${one(local.api)[1]}/POST/internal/v1/product-evaluation/retire"
  }] })
  lifecycle {
    precondition {
      condition     = local.instance_verified && local.orchestrator_verified && length(local.api) == 1
      error_message = "Retained recovery identity or reviewed internal invoke URL differs."
    }
  }
}

resource "aws_iam_role_policy" "orchestrator_product_read" {
  name = "protected-product-instance-identity"
  role = data.aws_iam_role.orchestrator.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow"
    Action   = ["iam:GetRole"]
    Resource = data.aws_iam_role.instance.arn
  }] })
  lifecycle {
    precondition {
      condition     = local.instance_verified && local.orchestrator_verified
      error_message = "Retained evaluator roles do not match their reviewed ownership tags."
    }
  }
}

resource "aws_iam_role_policy" "orchestrator_runtime_proof" {
  name = "protected-product-runtime-proof"
  role = data.aws_iam_role.orchestrator.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Effect   = "Allow"
      Action   = ["execute-api:Invoke"]
      Resource = "arn:aws:execute-api:${var.config.region}:${var.config.account}:${one(local.api)[0]}/${one(local.api)[1]}/POST/internal/v1/runtime-proof/schema"
    },
    {
      Effect   = "Allow"
      Action   = ["lambda:GetFunction"]
      Resource = "arn:aws:lambda:${var.config.region}:${var.config.account}:function:adp-dev-orchestration-tick"
    }
  ] })
  lifecycle {
    precondition {
      condition     = local.orchestrator_verified && length(local.api) == 1
      error_message = "Native observation requires the reviewed evaluator and protected internal route."
    }
  }
}

resource "aws_iam_role_policy" "orchestrator_release_read" {
  count = var.config.repository_id == null ? 0 : 1
  name  = "protected-product-release-read"
  role  = data.aws_iam_role.orchestrator.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow"
    Action   = ["s3:GetObject"]
    Resource = "arn:aws:s3:::adp-dev-deployment-evidence-${var.config.account}/deployment-evidence/v1/${var.config.repository_id}/*/*/release/gateway-backend.json"
  }] })
  lifecycle {
    precondition {
      condition     = local.orchestrator_verified && can(regex("^[1-9][0-9]*$", var.config.repository_id))
      error_message = "Release read scope requires the numeric reviewed repository identity."
    }
  }
}

resource "aws_eks_access_policy_association" "gateway_view" {
  cluster_name  = var.config.cluster
  principal_arn = data.aws_iam_role.orchestrator.arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy"
  access_scope {
    type       = "namespace"
    namespaces = [var.config.namespace]
  }
  lifecycle {
    precondition {
      condition     = local.orchestrator_verified
      error_message = "Retained orchestrator identity differs from the reviewed role."
    }
  }
}

output "registration_item" {
  value = jsonencode({
    agent_id          = { S = "00000000-0000-0000-0000-000000008243" }
    role_arn          = { S = data.aws_iam_role.instance.arn }
    iam_role_id       = { S = data.aws_iam_role.instance.unique_id }
    agent_name        = { S = "product-evaluator-instance" }
    org_id            = { S = "__product_evaluation__" }
    team_id           = { S = "__platform__" }
    scope             = { S = "internal" }
    owner             = { S = "product-evaluator-legacy-v1" }
    status            = { S = "active" }
    credential_scopes = { SS = ["internal:product-fixture:retire"] }
  })
  precondition {
    condition     = local.instance_verified
    error_message = "Do not register an instance with mismatched ownership tags."
  }
}

output "recovery_registration_item" {
  value = jsonencode({
    agent_id          = { S = "00000000-0000-0000-0000-000008243001" }
    role_arn          = { S = data.aws_iam_role.orchestrator.arn }
    iam_role_id       = { S = data.aws_iam_role.orchestrator.unique_id }
    agent_name        = { S = "product-evaluator-recovery" }
    org_id            = { S = "__product_evaluation__" }
    team_id           = { S = "__platform__" }
    scope             = { S = "internal" }
    owner             = { S = "product-evaluator-legacy-v1" }
    status            = { S = "active" }
    credential_scopes = { SS = ["internal:product-fixture:retire"] }
  })
  precondition {
    condition     = local.instance_verified && local.orchestrator_verified
    error_message = "Do not register recovery with mismatched ownership tags."
  }
}
