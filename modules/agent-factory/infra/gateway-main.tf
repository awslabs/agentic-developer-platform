# =============================================================================
# Agent Gateway — SQS/KEDA-based async agent delivery
# =============================================================================
# Always deployed as part of agent-factory. Provides WebSocket, Slack, and CLI
# channels for triggering agents via SQS + KEDA (alongside GitHub Actions).
# =============================================================================

variable "gateway_namespace" {
  description = "Kubernetes namespace for gateway agent pods"
  type        = string
  default     = "adp-gateway-agents"
}

# --- SQS Queues ---

module "gateway_sqs" {
  source      = "./modules/sqs"
  name_prefix = local.name_prefix
  tags        = { Component = "agent-gateway" }
}

# --- DynamoDB Sessions ---

module "gateway_sessions" {
  source      = "./modules/dynamodb-sessions"
  name_prefix = local.name_prefix
  tags        = { Component = "agent-gateway" }
  kms_key_arn = aws_kms_key.dynamodb.arn
}

# --- Lambda Functions ---
# Note: ws_api_endpoint / ws_execution_arn reference the API GW module below.
# Terraform resolves these at plan-time; the Lambda config update depends on
# the API GW being created first, which is the correct ordering.

module "gateway_lambda" {
  source = "./modules/lambda-gateway"

  name_prefix         = local.name_prefix
  environment         = var.environment
  aws_region          = var.aws_region
  github_org          = var.github_org
  ingest_source_dir   = "${path.module}/../gateway/lambdas/ingest"
  response_source_dir = "${path.module}/../gateway/lambdas/response"
  # Chat agent uses the FIFO queue (MessageGroupId=session_id serializes
  # per-session turns). The standard queue still exists for the legacy Python
  # worker but the ingest Lambda only sends to FIFO now.
  input_queue_url       = aws_sqs_queue.chat_agent_tasks_fifo.url
  input_queue_arn       = aws_sqs_queue.chat_agent_tasks_fifo.arn
  response_queue_url    = module.gateway_sqs.response_queue_url
  response_queue_arn    = module.gateway_sqs.response_queue_arn
  sessions_table_name   = module.gateway_sessions.table_name
  sessions_table_arn    = module.gateway_sessions.table_arn
  artifacts_bucket_arn  = aws_s3_bucket.chat_artifacts.arn
  artifacts_bucket_name = aws_s3_bucket.chat_artifacts.id
  artifacts_table_arn   = aws_dynamodb_table.chat_artifacts.arn
  artifacts_table_name  = aws_dynamodb_table.chat_artifacts.name
  ws_api_endpoint       = var.gateway_deployed ? module.gateway_apigw[0].stage_invoke_url : ""
  ws_api_id             = var.gateway_deployed ? module.gateway_apigw[0].api_id : ""
  ws_execution_arn      = var.gateway_deployed ? module.gateway_apigw[0].execution_arn : ""
  enable_ws_policies    = true
  tags                  = { Component = "agent-gateway" }
}

# --- Gateway Auth (reuse authorizer from gateway module via remote state) ---

data "terraform_remote_state" "gateway" {
  count   = var.gateway_deployed ? 1 : 0
  backend = "s3"
  config = {
    bucket = "adp-terraform-state-${data.aws_caller_identity.current.account_id}"
    key    = "${var.environment}/modules/gateway/terraform.tfstate"
    region = var.aws_region
  }
}

locals {
  # Authorizer Lambda from gateway module (empty if gateway not deployed)
  authorizer_invoke_arn    = var.gateway_deployed ? try(data.terraform_remote_state.gateway[0].outputs.authorizer_lambda_invoke_arn, "") : ""
  authorizer_function_name = var.gateway_deployed ? try(data.terraform_remote_state.gateway[0].outputs.lambda_authorizer_name, "") : ""
}

# Fail-closed: when gateway_deployed=true, the authorizer outputs MUST be
# populated. If they're empty, gateway-infra-apply hasn't run with
# enable_api_gateway=true. This check produces a clear error instead of
# silently deploying an unauthenticated WebSocket.
check "authorizer_available_when_gateway_deployed" {
  assert {
    condition     = !var.gateway_deployed || (local.authorizer_invoke_arn != "" && local.authorizer_function_name != "")
    error_message = "gateway_deployed=true but the gateway module's authorizer outputs are empty. Ensure gateway-infra-apply has been run with enable_api_gateway=true before applying agent-factory."
  }
}

