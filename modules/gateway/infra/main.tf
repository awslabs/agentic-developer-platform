# =============================================================================
# Gateway Infrastructure — layers on top of the shared platform
# =============================================================================
# Gateway-specific resources (RDS, Redis, Cognito, CloudFront, S3, Lambdas,
# API Gateway, CloudWatch Dashboard). Networking, EKS, ECR, IAM base roles,
# and CloudTrail are owned by the shared platform in platform/infra/.
#
# Pattern matches modules/agent-factory/infra/main.tf.
# =============================================================================

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "adp"
      Environment = var.environment
      Module      = "gateway"
      ManagedBy   = "terraform"
      Owner       = "gateway-team"
      CostCenter  = var.cost_center
    }
  }
}

# =============================================================================
# Shared Platform Remote State
# =============================================================================
# Read outputs from the shared platform infrastructure (VPC, EKS, ECR, IAM)
# deployed via platform/infra/. This avoids duplicating networking and compute.
# =============================================================================

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "terraform_remote_state" "platform" {
  backend = "s3"
  config = {
    bucket = "adp-terraform-state-${data.aws_caller_identity.current.account_id}"
    key    = "${var.environment}/platform/terraform.tfstate"
    region = var.aws_region
  }
}

# Local values for resource naming and platform lookups
locals {
  name_prefix = "bedrockgw-${var.environment}"

  # Common tags to merge with provider default tags
  common_tags = {
    Project     = "adp"
    Environment = var.environment
    Module      = "gateway"
    ManagedBy   = "terraform"
    Owner       = "gateway-team"
    CostCenter  = var.cost_center
  }

  # Shared platform resources
  vpc_id                    = data.terraform_remote_state.platform.outputs.vpc_id
  private_subnets           = data.terraform_remote_state.platform.outputs.private_subnet_ids
  cluster_name              = data.terraform_remote_state.platform.outputs.eks_cluster_name
  cluster_endpoint          = data.terraform_remote_state.platform.outputs.eks_cluster_endpoint
  cluster_ca                = data.terraform_remote_state.platform.outputs.eks_cluster_ca_certificate
  oidc_issuer               = data.terraform_remote_state.platform.outputs.eks_oidc_issuer
  oidc_provider_arn         = data.terraform_remote_state.platform.outputs.eks_oidc_provider_arn
  ecr_gateway_url           = data.terraform_remote_state.platform.outputs.ecr_repository_urls["adp-gateway"]
  cluster_security_group_id = data.terraform_remote_state.platform.outputs.eks_cluster_security_group_id
  rds_security_group_id     = data.terraform_remote_state.platform.outputs.rds_security_group_id
  redis_security_group_id   = data.terraform_remote_state.platform.outputs.redis_security_group_id

  # Gateway service IRSA role (created by platform EKS module)
  gateway_service_irsa_role_arn  = data.terraform_remote_state.platform.outputs.gateway_service_irsa_role_arn
  gateway_service_irsa_role_name = data.terraform_remote_state.platform.outputs.gateway_service_irsa_role_name

  state_bucket         = "adp-terraform-state-${data.aws_caller_identity.current.account_id}"
  layer_builder_script = "${path.module}/../../../platform/scripts/build-lambda-layers.sh"
}

# =============================================================================
# Lambda layer builds (Issue #1038 / #408) — auto-build so NO deploy path fails
# =============================================================================
# The budget-lambda (psycopg2) and lambda-authorizer (pyjwt) modules read their
# layer zips from s3://<state-bucket>/lambda-layers/*.zip via `aws_s3_object`
# data sources that resolve at PLAN time. If the zip is absent the apply dies
# with "couldn't find resource". Previously only deploy-all.sh built these, so
# stage-by-stage `terraform apply` and CI failed.
#
# These null_resources trigger the CodeBuild layer build (via the shared
# build-lambda-layers.sh) BEFORE the consuming modules read S3. They are gated
# by the SAME feature flags as their consumers (enable_chat_logging for
# psycopg2, enable_api_gateway for pyjwt), so we never build a layer no Lambda
# will use. CodeBuild does the Docker work — no local Docker needed. Matches the
# existing null_resource + local-exec pattern used in the cognito module.
resource "null_resource" "build_psycopg2_layer" {
  count = var.enable_chat_logging ? 1 : 0

  # Rebuild when the layer's build recipe changes; the build script itself is
  # idempotent so re-running on every change is safe.
  triggers = {
    build_script = filesha256(local.layer_builder_script)
    layer_recipe = filesha256("${path.module}/../lambda/layers/psycopg2/build.sh")
    state_bucket = local.state_bucket
  }

  provisioner "local-exec" {
    command     = "bash '${local.layer_builder_script}' psycopg2"
    interpreter = ["/bin/bash", "-c"]
    environment = {
      AWS_REGION       = var.aws_region
      ENVIRONMENT      = var.environment
      ADP_STATE_BUCKET = local.state_bucket
    }
  }
}

resource "null_resource" "build_pyjwt_layer" {
  count = var.enable_api_gateway ? 1 : 0

  triggers = {
    build_script = filesha256(local.layer_builder_script)
    layer_recipe = filesha256("${path.module}/../lambda/layers/pyjwt/build.sh")
    state_bucket = local.state_bucket
  }

  provisioner "local-exec" {
    command     = "bash '${local.layer_builder_script}' pyjwt"
    interpreter = ["/bin/bash", "-c"]
    environment = {
      AWS_REGION       = var.aws_region
      ENVIRONMENT      = var.environment
      ADP_STATE_BUCKET = local.state_bucket
    }
  }
}

# =============================================================================
# Kubernetes & Helm Providers (using shared EKS cluster)
# =============================================================================

provider "kubernetes" {
  host                   = local.cluster_endpoint
  cluster_ca_certificate = base64decode(local.cluster_ca)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", local.cluster_name]
  }
}

provider "helm" {
  kubernetes {
    host                   = local.cluster_endpoint
    cluster_ca_certificate = base64decode(local.cluster_ca)

    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", local.cluster_name]
    }
  }
}

# =============================================================================
# Gateway-specific IAM Policy Extensions (Option B)
# =============================================================================
# The platform's EKS module creates the base IRSA role with Bedrock, STS,
# CloudWatch Logs, and DynamoDB permissions. Here we attach incremental
# policies that depend on gateway-specific resources (Cognito pool ID,
# RDS IAM auth, ElastiCache IAM auth, chat logs bucket, etc.).
# =============================================================================

# RDS IAM Authentication — attach to the platform IRSA role
resource "aws_iam_role_policy" "gateway_rds_iam_auth" {
  count = var.enable_rds_iam_auth ? 1 : 0
  name  = "${local.name_prefix}-policy-gateway-rds-iam-auth"
  role  = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["rds-db:connect"]
        Resource = "arn:aws:rds-db:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:dbuser:*/${var.rds_username}"
      }
    ]
  })
}

# ElastiCache IAM Authentication
resource "aws_iam_role_policy" "gateway_elasticache_iam_auth" {
  count = var.enable_redis && var.enable_elasticache_iam_auth ? 1 : 0
  name  = "${local.name_prefix}-policy-gateway-elasticache-iam-auth"
  role  = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["elasticache:Connect"]
        Resource = [
          "arn:aws:elasticache:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:replicationgroup:${module.redis[0].replication_group_id}",
          "arn:aws:elasticache:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:user:${module.redis[0].redis_iam_user_id}"
        ]
      }
    ]
  })

  depends_on = [module.redis]
}

