# The tick and gateway must use one shared budget accumulator. Reuse the
# gateway's existing Redis replication group and IAM user; create no new store.
variable "redis_enabled" {
  type    = bool
  default = false
}

variable "redis_host" {
  type    = string
  default = ""
}

variable "redis_port" {
  type    = number
  default = 6379
}

variable "redis_security_group_id" {
  type    = string
  default = ""
}

variable "redis_iam_auth" {
  type    = bool
  default = false
}

variable "redis_username" {
  type    = string
  default = ""
}

variable "redis_cache_name" {
  type    = string
  default = ""
}

locals {
  redis_environment = var.redis_enabled ? {
    BG_REDIS_URL        = "rediss://${var.redis_host}:${var.redis_port}/0"
    BG_REDIS_IAM_AUTH   = tostring(var.redis_iam_auth)
    BG_REDIS_USERNAME   = var.redis_username
    BG_REDIS_CACHE_NAME = var.redis_cache_name
  } : {}
}

resource "aws_security_group_rule" "redis_from_tick" {
  count                    = var.redis_enabled ? 1 : 0
  description              = "Redis budget access from the orchestration tick"
  type                     = "ingress"
  from_port                = var.redis_port
  to_port                  = var.redis_port
  protocol                 = "tcp"
  security_group_id        = var.redis_security_group_id
  source_security_group_id = aws_security_group.tick.id
}

resource "aws_iam_role_policy" "tick_redis" {
  count = var.redis_enabled && var.redis_iam_auth ? 1 : 0
  name  = "${local.tick_name}-redis"
  role  = aws_iam_role.tick.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "ConnectSharedBudgetRedis"
      Effect = "Allow"
      Action = ["elasticache:Connect"]
      Resource = [
        "arn:aws:elasticache:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:replicationgroup:${var.redis_cache_name}",
        "arn:aws:elasticache:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:user:${var.redis_username}"
      ]
    }]
  })
}
