# Independent operator-owned state; never apply this with the evaluator role.
terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  backend "s3" {}
}
variable "credentials_enabled" {
  type    = bool
  default = true
}
variable "config" {
  type = object({
    account              = string, region = string, repository = string, target = string,
    prefix               = string, bucket = string, cluster = string, namespace = string,
    image_repository     = string, subnet = string, security_group = string,
    user_pool            = string, fixture_arn = string, fixture_kms_arn = string,
    instance_type        = string, destination_roles = list(string), create_oidc = bool,
    resource_environment = optional(string)
  })
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,62}$", coalesce(var.config.resource_environment, var.config.target)))
    error_message = "The AWS resource environment must be a lowercase namespace, not a parameter path."
  }
}
provider "aws" {
  region              = var.config.region
  allowed_account_ids = [var.config.account]
  default_tags {
    tags = { "adp:managed-by" = "evaluator-setup", "adp:evaluator-target" = var.config.target, "adp:evaluator-owner" = var.config.prefix }
  }
}
locals {
  c                    = var.config
  resource_environment = coalesce(local.c.resource_environment, local.c.target)
  arn                  = "arn:aws"
  region               = "${local.arn}:${local.c.region}:${local.c.account}"
  ec2                  = "arn:aws:ec2:${local.c.region}:${local.c.account}"
  group                = "adp:evaluator:${local.c.prefix}"
  oidc                 = "arn:aws:iam::${local.c.account}:oidc-provider/token.actions.githubusercontent.com"
  owner                = { StringLike = { "ssm:ResourceTag/adp:cli-uplift-eval" = "adp-e2e-*" }, StringEquals = { "ssm:ResourceTag/adp:evaluator-target" = local.c.prefix } }
  store = [
    { Effect = "Allow", Action = ["s3:GetBucketLocation"], Resource = aws_s3_bucket.state.arn },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.state.arn,
    Condition = { StringLike = { "s3:prefix" = ["cli-uplift-eval", "cli-uplift-eval/*"] } } },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:PutObjectAcl", "s3:DeleteObject"], Resource = "${aws_s3_bucket.state.arn}/cli-uplift-eval/*" },
    { Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = aws_kms_key.state.arn,
    Condition = { StringEquals = { "kms:ViaService" = "s3.${local.c.region}.amazonaws.com", "kms:CallerAccount" = local.c.account } } }
  ]
  fixture_kms = local.c.fixture_kms_arn == "" ? [] : [{
    Effect    = "Allow", Action = ["kms:Decrypt"], Resource = local.c.fixture_kms_arn,
    Condition = { StringEquals = { "kms:ViaService" = "secretsmanager.${local.c.region}.amazonaws.com" } }
  }]
}
resource "aws_iam_openid_connect_provider" "github" {
  count          = local.c.create_oidc ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  lifecycle { prevent_destroy = true }
}
resource "aws_iam_role" "evaluator" {
  count                = var.credentials_enabled ? 1 : 0
  name                 = "${local.c.prefix}-oidc"
  max_session_duration = 10800
  depends_on           = [aws_iam_openid_connect_provider.github]
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = local.oidc },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:${local.c.repository}:environment:${local.c.target}"
    } }
  }] })
}
data "aws_ssm_parameter" "gateway_internal_invoke_url" {
  count = var.credentials_enabled ? 1 : 0
  name  = "/adp/${local.resource_environment}/gateway/apigw-invoke-url"
}

data "aws_ssm_parameter" "agent_registry_table" {
  count = var.credentials_enabled ? 1 : 0
  name  = "/adp/${local.resource_environment}/gateway/agent-registry-table"
}

locals {
  gateway_internal_invoke_match = var.credentials_enabled ? regexall(
    "^https://([a-z0-9]+)\\.execute-api\\.${local.c.region}\\.amazonaws\\.com/([A-Za-z0-9_-]+)$",
    nonsensitive(data.aws_ssm_parameter.gateway_internal_invoke_url[0].value)
  ) : []
}

resource "aws_iam_role" "instance" {
  count = var.credentials_enabled ? 1 : 0
  name  = "${local.c.prefix}-instance"
  tags  = { "adp:managed-by" = "evaluator-setup" }
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" }
  }] })
}
resource "aws_iam_instance_profile" "instance" {
  count = var.credentials_enabled ? 1 : 0
  name  = "${local.c.prefix}-instance"
  role  = aws_iam_role.instance[0].name
}
resource "aws_iam_role_policy_attachment" "ssm" {
  count      = var.credentials_enabled ? 1 : 0
  role       = aws_iam_role.instance[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "instance" {
  count = var.credentials_enabled ? 1 : 0
  name  = "evaluation-bundle-and-fixture"
  role  = aws_iam_role.instance[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat(local.store, local.fixture_kms, [
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = local.c.fixture_arn }
    ], length(local.c.destination_roles) == 0 ? [] : [
    { Effect = "Allow", Action = ["sts:AssumeRole"], Resource = local.c.destination_roles }
  ]) })
}
resource "aws_iam_role_policy" "product_retire" {
  count = var.credentials_enabled ? 1 : 0
  name  = "protected-product-fixture-retire"
  role  = aws_iam_role.instance[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow"
    Action   = ["execute-api:Invoke"]
    Resource = "arn:aws:execute-api:${local.c.region}:${local.c.account}:${one(local.gateway_internal_invoke_match)[0]}/${one(local.gateway_internal_invoke_match)[1]}/POST/internal/v1/product-evaluation/retire"
  }] })
}

resource "aws_dynamodb_table_item" "product_evaluator" {
  count      = var.credentials_enabled ? 1 : 0
  table_name = nonsensitive(data.aws_ssm_parameter.agent_registry_table[0].value)
  hash_key   = "agent_id"
  item = jsonencode({
    agent_id          = { S = "00000000-0000-0000-0000-000000008243" }
    role_arn          = { S = aws_iam_role.instance[0].arn }
    iam_role_id       = { S = aws_iam_role.instance[0].unique_id }
    agent_name        = { S = "product-evaluator-instance" }
    org_id            = { S = "__product_evaluation__" }
    team_id           = { S = "__platform__" }
    scope             = { S = "internal" }
    owner             = { S = "product-evaluator-terraform-v1" }
    status            = { S = "active" }
    credential_scopes = { SS = ["internal:product-fixture:retire"] }
  })
}

resource "aws_iam_role_policy" "evaluator" {
  count = var.credentials_enabled ? 1 : 0
  name  = "bounded-evaluation-and-gateway-observer"
  role  = aws_iam_role.evaluator[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat(local.store, [
    { Sid = "Discovery", Effect = "Allow", Action = [
      "sts:GetCallerIdentity", "ec2:DescribeInstances", "ec2:DescribeInstanceStatus", "ec2:DescribeImages",
      "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups", "ec2:DescribeVpcs",
      "ssm:DescribeInstanceInformation", "ssm:GetCommandInvocation", "ssm:ListCommandInvocations", "ssm:CancelCommand"
    ], Resource = "*" },
    { Effect = "Allow", Action = ["eks:DescribeCluster"], Resource = "arn:aws:eks:${local.c.region}:${local.c.account}:cluster/${local.c.cluster}" },
    { Effect = "Allow", Action = ["ecr:DescribeImages", "ecr:DescribeRepositories"], Resource = "arn:aws:ecr:${local.c.region}:${local.c.account}:repository/${local.c.image_repository}" },
    { Effect = "Allow", Action = ["iam:GetInstanceProfile"], Resource = aws_iam_instance_profile.instance[0].arn },
    { Effect = "Allow", Action = ["iam:GetRole"], Resource = aws_iam_role.instance[0].arn },
    { Effect = "Allow", Action = ["cognito-idp:DescribeUserPool", "cognito-idp:DescribeUserPoolClient", "cognito-idp:ListUserPoolClients"], Resource = "arn:aws:cognito-idp:${local.c.region}:${local.c.account}:userpool/${local.c.user_pool}" },
    { Effect = "Allow", Action = ["secretsmanager:DescribeSecret"], Resource = local.c.fixture_arn },
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = "arn:aws:ssm:${local.c.region}::parameter/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64" },
    { Sid = "LaunchOwnedInstances", Effect = "Allow", Action = ["ec2:RunInstances"], Resource = "${local.ec2}:instance/*",
      Condition = { StringLike = { "aws:RequestTag/adp:cli-uplift-eval" = "adp-e2e-*" },
        StringEquals = { "ec2:InstanceType" = local.c.instance_type, "aws:RequestTag/adp:evaluator-target" = local.c.prefix },
    ArnEquals = { "ec2:InstanceProfile" = aws_iam_instance_profile.instance[0].arn } } },
    { Sid = "LaunchDependencies", Effect = "Allow", Action = ["ec2:RunInstances"], Resource = [
      "arn:aws:ec2:${local.c.region}::image/*", "${local.ec2}:subnet/${local.c.subnet}",
      "${local.ec2}:security-group/${local.c.security_group}", "${local.ec2}:network-interface/*", "${local.ec2}:volume/*"
    ] },
    { Effect = "Allow", Action = ["ec2:CreateTags"], Resource = "${local.ec2}:instance/*",
    Condition = { StringEquals = { "ec2:CreateAction" = "RunInstances" } } },
    { Effect = "Allow", Action = ["ec2:TerminateInstances", "ec2:StopInstances"], Resource = "${local.ec2}:instance/*",
    Condition = { StringLike = { "ec2:ResourceTag/adp:cli-uplift-eval" = "adp-e2e-*" }, StringEquals = { "ec2:ResourceTag/adp:evaluator-target" = local.c.prefix } } },
    { Effect = "Allow", Action = ["iam:PassRole"], Resource = aws_iam_role.instance[0].arn,
    Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } } },
    { Effect = "Allow", Action = ["ssm:SendCommand"], Resource = "${local.ec2}:instance/*", Condition = local.owner },
    { Effect = "Allow", Action = ["ssm:SendCommand"], Resource = "arn:aws:ssm:${local.c.region}::document/AWS-RunShellScript" }
    ], length(local.c.destination_roles) == 0 ? [] : [
    { Effect = "Allow", Action = ["sts:AssumeRole"], Resource = local.c.destination_roles }
  ]) })
}
resource "aws_kms_key" "state" {
  description         = "Evaluator recovery and bundle encryption for ${local.c.prefix}"
  enable_key_rotation = true
  lifecycle { prevent_destroy = true }
}
resource "aws_kms_alias" "state" {
  name          = "alias/${local.c.prefix}-state"
  target_key_id = aws_kms_key.state.key_id
}
resource "aws_s3_bucket" "state" {
  bucket        = local.c.bucket
  force_destroy = false
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule { object_ownership = "BucketOwnerEnforced" }
}
resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.state.arn
    }
    bucket_key_enabled = true
  }
}
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Deny", Principal = "*", Action = "s3:*",
    Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"],
    Condition = { Bool = { "aws:SecureTransport" = "false" } }
  }] })
}
resource "aws_eks_access_entry" "evaluator" {
  count             = var.credentials_enabled ? 1 : 0
  cluster_name      = local.c.cluster
  principal_arn     = aws_iam_role.evaluator[0].arn
  type              = "STANDARD"
  kubernetes_groups = [local.group]
}
resource "aws_eks_access_policy_association" "gateway_view" {
  count         = var.credentials_enabled ? 1 : 0
  cluster_name  = aws_eks_access_entry.evaluator[0].cluster_name
  principal_arn = aws_eks_access_entry.evaluator[0].principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy"
  access_scope {
    type       = "namespace"
    namespaces = [local.c.namespace]
  }
  depends_on = [aws_eks_access_entry.evaluator]
}
output "role_arn" { value = var.credentials_enabled ? aws_iam_role.evaluator[0].arn : null }
output "instance_profile" { value = var.credentials_enabled ? aws_iam_instance_profile.instance[0].name : null }
output "state_bucket" { value = aws_s3_bucket.state.id }

output "state_kms_key_id" { value = aws_kms_alias.state.name }

moved {
  from = aws_iam_role.evaluator
  to   = aws_iam_role.evaluator[0]
}

moved {
  from = aws_iam_role.instance
  to   = aws_iam_role.instance[0]
}

moved {
  from = aws_iam_instance_profile.instance
  to   = aws_iam_instance_profile.instance[0]
}

moved {
  from = aws_iam_role_policy_attachment.ssm
  to   = aws_iam_role_policy_attachment.ssm[0]
}

moved {
  from = aws_iam_role_policy.instance
  to   = aws_iam_role_policy.instance[0]
}

moved {
  from = aws_iam_role_policy.evaluator
  to   = aws_iam_role_policy.evaluator[0]
}

moved {
  from = aws_eks_access_entry.evaluator
  to   = aws_eks_access_entry.evaluator[0]
}