# Cognito permissions (scoped to the gateway's Cognito pool).
# Read: onboarding identity lookup, admin group/user listing.
# Write (AdminUpdateUserAttributes): onboarding approval syncs the approved
# user's role/org onto their Cognito custom: attributes so the pre-token Lambda
# emits them in the access token (see admin/onboarding/approval.py). Without
# this the approved user logs in with an empty role/org → broken SPA nav +
# dashboard. Scoped to this pool only.
resource "aws_iam_role_policy" "gateway_cognito_read" {
  name = "${local.name_prefix}-policy-gateway-cognito-read"
  role = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CognitoReadUsers"
        Effect = "Allow"
        Action = [
          "cognito-idp:ListUsers",
          "cognito-idp:ListGroups",
          "cognito-idp:ListUsersInGroup",
          "cognito-idp:AdminGetUser",
          "cognito-idp:AdminUpdateUserAttributes"
        ]
        Resource = "arn:aws:cognito-idp:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:userpool/${module.cognito.cognito_user_pool_id}"
      },
      {
        # Native identity lifecycle (#5010). Keep every write scoped to the
        # gateway pool; GetGroup is needed for idempotent CreateGroup retries.
        Sid    = "CognitoIdentityLifecycle"
        Effect = "Allow"
        Action = [
          "cognito-idp:AdminCreateUser",
          "cognito-idp:AdminDeleteUser",
          "cognito-idp:AdminAddUserToGroup",
          "cognito-idp:AdminRemoveUserFromGroup",
          "cognito-idp:AdminListGroupsForUser",
          "cognito-idp:CreateGroup",
          "cognito-idp:GetGroup",
          "cognito-idp:DeleteGroup"
        ]
        Resource = "arn:aws:cognito-idp:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:userpool/${module.cognito.cognito_user_pool_id}"
      },
      {
        # Web CLI login (/auth/cli): mints tokens on the CLI app client the
        # same way the github-auth-broker does — fresh random permanent
        # password + admin auth. Only invoked after the signed-in browser
        # user approves, and only for broker-provisioned GitHub_* users
        # (who never hold a real password). Scoped to this pool only.
        Sid    = "CognitoCliLoginMint"
        Effect = "Allow"
        Action = [
          "cognito-idp:AdminSetUserPassword",
          "cognito-idp:AdminInitiateAuth",
          "cognito-idp:AdminRespondToAuthChallenge"
        ]
        Resource = "arn:aws:cognito-idp:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:userpool/${module.cognito.cognito_user_pool_id}"
      }
    ]
  })

  depends_on = [module.cognito]
}

# CFN template read permissions (issue #562)
# The gateway pre-signs an S3 GET URL for the CloudFormation template YAML;
# AWS Console requires templateURL to be an S3 host, so we host the template
# in the existing frontend bucket and let the gateway sign a short-lived URL.
resource "aws_iam_role_policy" "gateway_cfn_template_read" {
  name = "${local.name_prefix}-policy-gateway-cfn-template-read"
  role = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CfnTemplateGet"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${module.frontend_s3.bucket_arn}/cfn-templates/*"
      },
      {
        # The "Add AWS account" flow pre-signs a GET for the CFN template; the
        # SDK/console fetch path also performs s3:ListBucket on the bucket (a
        # bucket-level action, so it's scoped by prefix here, not the object
        # ARN). Without it the flow fails: "not authorized to perform
        # s3:ListBucket on resource arn:aws:s3:::<frontend-bucket>".
        Sid      = "CfnTemplateList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = module.frontend_s3.bucket_arn
        Condition = {
          StringLike = {
            "s3:prefix" = ["cfn-templates/*"]
          }
        }
      }
    ]
  })

  depends_on = [module.frontend_s3]
}

# S3 chat logs write permissions
resource "aws_iam_role_policy" "gateway_chat_logs_s3" {
  count = var.enable_chat_logging ? 1 : 0
  name  = "${local.name_prefix}-policy-gateway-chat-logs-s3"
  role  = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ChatLogsS3Write"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${module.s3_chat_logs[0].bucket_arn}/*"
      },
      {
        Sid      = "ChatLogsS3BucketAccess"
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation"]
        Resource = module.s3_chat_logs[0].bucket_arn
      }
    ]
  })

  depends_on = [module.s3_chat_logs]
}

# Agent run-logs transcripts — read-only (Issue #3069 / #3105)
# The transcript viewer endpoint fetches markdown transcripts from the
# agent-run-logs bucket. GetObject only — no List, no Put. Scoped to
# this account's bucket by convention (adp-<env>-agent-run-logs-<account>).
resource "aws_iam_role_policy" "gateway_run_logs_read" {
  name = "${local.name_prefix}-policy-gateway-run-logs-read"
  role = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AgentRunLogsRead"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "arn:aws:s3:::adp-${var.environment}-agent-run-logs-${data.aws_caller_identity.current.account_id}/*"
      }
    ]
  })
}

# Comprehend PII detection permissions
resource "aws_iam_role_policy" "gateway_comprehend_pii" {
  count = var.enable_chat_logging && var.chat_logging_scrub_level == "standard" ? 1 : 0
  name  = "${local.name_prefix}-policy-gateway-comprehend-pii"
  role  = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ComprehendPiiDetection"
        Effect   = "Allow"
        Action   = ["comprehend:DetectPiiEntities"]
        Resource = "*"
      }
    ]
  })
}

# Bedrock InvokeModel permissions for the bedrock-mantle / OpenAI passthrough
# (Issue #2709). The mantle passthrough route (POST /openai/v1/responses) signs
# upstream requests with SigV4 using the gateway pod's OWN IRSA credentials —
# unlike the Claude proxy path, which assumes a cross-account pool role.
#
# The route now targets AWS Bedrock's OpenAI-compatible endpoint on
# bedrock-runtime.<region>.amazonaws.com (BG_MANTLE_BASE_URL), which authorizes
# against the native bedrock:InvokeModel* actions on the inference profile and
# its underlying foundation model — covered by the "MantleBedrockInvoke"
# statement below (Resource "*"). No IAM change was needed for that migration.
#
# The earlier preview host bedrock-mantle.<region>.api.aws is its OWN service
# (prefix "bedrock-mantle:") and authorized against bedrock-mantle:CreateInference
# on the mantle project resource — NOT bedrock:InvokeModel*. Spike #2703 missed
# this because it tested from a role with AdministratorAccess attached, which
# masked the real required action; the gateway pod's own role got 401
# access_denied until this grant was added (Issue #2817). The
# "MantleCreateInference" statement below is now vestigial (the route no longer
# calls that host) but is retained harmlessly for rollback to the preview host.
resource "aws_iam_role_policy" "gateway_mantle_bedrock_invoke" {
  count = var.enable_mantle_passthrough ? 1 : 0
  name  = "${local.name_prefix}-policy-gateway-mantle-bedrock-invoke"
  role  = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "MantleBedrockInvoke"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]
        Resource = "*"
      },
      {
        Sid    = "MantleCreateInference"
        Effect = "Allow"
        Action = [
          "bedrock-mantle:CreateInference"
        ]
        Resource = "arn:aws:bedrock-mantle:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:project/default"
      }
    ]
  })
}

# X-Ray Tracing permissions
resource "aws_iam_role_policy" "gateway_xray_tracing" {
  count = var.enable_xray_tracing ? 1 : 0
  name  = "${local.name_prefix}-policy-gateway-xray-tracing"
  role  = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets"
        ]
        Resource = "*"
      }
    ]
  })
}

# Secrets Manager read access for agent Cognito credentials (Issue #33)
# The gateway (and E2E tests running in-cluster) need to read the agent
# client_credentials secret to obtain M2M tokens.
resource "aws_iam_role_policy" "gateway_secretsmanager_read" {
  name = "${local.name_prefix}-policy-gateway-secretsmanager-read"
  role = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SecretsManagerReadAgentCreds"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret"
        ]
        Resource = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:bedrockgw-*"
      }
    ]
  })

  depends_on = [module.cognito]
}

# Vault credentials — full CRUD across all 4 ownership scopes.
# The gateway is the sole writer/reader of user-vault secrets; agent pods
# have no direct Secrets Manager access and must route through
# /internal/v1/proxy-request (per docs/user-identity-and-credentials-design.md).
#
# Namespaces (issue #440 scope relaxation):
#   adp/users/<cognito_sub>/*           — user-owned
#   adp/teams/<team_id>/*               — team-owned
#   adp/orgs/<org_id>/*                 — tenant-owned
#   adp/domain-apps/<app>/<org_id>/*    — domain-app, per-tenant install
resource "aws_iam_role_policy" "gateway_vault_secrets" {
  name = "${local.name_prefix}-policy-gateway-vault-secrets"
  role = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "VaultSecretsCRUD"
        Effect = "Allow"
        Action = [
          "secretsmanager:CreateSecret",
          "secretsmanager:GetSecretValue",
          "secretsmanager:PutSecretValue",
          "secretsmanager:UpdateSecret",
          "secretsmanager:DescribeSecret",
          "secretsmanager:DeleteSecret",
          "secretsmanager:TagResource",
          "secretsmanager:UntagResource"
        ]
        Resource = [
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:adp/users/*",
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:adp/teams/*",
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:adp/orgs/*",
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:adp/domain-apps/*",
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:adp/*/github-app/*",
          # Issue #3473: per-tenant GitHub App credentials seeded by
          # connections/tenant_secret.py at adp/<env>/tenants/<org_id>/github-app.
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:adp/*/tenants/*/github-app*",
          # Issue #2708: the register flow writes the broker's OAuth client_id/
          # secret here so "Sign in with GitHub" works right after registration,
          # and get_app_status reads it to report login_enabled. Narrow to this
          # exact secret family — NOT adp/*/cognito/*.
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:adp/*/cognito/github-oauth-credentials*",
          # Issue #2824: the register flow writes the manifest-conversion
          # webhook_secret here so webhooks from a UI-registered App pass HMAC
          # validation in the webhook-ingress Lambda. Terraform seeds this secret
          # with a placeholder and never updates it. Narrow to this exact secret.
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:adp/*/webhook-ingress/github-webhook-secret*",
          # Issue #3529: The gateway resolver needs to read ops-App credentials
          # (gh-app-ops-id / gh-app-ops-key) so it resolves installation_ids
          # using the SAME App the ingestion worker mints tokens with. Without
          # this pattern, GetSecretValue returns AccessDeniedException.
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:adp/*/gh-app-ops-*"
        ]
      },
      {
        # ListSecrets is account-wide by necessity (no resource-level scoping).
        # The gateway uses it to enumerate its own vault inventory (e.g. for
        # the orphan sweeper, admin listings, and per-user quota checks).
        Sid      = "VaultSecretsList"
        Effect   = "Allow"
        Action   = ["secretsmanager:ListSecrets"]
        Resource = "*"
      }
    ]
  })
}