# --- WebSocket API Gateway ---
# Only deployed when gateway_deployed=true — a WebSocket API without an
# authorizer is worse than no WebSocket API (sec/H6).

module "gateway_apigw" {
  count  = var.gateway_deployed ? 1 : 0
  source = "./modules/api-gateway-ws"

  name_prefix                     = local.name_prefix
  ingest_lambda_arn               = module.gateway_lambda.ingest_lambda_arn
  ingest_lambda_name              = module.gateway_lambda.ingest_lambda_name
  authorizer_lambda_invoke_arn    = local.authorizer_invoke_arn
  authorizer_lambda_function_name = local.authorizer_function_name
  tags                            = { Component = "agent-gateway" }
}

# --- Gateway Agents Namespace ---

resource "kubernetes_namespace" "gateway_agents" {
  metadata {
    name = var.gateway_namespace
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "app.kubernetes.io/part-of"    = "adp-agent-factory"
      "app.kubernetes.io/component"  = "agent-gateway"
    }
  }
}

# =============================================================================
# KEDA CRD Assertion — Phase 7 (webhook-ingress) owns KEDA installation
# =============================================================================
# KEDA is installed by Phase 7 (modules/agent-factory/webhook-ingress/infra/).
# This data source asserts the CRD exists at plan time — if Phase 7 hasn't run,
# terraform plan fails with a clear error instead of a cryptic apply failure.
#
# Issue: #1052 — moved KEDA ownership from Phase 8 to Phase 7.
# =============================================================================

data "kubernetes_resource" "keda_crd" {
  api_version = "apiextensions.k8s.io/v1"
  kind        = "CustomResourceDefinition"

  metadata {
    name = "scaledjobs.keda.sh"
  }
}

# =============================================================================
# KEDA Operator IAM Role — discovered from Phase 7 (read-only reference)
# =============================================================================
# The role is owned by Phase 7's keda.tf. Phase 8 references it to:
#   1. Allow chain-assume in gateway_agent trust policy
#   2. Attach SQS read policy for Phase 8's gateway queues
# =============================================================================

data "aws_iam_role" "keda_operator" {
  name = "adp-${var.environment}-keda-operator-role"
}

# Grant KEDA operator SQS read access to Phase 8's gateway queues so it can
# poll queue depth for the chat-agent ScaledJob.
resource "aws_iam_role_policy" "keda_operator_gateway_sqs" {
  name = "gateway-sqs-scaler-read"
  role = data.aws_iam_role.keda_operator.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SQSQueuePolling"
        Effect = "Allow"
        Action = [
          "sqs:GetQueueAttributes",
          "sqs:ListQueues"
        ]
        Resource = [
          module.gateway_sqs.input_queue_arn,
          module.gateway_sqs.response_queue_arn,
        ]
      },
      {
        Sid      = "AssumeWorkloadRole"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = aws_iam_role.gateway_agent.arn
      }
    ]
  })

  depends_on = [data.kubernetes_resource.keda_crd]
}

# =============================================================================
# WebSocket endpoint SSM Parameter (Issue #1007)
# =============================================================================
# Publish the WebSocket API Gateway endpoint to SSM so the frontend build
# workflow can resolve it at deploy time for cross-account deploys.
# =============================================================================

resource "aws_ssm_parameter" "gateway_ws_endpoint" {
  count = var.gateway_deployed ? 1 : 0

  name        = "/adp/${var.environment}/gateway/agent-ws-url"
  description = "WebSocket API Gateway endpoint for agent streaming"
  type        = "String"
  value       = module.gateway_apigw[0].stage_invoke_url

  tags = { Component = "agent-gateway" }
}

# --- Extend runner IAM with gateway permissions ---

