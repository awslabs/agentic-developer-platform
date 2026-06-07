# =============================================================================
# ADP Research: gbrain — Experimental Persona Learning Evaluation
# =============================================================================
# Isolated Fargate + RDS deployment for evaluating gbrain as a per-persona
# experiential knowledge store. Independent state, own cluster, clean teardown.
# =============================================================================

locals {
  name_prefix = "adp-research-gbrain"
  account_id  = data.aws_caller_identity.current.account_id

  common_tags = {
    Project      = "adp-research-gbrain"
    Environment  = var.environment
    ManagedBy    = "terraform"
    Experiment   = "true"
    ExperimentId = "gbrain-eval-2026-06"
    Owner        = var.owner_email
    Module       = "research/gbrain"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}

# -----------------------------------------------------------------------------
# Storage (S3 bucket for brain repo + ECR for container image)
# -----------------------------------------------------------------------------
module "storage" {
  source = "./modules/storage"

  name_prefix = local.name_prefix
  account_id  = local.account_id
}

# -----------------------------------------------------------------------------
# RDS PostgreSQL with pgvector
# -----------------------------------------------------------------------------
module "rds" {
  source = "./modules/rds"

  name_prefix       = local.name_prefix
  vpc_id            = var.vpc_id
  subnet_ids        = var.private_subnet_ids
  fargate_sg_id     = aws_security_group.svc.id
  instance_class    = var.instance_class
  allocated_storage = var.db_allocated_storage
}

# -----------------------------------------------------------------------------
# Fargate (ECS cluster + task definition + service)
# -----------------------------------------------------------------------------
module "fargate" {
  source = "./modules/fargate"

  name_prefix        = local.name_prefix
  vpc_id             = var.vpc_id
  subnet_ids         = var.private_subnet_ids
  service_sg_id      = aws_security_group.svc.id
  cpu                = var.cpu
  memory             = var.memory
  desired_count      = var.desired_count
  container_image    = "${module.storage.ecr_repo_url}:latest"
  db_endpoint        = module.rds.endpoint
  db_credentials_arn = module.rds.credentials_secret_arn
  mcp_token_arn      = aws_secretsmanager_secret.mcp_token.arn
  s3_bucket_name     = module.storage.bucket_name
  log_group_name     = aws_cloudwatch_log_group.gbrain.name
  task_role_arn      = aws_iam_role.app.arn
  execution_role_arn = aws_iam_role.task_execution.arn
}

# -----------------------------------------------------------------------------
# Scheduler (EventBridge dream cycle)
# -----------------------------------------------------------------------------
module "scheduler" {
  source = "./modules/scheduler"

  name_prefix = local.name_prefix
  cluster_arn = module.fargate.cluster_arn
  task_def    = module.fargate.task_definition_arn
  subnet_ids  = var.private_subnet_ids
  sg_id       = aws_security_group.svc.id
  role_arn    = aws_iam_role.scheduler.arn
}

# -----------------------------------------------------------------------------
# Security Groups
# -----------------------------------------------------------------------------
resource "aws_security_group" "svc" {
  name_prefix = "${local.name_prefix}-svc-"
  description = "gbrain Fargate service - inbound from VPC on port 3000"
  vpc_id      = var.vpc_id

  ingress {
    description = "MCP from VPC"
    from_port   = 3000
    to_port     = 3000
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  lifecycle {
    create_before_destroy = true
  }
}

# -----------------------------------------------------------------------------
# Secrets Manager — MCP bearer token
# -----------------------------------------------------------------------------
resource "random_password" "mcp_token" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "mcp_token" {
  name                    = "adp/research/gbrain/mcp-token"
  description             = "Bearer token for gbrain MCP endpoint authentication"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "mcp_token" {
  secret_id     = aws_secretsmanager_secret.mcp_token.id
  secret_string = random_password.mcp_token.result
}

# -----------------------------------------------------------------------------
# CloudWatch Log Group
# -----------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "gbrain" {
  name              = "/adp/research/gbrain"
  retention_in_days = 30
}

# -----------------------------------------------------------------------------
# IAM — Task Execution Role (ECR pull, CW Logs, Secrets Manager)
# -----------------------------------------------------------------------------
resource "aws_iam_role" "task_execution" {
  name = "${local.name_prefix}-task-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "task_execution_managed" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "task_execution_secrets" {
  name = "secrets-access"
  role = aws_iam_role.task_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "secretsmanager:GetSecretValue"
      ]
      Resource = [
        module.rds.credentials_secret_arn,
        aws_secretsmanager_secret.mcp_token.arn
      ]
    }]
  })
}

# -----------------------------------------------------------------------------
# IAM — Application Role (Bedrock, S3, RDS connect)
# -----------------------------------------------------------------------------
resource "aws_iam_role" "app" {
  name = "${local.name_prefix}-app-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "app_permissions" {
  name = "app-permissions"
  role = aws_iam_role.app.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BedrockInvoke"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]
        Resource = "arn:aws:bedrock:${var.aws_region}::foundation-model/*"
      },
      {
        Sid    = "S3BrainRepo"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:ListBucket",
          "s3:DeleteObject"
        ]
        Resource = [
          module.storage.bucket_arn,
          "${module.storage.bucket_arn}/*"
        ]
      },
      {
        Sid    = "SecretsRead"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = [
          module.rds.credentials_secret_arn,
          aws_secretsmanager_secret.mcp_token.arn
        ]
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# IAM — Scheduler Role (EventBridge → ECS RunTask)
# -----------------------------------------------------------------------------
resource "aws_iam_role" "scheduler" {
  name = "${local.name_prefix}-scheduler-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "events.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_run_task" {
  name = "run-task"
  role = aws_iam_role.scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "ecs:RunTask"
        Resource = module.fargate.task_definition_arn
        Condition = {
          ArnLike = {
            "ecs:cluster" = module.fargate.cluster_arn
          }
        }
      },
      {
        Effect = "Allow"
        Action = "iam:PassRole"
        Resource = [
          aws_iam_role.task_execution.arn,
          aws_iam_role.app.arn
        ]
      }
    ]
  })
}