# Issue #2652: The github-app secrets (adp/<env>/github-app/*) are encrypted
# with the webhook-ingress CMK (alias/adp-<env>-webhook-secrets). Without
# kms:Decrypt on this key, secretsmanager:GetSecretValue returns
# AccessDeniedException even though the SecretsManager permission is granted
# above — same bug class as #2567 (webhook Lambda KMS gap).
# Issue #2797: the register-app flow also WRITES these secrets
# (PutSecretValue/CreateSecret in _store_app_credentials), which needs
# kms:GenerateDataKey*/kms:Encrypt on the same CMK — read-only KMS access made
# UI registration fail with AccessDenied once #2394 pinned the secrets to it.
# Referenced by alias so key rotation doesn't break the policy.
#
# Issue #3789: CMK moved to platform infra (always exists before gateway applies).
# The grant is now unconditional — no more enable_webhook_secrets_kms_grant flag.
data "aws_kms_alias" "webhook_secrets" {
  name = "alias/adp-${var.environment}-webhook-secrets"
}

resource "aws_iam_role_policy" "gateway_webhook_secrets_kms" {
  name = "${local.name_prefix}-policy-gateway-webhook-secrets-kms"
  role = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "WebhookSecretsKMSReadWrite"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
          "kms:Encrypt",
          "kms:GenerateDataKey*"
        ]
        Resource = [data.aws_kms_alias.webhook_secrets.target_key_arn]
      }
    ]
  })
}

# Cross-account Bedrock pool assume role (if pool accounts configured)
resource "aws_iam_role_policy" "gateway_cross_account" {
  count = length(var.pool_account_arns) > 0 ? 1 : 0
  name  = "${local.name_prefix}-policy-gateway-cross-account"
  role  = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sts:AssumeRole"]
        Resource = [for acct in var.pool_account_arns : "arn:aws:iam::${acct}:role/*BedrockGateway-Pool*"]
      }
    ]
  })
}

# =============================================================================
# S3 Chat Logs Module (Issue #143)
# =============================================================================

module "s3_chat_logs" {
  count  = var.enable_chat_logging ? 1 : 0
  source = "./modules/s3-chat-logs"

  environment     = var.environment
  name_prefix     = local.name_prefix
  account_id      = data.aws_caller_identity.current.account_id
  common_tags     = local.common_tags
  kms_key_arn     = var.chat_logging_kms_key_arn
  log_bucket_name = "" # Optional: set to access log bucket if needed
}

# =============================================================================
# RDS Module
# =============================================================================

module "rds" {
  source = "./modules/rds"

  environment             = var.environment
  name_prefix             = local.name_prefix
  common_tags             = local.common_tags
  vpc_id                  = local.vpc_id
  private_subnet_ids      = local.private_subnets
  rds_security_group_id   = local.rds_security_group_id
  instance_class          = var.rds_instance_class
  allocated_storage       = var.rds_allocated_storage
  max_allocated_storage   = var.rds_max_allocated_storage
  multi_az                = var.rds_multi_az
  backup_retention_period = var.rds_backup_retention_period
  backup_window           = var.rds_backup_window
  maintenance_window      = var.rds_maintenance_window
  db_name                 = var.rds_db_name
  username                = var.rds_username

  cloudwatch_kms_key_arn = aws_kms_key.cloudwatch.arn
}

# =============================================================================
# ElastiCache Redis Module (optional)
# =============================================================================

module "redis" {
  count  = var.enable_redis ? 1 : 0
  source = "./modules/redis"

  environment             = var.environment
  name_prefix             = local.name_prefix
  common_tags             = local.common_tags
  vpc_id                  = local.vpc_id
  private_subnet_ids      = local.private_subnets
  redis_security_group_id = local.redis_security_group_id
  node_type               = var.redis_node_type
  num_cache_nodes         = var.redis_num_cache_nodes
  parameter_group_name    = var.redis_parameter_group_name
  port                    = var.redis_port
}

# Note: ALB is managed by the EKS Ingress controller (AWS Load Balancer Controller).
# The Ingress resource in k8s/ingress.yaml creates and manages the ALB automatically.
# CloudFront's ALB origin is updated by the backend-deploy workflow after the
# Ingress ALB is created, since its DNS name is dynamic.

# =============================================================================
# Cognito Module for authentication
# =============================================================================

module "cognito" {
  source = "./modules/cognito"

  environment       = var.environment
  name_prefix       = local.name_prefix
  common_tags       = local.common_tags
  mfa_configuration = var.cognito_mfa_configuration
  callback_urls = concat(
    var.cognito_callback_urls,
    ["https://${module.cloudfront.distribution_domain_name}/auth/callback"]
  )
  logout_urls = concat(
    var.cognito_logout_urls,
    ["https://${module.cloudfront.distribution_domain_name}"]
  )
  custom_domain          = var.cognito_custom_domain
  certificate_arn        = var.cognito_custom_domain_certificate_arn
  access_token_validity  = var.cognito_access_token_validity
  refresh_token_validity = var.cognito_refresh_token_validity
  id_token_validity      = var.cognito_id_token_validity
  # Web CLI login: short-lived, rotated refresh tokens for the CLI app client
  cli_refresh_token_validity = var.cognito_cli_refresh_token_validity

  # Issue #60: Provision test users (admins group, test user, test admin)
  create_test_users = var.create_test_users

  # Issue #313: GitHub OAuth identity provider
  enable_github_oauth        = var.enable_github_oauth
  github_oauth_client_id     = var.github_oauth_client_id
  github_oauth_client_secret = var.github_oauth_client_secret

  # Issue #642: KMS encryption for DynamoDB tables
  kms_key_arn = aws_kms_key.dynamodb.arn

  # Issue #4844: pass the allowlist configuration EXPLICITLY, from the same root
  # variables that configure the broker. This block previously passed no
  # pre_signup_* arguments at all, so the trigger ran the child module's defaults
  # ("org" + an empty org list = deny-all if it ever fired) while the broker ran
  # whatever the environment set — deny-vs-open divergence between two copies of
  # one rule, in one environment. Introducing a new mode on top of unset defaults
  # is the exact shape of the ALLOWLIST_MODE / ALLOW_OPEN_SIGNUP outage in
  # CLAUDE.md, so the two copies are now configured together, in one deploy unit.
  #
  # This changes NO deployed behaviour today: pre-signup is not the live gate for
  # GitHub sign-in (admin_create_user does not fire PreSignUp_ExternalProvider —
  # see lambda/github-auth-broker/handler.py), so what it would have decided has
  # never been consulted. It stops the two from drifting further.
  pre_signup_allowlist_mode    = var.github_auth_allowlist_mode
  pre_signup_allowed_orgs      = var.github_auth_allowed_orgs
  pre_signup_allow_open_signup = var.github_auth_allow_open_signup