# =============================================================================
# Gateway Agent IAM Role (IRSA) — for SQS consumer pods in adp-gateway-agents
# =============================================================================
# This role is assumed by the `adp-agent` service account in the
# `adp-gateway-agents` namespace. It grants the worker pods permissions to:
#   - Invoke Bedrock models (foundation-model + inference-profile ARNs)
#   - Consume from the tasks SQS queue and send to the responses queue
#   - Read/write DynamoDB session state
#   - Fetch secrets from Secrets Manager (GitHub App keys, etc.)
#
# Originally created via CLI in PR #9; codified here in issue #10.
# =============================================================================

resource "aws_iam_role" "gateway_agent" {
  name = "adp-${var.environment}-gateway-agent-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # IRSA: worker pods in adp-gateway-agents assume this role via their SA
        Effect = "Allow"
        Principal = {
          Federated = local.oidc_provider_arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${replace(local.oidc_issuer, "https://", "")}:sub" = "system:serviceaccount:${var.gateway_namespace}:adp-agent"
            "${replace(local.oidc_issuer, "https://", "")}:aud" = "sts.amazonaws.com"
          }
        }
      },
      {
        # KEDA operator chain-assumes this role for SQS queue-depth polling.
        # KEDA's aws-eks identity provider authenticates as the operator SA first,
        # then assumes the workload role to make the SQS GetQueueAttributes call.
        # Role is owned by Phase 7 (webhook-ingress/infra/keda.tf), referenced here.
        Effect = "Allow"
        Principal = {
          AWS = data.aws_iam_role.keda_operator.arn
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name      = "adp-${var.environment}-gateway-agent-role"
    Component = "agent-gateway"
  }
}

resource "aws_iam_role_policy" "gateway_agent_bedrock" {
  name = "bedrock-invoke"
  role = aws_iam_role.gateway_agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ]
      Resource = [
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*"
      ]
    }]
  })
}

resource "aws_iam_role_policy" "gateway_agent_sqs" {
  name = "sqs-access"
  role = aws_iam_role.gateway_agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ConsumeTasksQueue"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        Resource = module.gateway_sqs.input_queue_arn
      },
      {
        Sid      = "SendToResponsesQueue"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = module.gateway_sqs.response_queue_arn
      }
    ]
  })
}

resource "aws_iam_role_policy" "gateway_agent_dynamodb" {
  name = "dynamodb-sessions"
  role = aws_iam_role.gateway_agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query"
        ]
        Resource = [
          module.gateway_sessions.table_arn,
          "${module.gateway_sessions.table_arn}/index/*"
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

resource "aws_iam_role_policy" "gateway_agent_secrets" {
  name = "secrets-access"
  role = aws_iam_role.gateway_agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowGitHubAppSecrets"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:adp/*/gh-app-*"
      },
      {
        # Deny direct access to tenant-level AWS credentials.
        # AWS creds are now fetched via gateway /internal/v1/credential-raw-read
        # which resolves user-scoped credentials (issue #455).
        Sid      = "DenyTenantAwsAccess"
        Effect   = "Deny"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:adp/*/tenants/*/aws-access-*"
      }
    ]
  })
}

# =============================================================================
# Kubernetes Service Account — adp-agent in adp-gateway-agents namespace
# =============================================================================
# IRSA-annotated service account for the gateway worker pods. Referenced by
# the KEDA ScaledJob (keda-scaledjob.yaml) as serviceAccountName: adp-agent.
#
# This was previously created by deploy-gateway.sh via kubectl apply of
# k8s/serviceaccount.yaml. Now Terraform-managed so it survives destroy/apply.
# =============================================================================

