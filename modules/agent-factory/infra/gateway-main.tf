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
}

# --- Lambda Functions (created first with empty WS vars) ---

module "gateway_lambda" {
  source = "./modules/lambda-gateway"

  name_prefix         = local.name_prefix
  environment         = var.environment
  aws_region          = var.aws_region
  ingest_source_dir   = "${path.module}/../gateway/lambdas/ingest"
  response_source_dir = "${path.module}/../gateway/lambdas/response"
  input_queue_url     = module.gateway_sqs.input_queue_url
  input_queue_arn     = module.gateway_sqs.input_queue_arn
  response_queue_arn  = module.gateway_sqs.response_queue_arn
  sessions_table_name = module.gateway_sessions.table_name
  sessions_table_arn  = module.gateway_sessions.table_arn
  tags                = { Component = "agent-gateway" }
}

# --- Gateway Auth (reuse authorizer from gateway module via remote state) ---

data "terraform_remote_state" "gateway" {
  backend = "s3"
  config = {
    bucket = "adp-terraform-state-${var.account_id}"
    key    = "${var.environment}/modules/gateway/terraform.tfstate"
    region = var.aws_region
  }
}

locals {
  # Authorizer Lambda from gateway module (empty if gateway not deployed)
  authorizer_invoke_arn = try(data.terraform_remote_state.gateway.outputs.authorizer_lambda_invoke_arn, "")
}

# --- WebSocket API Gateway ---

module "gateway_apigw" {
  source = "./modules/api-gateway-ws"

  name_prefix                  = local.name_prefix
  ingest_lambda_arn            = module.gateway_lambda.ingest_lambda_arn
  ingest_lambda_name           = module.gateway_lambda.ingest_lambda_name
  authorizer_lambda_invoke_arn = local.authorizer_invoke_arn
  tags                         = { Component = "agent-gateway" }
}

# --- KEDA ---

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

resource "helm_release" "keda" {
  name             = "keda"
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  namespace        = "keda"
  version          = "2.16.0"
  create_namespace = true
  wait             = true
  timeout          = 600

  set { name = "serviceAccount.create"; value = "true" }
  set { name = "serviceAccount.name"; value = "keda-operator" }
  set { name = "metricsServer.enabled"; value = "true" }
  set { name = "resources.operator.requests.cpu"; value = "100m" }
  set { name = "resources.operator.requests.memory"; value = "128Mi" }
  set { name = "resources.operator.limits.cpu"; value = "500m" }
  set { name = "resources.operator.limits.memory"; value = "512Mi" }
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
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:GetItem", "dynamodb:Query"]
      Resource = [module.gateway_sessions.table_arn, "${module.gateway_sessions.table_arn}/index/*"]
    }]
  })
}