  # Issue #4849: membership-eligibility projection read (shadow mode). Referencing
  # the tables directly is safe HERE — they are resources of this root module, so
  # this is root -> child, not the child -> root reference that would close the
  # cloudfront/api_gateway/broker cycle documented elsewhere in this file.
  identity_index_table_name      = aws_dynamodb_table.identity_index.name
  user_identity_index_table_name = aws_dynamodb_table.user_identity_index.name
  identity_index_table_arns = [
    aws_dynamodb_table.identity_index.arn,
    aws_dynamodb_table.user_identity_index.arn,
  ]
  user_identity_index_v2_read = var.user_identity_index_v2_read

  # Issue #2380: CloudWatch Log Group KMS encryption (CKV_AWS_158)
  cloudwatch_kms_key_arn = aws_kms_key.cloudwatch.arn

  # Issue #2910: Lambda reserved concurrency gated for fresh-account quota
  enable_reserved_concurrency = var.enable_lambda_reserved_concurrency

  # Issue #769: removed `depends_on = [module.cloudfront]`. The implicit
  # dependency through callback_urls/logout_urls (which reference
  # module.cloudfront.distribution_domain_name) already enforces ordering.
  # The explicit depends_on caused all data sources inside this module to
  # be deferred to apply-time per Terraform's documented module-depends_on
  # behavior, which made data.aws_caller_identity.current.account_id
  # `(known after apply)` at plan-time, which forced the domain attribute
  # to be `(known after apply)`, which forced replacement of an already-
  # existing AWS resource — yielding "Domain already exists" on every apply.
}

# =============================================================================
# S3 bucket for CloudFront access logs (optional)
# =============================================================================

module "s3_cloudfront_logs" {
  count  = var.enable_cloudfront_logging ? 1 : 0
  source = "./modules/s3-cloudfront-logs"

  environment        = var.environment
  name_prefix        = local.name_prefix
  common_tags        = local.common_tags
  log_retention_days = var.cloudfront_log_retention_days
}

# =============================================================================
# CloudFront Module for Frontend CDN
# =============================================================================

# -----------------------------------------------------------------------------
# Broker origin for CloudFront (see enable_broker_cloudfront_route)
# -----------------------------------------------------------------------------
# Resolved from the published invoke URL rather than from module.api_gateway
# outputs, which would create a dependency cycle:
#
#   cloudfront -> api_gateway (origin id/stage)
#             api_gateway -> github_auth_broker (broker_lambda_invoke_arn)
#                        github_auth_broker -> cloudfront (frontend_url)
#
# Terraform builds the graph from both branches of a ternary, so making
# frontend_url conditional does not remove that last edge. Issue #2708 hit the
# same cycle from the other direction and solved it the same way — by not
# referencing across the loop.
#
# Reading a parameter this stack also writes is safe here because the data source
# only exists when the flag is on, and the flag is a second pass by definition:
# the route cannot be enabled until the API Gateway it points at exists. This is
# the same shape as enable_vpc_origin, which likewise depends on a value from an
# earlier apply.
data "aws_ssm_parameter" "apigw_invoke_url_for_broker_origin" {
  count = var.enable_broker_cloudfront_route ? 1 : 0

  name = "/adp/${var.environment}/gateway/apigw-invoke-url"
}

locals {
  # https://<id>.execute-api.<region>.amazonaws.com/<stage>
  broker_origin_match = var.enable_broker_cloudfront_route ? regexall(
    "https://([a-z0-9]+)\\.execute-api\\.[a-z0-9-]+\\.amazonaws\\.com/(.+)$",
    nonsensitive(data.aws_ssm_parameter.apigw_invoke_url_for_broker_origin[0].value)
  ) : []

  broker_origin_domain_name = length(local.broker_origin_match) > 0 ? "${local.broker_origin_match[0][0]}.execute-api.${var.aws_region}.amazonaws.com" : ""
  broker_origin_path        = length(local.broker_origin_match) > 0 ? "/${local.broker_origin_match[0][1]}" : ""
}

module "cloudfront" {
  source = "./modules/cloudfront"

  environment                    = var.environment
  name_prefix                    = local.name_prefix
  common_tags                    = local.common_tags
  s3_bucket_regional_domain_name = module.frontend_s3.bucket_regional_domain_name
  s3_bucket_id                   = module.frontend_s3.bucket_id
  custom_domain_name             = var.frontend_domain_name
  acm_certificate_arn            = var.frontend_acm_certificate_arn
  additional_connect_src         = var.frontend_additional_connect_src

  # Route /auth/github/* through the distribution to the broker, so the OAuth
  # flow stays on the frontend hostname. Gated on its own variable rather than on
  # enable_api_gateway: the origin is additive and inert until
  # VITE_GITHUB_AUTH_BROKER_URL and the broker's CALLBACK_URL point at it, so
  # enabling it should be a deliberate step rather than a side effect of having
  # an API Gateway.
  broker_origin_domain_name = local.broker_origin_domain_name
  broker_origin_path        = local.broker_origin_path

  waf_web_acl_arn        = var.cloudfront_waf_web_acl_arn
  enable_ipv6            = var.cloudfront_enable_ipv6
  log_bucket_domain_name = var.enable_cloudfront_logging ? module.s3_cloudfront_logs[0].bucket_domain_name : ""
  # ALB domain is set dynamically by backend-deploy workflow after Ingress ALB is created
  # Pass empty string here — CloudFront will only have the S3 origin initially
  alb_domain_name = ""

  # VPC Origin configuration for internal ALB (security enhancement)
  # When enable_vpc_origin=true, CloudFront uses VPC Origin instead of custom origin
  # This allows the ALB to be internal, blocking direct public access
  # NOTE: internal_alb_arn is typically set dynamically in backend-deploy workflow
  # after the Ingress ALB is created by EKS
  enable_vpc_origin            = var.enable_vpc_origin
  internal_alb_arn             = var.internal_alb_arn
  internal_alb_dns             = var.internal_alb_dns
  vpc_origin_read_timeout      = var.vpc_origin_read_timeout
  vpc_origin_keepalive_timeout = var.vpc_origin_keepalive_timeout

  # GitLab VPC Origin (Issue #3583)
  gitlab_origin_dns = var.gitlab_origin_dns
  gitlab_origin_arn = var.gitlab_origin_arn
}

# =============================================================================
# Frontend S3 Module for SPA Hosting
# =============================================================================

module "frontend_s3" {
  source = "./modules/s3-frontend"

  environment                 = var.environment
  name_prefix                 = local.name_prefix
  common_tags                 = local.common_tags
  cloudfront_distribution_arn = module.cloudfront.distribution_arn
  cors_allowed_origins        = var.frontend_domain_name != "" ? ["https://${var.frontend_domain_name}"] : ["*"]
}

# =============================================================================
# CloudWatch Latency Dashboard (Issue #144)
# =============================================================================
# Unified end-to-end latency dashboard: CloudFront -> ALB -> Pod -> Bedrock
# ALB ARN suffix is set via variable because the ALB is created dynamically
# by the EKS Ingress controller, not by Terraform.

module "cloudwatch_dashboard" {
  source = "./modules/cloudwatch-dashboard"

  environment                = var.environment
  name_prefix                = local.name_prefix
  common_tags                = local.common_tags
  aws_region                 = var.aws_region
  cloudfront_distribution_id = module.cloudfront.distribution_id
  alb_arn_suffix             = var.alb_arn_suffix
  eks_cluster_name           = local.cluster_name
  eks_namespace              = "adp-gateway"
  pod_deployment_name        = "bedrockgateway"
}

# =============================================================================
# Budget Enforcement Alarms (Issue #4075)
# =============================================================================
# Budget enforcement fails CLOSED with a bounded grace window. These alarms make
# that window observable — an unobserved grace window is fail-open with extra
# steps. Metrics are app EMF from the gateway pod (namespace BedrockGateway,
# matching src/shared/metrics.py).
# =============================================================================