resource "kubernetes_service_account" "gateway_agent" {
  metadata {
    name      = "adp-agent"
    namespace = kubernetes_namespace.gateway_agents.metadata[0].name

    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.gateway_agent.arn
    }

    labels = {
      "app.kubernetes.io/name"       = "adp-agent"
      "app.kubernetes.io/part-of"    = "agent-gateway"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}

# =============================================================================
# Terraform-managed ConfigMap for the agent-gateway worker pods (Phase 3 #748)
# =============================================================================
# Replaces the static YAML ConfigMap in gateway/k8s/keda-scaledjob.yaml.
# Values are derived from Terraform outputs / SSM so subsequent deploys
# (new env, new account, API GW rebuild) automatically pick up the right URLs.
# =============================================================================

data "aws_ssm_parameter" "gateway_apigw_invoke_url" {
  count = var.gateway_deployed ? 1 : 0
  name  = "/adp/${var.environment}/gateway/apigw-invoke-url"
}

resource "kubernetes_config_map" "agent_gateway_config" {
  metadata {
    name      = "agent-gateway-config"
    namespace = kubernetes_namespace.gateway_agents.metadata[0].name
    labels = {
      "app.kubernetes.io/name"       = "agent-gateway"
      "app.kubernetes.io/part-of"    = "agent-gateway"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  data = {
    INPUT_QUEUE_URL     = module.gateway_sqs.input_queue_url
    RESPONSE_QUEUE_URL  = module.gateway_sqs.response_queue_url
    SESSIONS_TABLE_NAME = module.gateway_sessions.table_name
    AWS_REGION          = var.aws_region
    AGENT_DIR           = "/app/agent"
    # Use the `us.` cross-region inference profile (NOT `global.`): the `us.`
    # profile is available in every account we deploy to, whereas `global.` is
    # not enabled on all accounts (e.g. test account 919157478356 returns
    # "invalid model identifier" for global.* and AccessDenied until the
    # Marketplace subscription lands). `us.` works on both 919 and embark1.
    ANTHROPIC_MODEL = "us.anthropic.claude-opus-4-6-v1"
    # Phase 3 gateway routing (issue #748)
    ADP_BEDROCK_VIA            = "gateway"
    SIGV4_PROXY_TARGET         = var.gateway_deployed ? "${data.aws_ssm_parameter.gateway_apigw_invoke_url[0].value}/agent" : ""
    SIGV4_PROXY_PORT           = "9090"
    ANTHROPIC_BEDROCK_BASE_URL = "http://127.0.0.1:9090"
    CLAUDE_CODE_USE_BEDROCK    = "1"
  }
}

# =============================================================================
# Gateway Agent: execute-api:Invoke for sigv4-proxy → API Gateway (Phase 3)
# =============================================================================
# The worker pod's sigv4-proxy re-signs requests with service=execute-api.
# This policy grants the pod's IRSA role permission to invoke the /agent/*
# routes on the gateway's REST API Gateway.
# =============================================================================

# =============================================================================
# Gateway Agent: CloudWatch Logs for durable bootstrap logging (#1690)
# =============================================================================
# The entrypoint writes step-level bootstrap logs directly to CloudWatch so
# Setup failures are diagnosable after the pod is GC'd by KEDA.
# =============================================================================

resource "aws_iam_role_policy" "gateway_agent_bootstrap_logs" {
  name = "bootstrap-cloudwatch-logs"
  role = aws_iam_role.gateway_agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "BootstrapLogging"
      Effect = "Allow"
      Action = [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:PutRetentionPolicy"
      ]
      Resource = [
        "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/adp/*/agent-factory/bootstrap",
        "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/adp/*/agent-factory/bootstrap:*"
      ]
    }]
  })
}

resource "aws_iam_role_policy" "gateway_agent_execute_api" {
  name = "execute-api-invoke"
  role = aws_iam_role.gateway_agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "InvokeGatewayAgentRoutes"
      Effect   = "Allow"
      Action   = ["execute-api:Invoke"]
      Resource = "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/*/*/agent/*"
    }]
  })
}

# --- Extend runner IAM with gateway permissions ---

resource "aws_iam_role_policy" "runner_gateway_sqs" {
  name = "gateway-sqs"
  role = module.runner_iam.runner_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"]
        Resource = module.gateway_sqs.input_queue_arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage", "sqs:GetQueueAttributes"]
        Resource = module.gateway_sqs.response_queue_arn
      }
    ]
  })
}

resource "aws_iam_role_policy" "runner_gateway_dynamodb" {
  name = "gateway-dynamodb"
  role = module.runner_iam.runner_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Query"]
        Resource = [module.gateway_sessions.table_arn, "${module.gateway_sessions.table_arn}/index/*"]
      },
      {
        Sid      = "DynamoDBKMSAccess"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"]
        Resource = [aws_kms_key.dynamodb.arn]
      }
    ]
  })
}
