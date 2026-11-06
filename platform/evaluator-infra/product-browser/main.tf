# Independent state for browser fixture retirement; existing evaluator trust is not owned here.
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
    environment         = string
    checks_role_id      = string
    internal_invoke_url = string
  })
  validation {
    condition = (
      can(regex("^[0-9]{12}$", var.config.account)) &&
      can(regex("^[a-z][a-z0-9-]{0,23}$", var.config.environment)) &&
      can(regex("^AROA[A-Z0-9]{16,32}$", var.config.checks_role_id)) &&
      var.config.region == "us-east-1"
    )
    error_message = "Select an exact account, environment, current browser role ID and supported region."
  }
}

provider "aws" {
  region              = var.config.region
  allowed_account_ids = [var.config.account]
}

data "aws_iam_role" "checks" {
  name = "adp-${var.config.environment}-browser-trusted-checks"
}

data "aws_iam_role" "gateway" {
  name = "adp-${var.config.environment}-role-gateway-service"
}

locals {
  api = regexall("^https://([a-z0-9]+)\\.execute-api\\.${var.config.region}\\.amazonaws\\.com/([A-Za-z0-9_-]+)$", var.config.internal_invoke_url)
}

resource "aws_iam_role" "retirement" {
  name                 = "adp-${var.config.environment}-product-browser-retirement"
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow"
    Action    = "sts:AssumeRole"
    Principal = { AWS = data.aws_iam_role.checks.arn }
  }] })
  tags = { Project = "adp", Environment = var.config.environment, Purpose = "product-browser-retirement", ManagedBy = "terraform" }
  lifecycle {
    precondition {
      condition = (
        data.aws_iam_role.checks.arn == "arn:aws:iam::${var.config.account}:role/adp-${var.config.environment}-browser-trusted-checks" &&
        data.aws_iam_role.checks.unique_id == var.config.checks_role_id &&
        data.aws_iam_role.gateway.arn == "arn:aws:iam::${var.config.account}:role/adp-${var.config.environment}-role-gateway-service" &&
        length(local.api) == 1
      )
      error_message = "Browser/Gateway identities or the protected regional invoke origin differ from the selected target."
    }
  }
}

resource "aws_iam_role_policy" "retire" {
  name = "owned-product-retirement-only"
  role = aws_iam_role.retirement.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow"
    Action   = ["execute-api:Invoke"]
    Resource = "arn:aws:execute-api:${var.config.region}:${var.config.account}:${one(local.api)[0]}/${one(local.api)[1]}/POST/internal/v1/product-evaluation/retire"
  }] })
}

resource "aws_iam_role_policy" "checks_assume" {
  name = "product-browser-retirement"
  role = data.aws_iam_role.checks.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow"
    Action   = ["sts:AssumeRole"]
    Resource = aws_iam_role.retirement.arn
  }] })
}

resource "aws_iam_role_policy" "gateway_identity" {
  name = "product-browser-retirement-identity"
  role = data.aws_iam_role.gateway.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow"
    Action   = ["iam:GetRole"]
    Resource = aws_iam_role.retirement.arn
  }] })
}

output "registration_item" {
  value = jsonencode({
    agent_id          = { S = "00000000-0000-0000-0000-000008243002" }
    role_arn          = { S = aws_iam_role.retirement.arn }
    iam_role_id       = { S = aws_iam_role.retirement.unique_id }
    agent_name        = { S = "product-browser-retirement" }
    org_id            = { S = "__product_evaluation__" }
    team_id           = { S = "__platform__" }
    scope             = { S = "internal" }
    owner             = { S = "product-browser-retirement-v1" }
    status            = { S = "active" }
    credential_scopes = { SS = ["internal:product-fixture:retire"] }
  })
}

output "runtime_config" {
  value = { browser-retirement-role-arn = aws_iam_role.retirement.arn }
}