module "budget_alarms" {
  source = "./modules/budget-alarms"

  environment   = var.environment
  name_prefix   = local.name_prefix
  common_tags   = local.common_tags
  alarm_actions = var.budget_alarm_sns_topic_arns
}

# =============================================================================
# Budget fail-mode SSM parameter (Issue #4075)
# =============================================================================
# Makes budget_fail_mode reversible at runtime. Without this the mode is a
# compile-time constant, and the documented rollback ("set it back to open")
# cannot be executed at all — which would make shipping fail-closed strictly
# worse than the previous fail-open behaviour.
#
# Both ConfigMap renderers (gateway-deploy.yml and deploy-all.sh) read this
# param and stamp it into BG_BUDGET_BUDGET_FAIL_MODE.
# =============================================================================

resource "aws_ssm_parameter" "budget_fail_mode" {
  name        = "/adp/${var.environment}/gateway/budget-fail-mode"
  description = "Budget enforcement fail mode: closed (deny on check failure, default) or open (rollback lever). Issue #4075."
  type        = "String"
  value       = "closed"

  tags = local.common_tags

  # Operators flip this out-of-band (SSM put + rollout restart) during an
  # incident. Terraform must not revert that on the next apply.
  lifecycle {
    ignore_changes = [value]
  }
}

# NOTE: EKS→RDS (5432) and EKS→Redis (6379) security group rules are owned by
# platform infra (platform/infra/main.tf) — do NOT duplicate them here.
# See: https://github.com/aws-e/adp/issues/2590

# =============================================================================
# RDS Bootstrap Module (Issue #60)
# =============================================================================
# One-shot Job that runs `GRANT rds_iam TO bgadmin` on fresh databases.
# Without this, IAM-authenticated connections fail even though RDS has
# iam_database_authentication_enabled = true. The Postgres role must
# explicitly have the rds_iam grant.
#
# Must run AFTER RDS is available and SG rules allow EKS → RDS.
# =============================================================================

module "rds_bootstrap" {
  source = "./modules/rds-bootstrap"

  name_prefix            = local.name_prefix
  namespace              = "bedrockgw"
  aws_region             = var.aws_region
  db_host                = module.rds.db_instance_address
  db_name                = var.rds_db_name
  db_username            = var.rds_username
  master_user_secret_arn = module.rds.master_user_secret_arn
  oidc_provider_arn      = local.oidc_provider_arn
  oidc_issuer            = local.oidc_issuer
  common_tags            = local.common_tags
  rds_instance_id        = module.rds.db_instance_id

  depends_on = [
    module.rds,
  ]
}

# =============================================================================
# Budget Lambda Module (Issue #234)
# =============================================================================
# Creates Lambda functions for accurate budget tracking:
# 1. Usage Tracker Lambda - S3 event-driven cost recording from chat logs
# 2. Pricing Refresh Lambda - Daily pricing updates from AWS Pricing API
# =============================================================================

module "budget_lambda" {
  count  = var.enable_chat_logging ? 1 : 0
  source = "./modules/budget-lambda"

  environment = var.environment
  name_prefix = local.name_prefix
  common_tags = local.common_tags
  aws_region  = var.aws_region

  # S3 Chat Logs Bucket
  chat_logs_bucket_name = module.s3_chat_logs[0].bucket_name
  chat_logs_bucket_arn  = module.s3_chat_logs[0].bucket_arn

  # VPC Configuration
  vpc_id             = local.vpc_id
  private_subnet_ids = local.private_subnets

  # RDS Configuration
  rds_security_group_id = local.rds_security_group_id
  db_host               = module.rds.db_instance_address
  db_port               = module.rds.db_instance_port
  db_name               = var.rds_db_name
  db_username           = var.rds_username
  rds_resource_id       = module.rds.db_instance_resource_id

  # S3 bucket containing pre-built Lambda layer artifacts (Issue #1038)
  lambda_artifact_bucket = "adp-terraform-state-${data.aws_caller_identity.current.account_id}"

  # Issue #2380: CloudWatch Log Group KMS encryption (CKV_AWS_158)
  cloudwatch_kms_key_arn = aws_kms_key.cloudwatch.arn

  # Issue #2910: Lambda reserved concurrency gated for fresh-account quota
  enable_reserved_concurrency = var.enable_lambda_reserved_concurrency

  pricing_refresh_timeout = var.pricing_refresh_timeout

  # Existing topics are reused; empty input provisions the pricing SNS -> SQS
  # operational inbox inside this module (no manual subscription confirmation).
  alarm_actions = var.budget_alarm_sns_topic_arns

  # Ensure the psycopg2 layer zip is built+uploaded before this module's
  # aws_s3_object data source reads it.
  depends_on = [module.s3_chat_logs, module.rds, null_resource.build_psycopg2_layer]
}

# =============================================================================
# Orchestration Tick Module (Issue #4203)
# =============================================================================
# The delivery-loop engine's heartbeat: EventBridge -> VPC Lambda -> RDS, every
# few minutes. Reads the orchestration graph (migration 029_orchestration_graph),
# moves nodes whose predecessors are satisfied from `pending` to `ready` through
# the `transition()` guard, and exits. No dispatch — that is a later story.
#
# NOTE ON NAMING: `name_prefix` here is `adp-<env>`, NOT `local.name_prefix`
# (which is `bedrockgw-<env>`). The tick's function, log group and schedule rule
# are pinned to `adp-<env>-orchestration-tick` by the wave-3 evaluation and the
# documented smoke check. Composed from `var.environment` rather than hardcoded,
# matching the `adp-${var.environment}` idiom already used in kms.tf and
# user_identity_index.tf in this same root module.
#
# Issue #4298: resolves the tick's image tag to a digest so a re-pushed tag
# produces a real Terraform diff. See the image_uri comment in the module call.
data "aws_ecr_image" "orchestration_tick" {
  repository_name = "adp-gateway"
  image_tag       = var.orchestration_tick_image_tag
}

# Issue #4316: the SG fronting the shared VPC interface endpoints, so the tick can
# be granted 443 ingress to reach SQS over the private endpoint (private DNS is
# enabled on it, so there is no public path to fall back to — see the variable's
# description in the module for the full failure mode).
#
# Read as a data source rather than added as a `terraform_remote_state.platform`
# output on purpose: a new platform output would not exist in state until
# platform-infra-apply.yml runs, so `gateway-infra-apply.yml` would fail at plan
# time on a missing output key until an operator applied the two roots in the right
# order. The SG is created unconditionally by modules/networking with a stable
# name and Service tag, so looking it up keeps this fix inside the single apply
# that owns the tick. Filtered on the VPC as well, since name_prefix collides
# across VPCs in the same account (adp-dev-vpc vs adp-dev-cyber-vpc).
data "aws_security_group" "vpc_endpoints" {
  count  = var.enable_orchestration_tick ? 1 : 0
  vpc_id = local.vpc_id

  filter {
    name   = "tag:Name"
    values = ["adp-${var.environment}-sg-vpce"]
  }
}

# The Lambda runs the existing adp-gateway container image: the tick's logic is
# `src/orchestration/tick.py` (async SQLAlchemy/asyncpg), and that image is the
# only artifact that already carries the async stack plus the RDS CA bundle the
# TLS path needs. See modules/orchestration-tick/main.tf for the full rationale.
module "orchestration_tick" {
  count  = var.enable_orchestration_tick ? 1 : 0
  source = "./modules/orchestration-tick"

  environment = var.environment
  name_prefix = "adp-${var.environment}"
  common_tags = local.common_tags
  aws_region  = var.aws_region

  # Issue #4298 / #4313: resolve the tag to an immutable DIGEST at plan time.
  #
  # Passing "<repo>:latest" here is a static string, so Terraform sees no diff
  # when a new image is pushed under the same tag and never calls UpdateFunctionCode.
  # Lambda resolves a tag to a digest exactly once (at update time), so the tick
  # kept executing whatever image it was last pointed at — across green
  # gateway-deploy runs, with no error and no alarm. Wave 4 hit this twice: the
  # only reason wave 3's tick ran current code is that an operator ran a one-off
  # CLI update by hand.
  #
  # `aws_ecr_image` is a data source, so the digest is re-read on every plan; when
  # the tag moves, image_uri changes and the function is updated as part of the
  # normal apply. Pinning `orchestration_tick_image_tag` to a specific tag still
  # works and now also pins the digest.
  image_uri = "${local.ecr_gateway_url}@${data.aws_ecr_image.orchestration_tick.image_digest}"

  # VPC Configuration
  vpc_id             = local.vpc_id
  private_subnet_ids = local.private_subnets

  # Issue #4316: reach SQS over the private interface endpoint.
  vpc_endpoint_security_group_id = data.aws_security_group.vpc_endpoints[0].id

  # RDS Configuration
  rds_security_group_id = local.rds_security_group_id
  db_host               = module.rds.db_instance_address
  db_port               = module.rds.db_instance_port
  db_name               = var.rds_db_name
  db_username           = var.rds_username
  rds_resource_id       = module.rds.db_instance_resource_id

  tick_schedule = var.orchestration_tick_schedule

  # Issue #4211: stall/halt alert delivery. Empty by default — see the variable's
  # description for why an unsubscribed topic is visible rather than fatal.
  alert_email_addresses = var.orchestration_alert_email_addresses

  # Issue #4313: engine dispatch. The tick resolves the gate approver in-process
  # and produces the agent envelope onto the agent-submit FIFO queue itself — no
  # transport, no new route, no credential added to the webhook Lambda. The queue
  # belongs to the webhook-ingress Terraform state, so it is referenced by
  # ARN/URL variables rather than by a resource address.
  agent_submit_queue_arn = var.orchestration_dispatch_queue_arn
  agent_submit_queue_url = var.orchestration_dispatch_queue_url
  dispatch_repo          = var.orchestration_dispatch_repo
  dispatch_max_per_tick  = var.orchestration_dispatch_max_per_tick

  # Issue #4527: the GitHub engine-command bridge. The webhook Lambda marks an
  # `@agent-engine` comment on the event row it already writes — no queue message,
  # no gateway call — and the tick consumes the mark on its next wake. The table,
  # its KMS key and the per-tenant App secrets live in other Terraform states, so
  # they arrive as variables for the same reason the dispatch queue does.
  #
  # All four default to empty/false, which leaves the bridge inert: the pass reads
  # nothing and reports `commands_enabled=false`.
  engine_enabled                = var.orchestration_engine_enabled
  agent_authority_enabled       = var.orchestration_agent_authority_enabled
  webhook_events_table_name     = var.orchestration_webhook_events_table
  webhook_events_kms_key_arn    = var.orchestration_webhook_events_kms_key_arn
  github_app_secret_arn_pattern = var.orchestration_github_app_secret_arn_pattern

  # Issue #4539: command attribution. The tick verifies the signature the webhook
  # Lambda wrote before it trusts any authority field on the row. The secret belongs
  # to the webhook-ingress state, so it arrives by ARN like the four above; empty
  # leaves the verifier without a key, which quarantines every command rather than
  # applying it unverified.
  engine_command_signing_key_secret_arn = var.orchestration_engine_command_signing_key_secret_arn

  # Issue #2380: CloudWatch Log Group KMS encryption (CKV_AWS_158)
  cloudwatch_kms_key_arn = aws_kms_key.cloudwatch.arn

  # Issue #2910: Lambda reserved concurrency gated for fresh-account quota
  reserved_concurrency = var.enable_lambda_reserved_concurrency ? 2 : -1

  depends_on = [module.rds]
}

# =============================================================================
# API Gateway REST API Module (Issue #236)
# =============================================================================
# Creates an API Gateway REST API as an alternate route to the internal ALB.
# This provides 15-minute timeout support for long-running LLM requests
# (vs CloudFront's 60s hard limit due to OriginReadTimeout constraints).
#
# Architecture:
# Client -> API Gateway REST API (regional) -> VPC Link -> Internal ALB -> EKS
#
# Both CloudFront and API Gateway routes coexist. Clients choose which
# endpoint to use based on their needs.
# =============================================================================

module "api_gateway" {
  count  = var.enable_api_gateway ? 1 : 0
  source = "./modules/api-gateway"

  # EAA runbook 5.1 — per-path source restrictions at the API edge. Empty by
  # default, in which case no resource policy is created at all.
  agent_route_source_cidrs    = var.agent_route_source_cidrs
  internal_route_source_cidrs = var.internal_route_source_cidrs

  environment = var.environment
  name_prefix = local.name_prefix
  common_tags = local.common_tags
  aws_region  = var.aws_region

  # VPC Configuration
  vpc_id             = local.vpc_id
  private_subnet_ids = local.private_subnets

  # ALB Configuration (set dynamically by backend-deploy workflow)
  # The ALB is created by the EKS Ingress controller, so the ARN/DNS
  # are not known at Terraform plan time. The workflow updates these.
  internal_alb_arn = var.internal_alb_arn
  internal_alb_dns = var.internal_alb_dns

  # ALB Security Groups (Issue #42) — VPC Link v2 SG needs egress to these
  # Set dynamically by the deploy workflow alongside ALB ARN/DNS
  alb_security_group_ids = var.alb_security_group_ids

  # Internal-plane ALB (Issue #4010) — the `/internal/{proxy+}` route targets a
  # separate ALB that CloudFront has no VPC origin for, so the internal control
  # plane is unreachable from the edge by routing. All three default to
  # empty/[], in which case the route falls back to the edge ALB (pre-#4010
  # behavior). Populated by platform/scripts/wire-gateway-alb.sh.
  internal_plane_alb_arn                = var.internal_plane_alb_arn
  internal_plane_alb_dns                = var.internal_plane_alb_dns
  internal_plane_alb_security_group_ids = var.internal_plane_alb_security_group_ids

  # Authentication (backend handles JWT validation)
  cognito_user_pool_arn = module.cognito.cognito_user_pool_arn

  # Throttling
  throttle_burst_limit = var.api_gateway_throttle_burst_limit
  throttle_rate_limit  = var.api_gateway_throttle_rate_limit

  # Logging
  log_retention_days = var.api_gateway_log_retention_days

  # Issue #2380: CloudWatch Log Group KMS encryption (CKV_AWS_158)
  cloudwatch_kms_key_arn = aws_kms_key.cloudwatch.arn

  # Issue #1011: GitHub Auth Broker route in OpenAPI body
  broker_lambda_invoke_arn    = var.enable_github_auth_broker ? module.github_auth_broker[0].invoke_arn : ""
  broker_lambda_function_name = var.enable_github_auth_broker ? module.github_auth_broker[0].function_name : ""
  # Plan-time-known bool so the broker Lambda permission's count is evaluable
  # at plan time (the invoke_arn above is unknown until apply).
  enable_broker_route = var.enable_github_auth_broker

  depends_on = [module.cognito]
}

# =============================================================================
# Lambda Authorizer Module (Issue #239)
# =============================================================================
# Creates a Lambda authorizer for API Gateway that supports:
# 1. JWT validation against Cognito JWKS
# 2. IAM-based agent authentication via DynamoDB registry
#
# The authorizer sets context headers (X-Auth-Source, X-Agent-*) that are
# passed to the backend for identity resolution.
# =============================================================================

module "lambda_authorizer" {
  count  = var.enable_api_gateway ? 1 : 0
  source = "./modules/lambda-authorizer"

  environment = var.environment
  name_prefix = local.name_prefix
  common_tags = local.common_tags
  aws_region  = var.aws_region

  # S3 bucket containing pre-built Lambda layer artifacts (Issue #408)
  lambda_artifact_bucket = "adp-terraform-state-${data.aws_caller_identity.current.account_id}"

  # Cognito Configuration
  cognito_user_pool_id = module.cognito.cognito_user_pool_id

  # API Gateway Configuration
  api_gateway_id            = module.api_gateway[0].api_gateway_id
  api_gateway_execution_arn = module.api_gateway[0].api_gateway_execution_arn

  # Optional source-IP allowlist for the JWT/browser path (empty = disabled)
  ip_allowlist_ssm_parameter = var.authorizer_ip_allowlist_ssm_parameter

  # Issue #642: KMS encryption for DynamoDB tables
  kms_key_arn = aws_kms_key.dynamodb.arn

  # Issue #2380: CloudWatch Log Group KMS encryption (CKV_AWS_158)
  cloudwatch_kms_key_arn = aws_kms_key.cloudwatch.arn

  # Issue #2910: Lambda reserved concurrency gated for fresh-account quota
  enable_reserved_concurrency = var.enable_lambda_reserved_concurrency

  # Ensure the pyjwt layer zip is built+uploaded before this module's
  # aws_s3_object data source reads it.
  depends_on = [module.api_gateway, module.cognito, null_resource.build_pyjwt_layer]
}

# =============================================================================
# SSM Parameters for deploy-all.sh and CI/CD workflows
# =============================================================================
# These parameters allow deploy scripts and GitHub Actions workflows to
# discover resource names without running terraform output.

resource "aws_ssm_parameter" "frontend_bucket" {
  name        = "/adp/${var.environment}/gateway/frontend-bucket"
  description = "S3 bucket name for gateway frontend assets"
  type        = "String"
  value       = module.frontend_s3.bucket_name

  tags = local.common_tags
}

resource "aws_ssm_parameter" "cloudfront_id" {
  name        = "/adp/${var.environment}/gateway/cloudfront-id"
  description = "CloudFront distribution ID for cache invalidation"
  type        = "String"
  value       = module.cloudfront.distribution_id

  tags = local.common_tags
}

resource "aws_ssm_parameter" "cloudfront_domain" {
  name        = "/adp/${var.environment}/gateway/cloudfront-domain"
  description = "CloudFront distribution domain name"
  type        = "String"
  value       = module.cloudfront.distribution_domain_name

  tags = local.common_tags
}

# Issue #575: Worker pods read this to SigV4-sign calls to /agent/internal/*.
# Published here so any consumer (agent-factory, webhook-ingress, etc.) can
# resolve it at apply time rather than plumbing the invoke URL through as
# a hardcoded tfvar per environment.
resource "aws_ssm_parameter" "apigw_invoke_url" {
  count = var.enable_api_gateway ? 1 : 0

  name        = "/adp/${var.environment}/gateway/apigw-invoke-url"
  description = "Gateway API Gateway stage invoke URL (for /agent/* IAM-authed routes)"
  type        = "String"
  value       = module.api_gateway[0].api_gateway_invoke_url

  tags = local.common_tags
}

# Issue #575: Worker pods need this table name to seed their IRSA role entry.
# Publishing here rather than reading via cross-module state so agent-factory
# stays loosely coupled to gateway-infra.
resource "aws_ssm_parameter" "agent_registry_table" {
  count = var.enable_api_gateway ? 1 : 0

  name        = "/adp/${var.environment}/gateway/agent-registry-table"
  description = "DynamoDB table name for the agent registry (IAM ARN → agent mapping)"
  type        = "String"
  value       = module.lambda_authorizer[0].agent_registry_table_name

  tags = local.common_tags
}

resource "aws_ssm_parameter" "internal_alb_arn" {
  name        = "/adp/${var.environment}/gateway/internal-alb-arn"
  description = "ARN of the internal ALB created by EKS Ingress controller"
  type        = "String"
  value       = "pending"

  tags = local.common_tags

  # The value is updated by deploy-all.sh after ALB discovery.
  # Terraform should not revert it to "pending" on subsequent applies.
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "internal_alb_dns" {
  name        = "/adp/${var.environment}/gateway/internal-alb-dns"
  description = "DNS name of the internal ALB created by EKS Ingress controller"
  type        = "String"
  value       = "pending"

  tags = local.common_tags

  lifecycle {
    ignore_changes = [value]
  }
}

# =============================================================================
# Identity Index DynamoDB Table (Issue #375)
# =============================================================================
# Unified identity lookup table for tenant-identity Phase A.
# Maps identity_type + identity_value → org_id for O(1) lookups from
# webhook-ingress and pre-token-generation Lambdas.
# Gateway API is the authoritative writer; other services read via SSM name.
# =============================================================================

resource "aws_dynamodb_table" "identity_index" {
  name         = "adp-${var.environment}-identity-index"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "identity_type"
  range_key    = "identity_value"

  attribute {
    name = "identity_type"
    type = "S"
  }

  attribute {
    name = "identity_value"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.dynamodb.arn
  }

  tags = merge(local.common_tags, {
    Name    = "adp-${var.environment}-identity-index"
    Service = "dynamodb"
    Purpose = "tenant-identity-index"
  })
}

resource "aws_ssm_parameter" "identity_index_table" {
  name        = "/adp/${var.environment}/gateway/identity-index-table"
  description = "DynamoDB table name for identity-index (Issue #375)"
  type        = "String"
  value       = aws_dynamodb_table.identity_index.name

  tags = local.common_tags
}

# Issue #537: The existing identity-index table is retained as the
# channel-tenant cache (github_installation_id, cognito_client_id rows).
# Once github_user rows are removed post-migration, a future cleanup PR
# may rename this resource to aws_dynamodb_table.channel_tenant_cache
# using a `moved` block. The AWS table name stays unchanged.

# =============================================================================
# Cognito SSM Parameters (Issue #1007)
# =============================================================================
# Publish Cognito identifiers to SSM so the frontend build workflow can resolve
# them at deploy time instead of hardcoding platform-account values.
# =============================================================================

resource "aws_ssm_parameter" "cognito_user_pool_id" {
  name        = "/adp/${var.environment}/gateway/cognito-user-pool-id"
  description = "Cognito User Pool ID for frontend auth"
  type        = "String"
  value       = module.cognito.cognito_user_pool_id

  tags = local.common_tags
}

resource "aws_ssm_parameter" "cognito_client_id" {
  name        = "/adp/${var.environment}/gateway/cognito-client-id"
  description = "Cognito User Pool Client ID for frontend auth"
  type        = "String"
  value       = module.cognito.cognito_user_pool_client_id

  tags = local.common_tags
}

resource "aws_ssm_parameter" "cognito_cli_client_id" {
  name        = "/adp/${var.environment}/gateway/cognito-cli-client-id"
  description = "Cognito app client ID for web CLI login (BG_COGNITO_CLI_CLIENT_ID)"
  type        = "String"
  value       = module.cognito.cli_client_id

  tags = local.common_tags
}

resource "aws_ssm_parameter" "cognito_domain" {
  name        = "/adp/${var.environment}/gateway/cognito-domain"
  description = "Cognito hosted-UI domain prefix"
  type        = "String"
  value       = module.cognito.cognito_domain

  tags = local.common_tags
}

# The user-facing origin of the platform: the custom domain when there is one,
# otherwise the distribution's default hostname.
#
# Published because both callers of the backend deploy previously composed this
# from `cloudfront-domain`, which is the wrong value once an alias exists — and
# it is not a cosmetic wrongness. BG_GATEWAY_BASE_URL builds the GitHub App
# Setup URL that the register flow *sends to GitHub*, and the magic-link URLs
# that are sent to users. So a stale value propagates outside the deployment and
# silently reverts an org-admin change to the App.
resource "aws_ssm_parameter" "frontend_url" {
  name        = "/adp/${var.environment}/gateway/frontend-url"
  description = "User-facing origin of the platform (custom domain if set, else the CloudFront default). Consumed by the backend deploy for BG_GATEWAY_BASE_URL and CORS."
  type        = "String"
  value       = var.frontend_domain_name != "" ? "https://${var.frontend_domain_name}" : "https://${module.cloudfront.distribution_domain_name}"

  tags = local.common_tags
}

resource "aws_ssm_parameter" "github_auth_broker_url" {
  count = var.enable_github_auth_broker ? 1 : 0

  name        = "/adp/${var.environment}/gateway/github-auth-broker-url"
  description = "GitHub auth broker API Gateway invoke URL"
  type        = "String"
  # Issue #1011: Append /auth/github so the frontend can construct /start and /callback
  #
  # When the broker is served through CloudFront, this must be the distribution's
  # hostname, not the API Gateway's. The frontend builds /start from this value,
  # and the broker sets its OAuth state cookie on whatever host serves /start —
  # so if this and the broker's CALLBACK_URL name different hosts, the callback
  # never receives that cookie and every login fails `missing_state`. The two are
  # a matched pair; this is the half the frontend sees.
  value = var.enable_broker_cloudfront_route && var.frontend_domain_name != "" ? "https://${var.frontend_domain_name}/auth/github" : "${module.api_gateway[0].api_gateway_invoke_url}/auth/github"

  tags = local.common_tags
}

# =============================================================================
# Chat-Logging SSM Parameter (Issue #1014 / EPIC #1013)
# =============================================================================
# Publish the chat-logs bucket name so the gateway-deploy workflow can inject
# BG_CHAT_LOGGING_BUCKET into the pod's ConfigMap without a terraform-output
# dependency. Only created when chat logging is enabled.
# =============================================================================

resource "aws_ssm_parameter" "chat_logs_bucket" {
  count = var.enable_chat_logging ? 1 : 0

  name        = "/adp/${var.environment}/gateway/chat-logs-bucket"
  description = "S3 bucket name for chat-log archive (read by gateway pod's chat_logging service + cost-tracking Lambdas)"
  type        = "String"
  value       = module.s3_chat_logs[0].bucket_name

  tags = local.common_tags
}

# =============================================================================
# RDS + Redis SSM Parameters (Issue #1008)
# =============================================================================
# Publish RDS and Redis connection details so the gateway-deploy workflow can
# render the ConfigMap without running terraform output.
# =============================================================================

resource "aws_ssm_parameter" "rds_host" {
  name        = "/adp/${var.environment}/gateway/rds-host"
  description = "RDS instance hostname (without port) for gateway ConfigMap"
  type        = "String"
  value       = module.rds.db_instance_address

  tags = local.common_tags
}

resource "aws_ssm_parameter" "rds_database_name" {
  name        = "/adp/${var.environment}/gateway/rds-database-name"
  description = "RDS database name for gateway ConfigMap"
  type        = "String"
  value       = module.rds.db_instance_name

  tags = local.common_tags
}

resource "aws_ssm_parameter" "redis_host" {
  count = var.enable_redis ? 1 : 0

  name        = "/adp/${var.environment}/gateway/redis-host"
  description = "ElastiCache Redis endpoint for gateway ConfigMap"
  type        = "String"
  value       = module.redis[0].primary_endpoint_address

  tags = local.common_tags
}

resource "aws_ssm_parameter" "redis_port" {
  count = var.enable_redis ? 1 : 0

  name        = "/adp/${var.environment}/gateway/redis-port"
  description = "ElastiCache Redis port for gateway ConfigMap"
  type        = "String"
  value       = tostring(module.redis[0].port)

  tags = local.common_tags
}

# Issue #4342: the IAM-auth username and replication-group id the app needs to
# mint an ElastiCache connect token. The user + user group have existed in
# modules/redis/ since IAM auth was turned on, but neither value was ever
# published, so the gateway had no way to learn them and connected with no
# credential at all (as the disabled `default` user). These two params close
# that gap; gateway-deploy.yml reads them into the ConfigMap.
resource "aws_ssm_parameter" "redis_iam_username" {
  count = var.enable_redis && var.enable_elasticache_iam_auth ? 1 : 0

  name        = "/adp/${var.environment}/gateway/redis-iam-username"
  description = "ElastiCache IAM-auth user name for gateway ConfigMap (Issue #4342)"
  type        = "String"
  value       = module.redis[0].redis_iam_user_id

  tags = local.common_tags
}

resource "aws_ssm_parameter" "redis_cache_name" {
  count = var.enable_redis && var.enable_elasticache_iam_auth ? 1 : 0

  name        = "/adp/${var.environment}/gateway/redis-cache-name"
  description = "ElastiCache replication group id — the IAM token is signed against this, NOT the endpoint host (Issue #4342)"
  type        = "String"
  value       = module.redis[0].replication_group_id

  tags = local.common_tags
}

# =============================================================================
# GitHub Auth Broker Lambda (Issue #520)
# =============================================================================
# Lambda-based broker that converts GitHub OAuth flow into Cognito sessions.
# Replaces the failed Cognito-native OIDC approach (#518/#519).
# =============================================================================

# The GitHub OAuth App credentials are provisioned out-of-band in Secrets
# Manager (see #520). Read the ARN via a data source instead of relying on
# the cognito submodule, whose own IdP output is gated behind the separate
# var.enable_github_oauth flag (that path was reverted in #519).
data "aws_secretsmanager_secret" "github_oauth_for_broker" {
  count = var.enable_github_auth_broker ? 1 : 0
  name  = "adp/${var.environment}/cognito/github-oauth-credentials"
}

module "github_auth_broker" {
  count  = var.enable_github_auth_broker ? 1 : 0
  source = "./modules/github-auth-broker"

  environment             = var.environment
  name_prefix             = local.name_prefix
  common_tags             = local.common_tags
  aws_region              = var.aws_region
  cognito_user_pool_id    = module.cognito.cognito_user_pool_id
  cognito_user_pool_arn   = module.cognito.cognito_user_pool_arn
  cognito_client_id       = module.cognito.cognito_user_pool_client_id
  github_oauth_secret_arn = data.aws_secretsmanager_secret.github_oauth_for_broker[0].arn
  # The user-facing origin, which is the custom domain when there is one: the
  # broker redirects the browser here after auth (FRONTEND_URL in
  # lambda/github-auth-broker/handler.py). Pinned to the distribution's default
  # domain, a deployment with an alias sends users who signed in at their own
  # hostname back to the *.cloudfront.net one — a working login that visibly
  # lands on the wrong URL, and a second origin for cookies and CSP to disagree
  # about.
  frontend_url            = var.frontend_domain_name != "" ? "https://${var.frontend_domain_name}" : "https://${module.cloudfront.distribution_domain_name}"
  allowlist_mode          = var.github_auth_allowlist_mode
  allowed_orgs            = var.github_auth_allowed_orgs
  allow_open_signup       = var.github_auth_allow_open_signup
  github_token_secret_arn = var.github_auth_token_secret_arn
  lambda_artifact_bucket  = "adp-terraform-state-${data.aws_caller_identity.current.account_id}"

  # Issue #4133: encrypt the session-handoff code table with the existing
  # gateway DynamoDB CMK.
  dynamodb_kms_key_arn = aws_kms_key.dynamodb.arn

  # Issue #4849: membership-eligibility projection read (shadow mode). Safe as a
  # direct reference because the tables are root-module resources — the cycle
  # documented above is about the *broker* module referencing back into the root.
  identity_index_table_name      = aws_dynamodb_table.identity_index.name
  user_identity_index_table_name = aws_dynamodb_table.user_identity_index.name
  identity_index_table_arns = [
    aws_dynamodb_table.identity_index.arn,
    aws_dynamodb_table.user_identity_index.arn,
  ]
  user_identity_index_v2_read = var.user_identity_index_v2_read

  # Issue #2380: CloudWatch Log Group KMS encryption (CKV_AWS_158)
  cloudwatch_kms_key_arn = aws_kms_key.cloudwatch.arn

  # Issue #2910: Lambda reserved concurrency gated for fresh-account quota
  enable_reserved_concurrency = var.enable_lambda_reserved_concurrency

  # Issue #1011: API Gateway route is now defined in the api-gateway module's
  # OpenAPI body. The broker module only creates the Lambda + IAM.

  depends_on = [module.cognito, module.cloudfront]
}

# IAM policy for Gateway IRSA role to access identity-index table
resource "aws_iam_role_policy" "gateway_identity_index" {
  name = "${local.name_prefix}-policy-gateway-identity-index"
  role = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "IdentityIndexReadWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:BatchWriteItem",
          "dynamodb:DeleteItem",
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:Query",
          "dynamodb:UpdateItem"
        ]
        Resource = [
          aws_dynamodb_table.identity_index.arn
        ]
      },
      {
        Sid    = "DynamoDBKMSAccess"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = [aws_kms_key.dynamodb.arn]
      }
    ]
  })
}

# Issue #1797: SQS SendMessage permission for Phase 1 inline ingestion dispatch.
# The gateway publishes to the agent-context ingestion queue when a user registers
# or reindexes a knowledge asset. Phase 2 (sweeper) will make this additive-only
# (removable without breaking the registration flow).
resource "aws_iam_role_policy" "gateway_ingestion_sqs_publish" {
  count = var.enable_agent_context_sqs ? 1 : 0
  name  = "${local.name_prefix}-policy-gateway-ingestion-sqs-publish"
  role  = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "IngestionSQSPublish"
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:GetQueueUrl"
        ]
        Resource = var.agent_context_ingestion_queue_arn
      }
    ]
  })
}
