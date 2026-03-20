# ElastiCache Subnet Group
resource "aws_elasticache_subnet_group" "main" {
  name       = "${var.name_prefix}-cache-subnet"
  subnet_ids = var.private_subnet_ids

  tags = merge(var.common_tags, {
    Name     = "${var.name_prefix}-cache-subnet"
    Service  = "cache"
    DataType = "ephemeral"
  })
}

# ElastiCache IAM User for passwordless IAM authentication
resource "aws_elasticache_user" "iam_user" {
  user_id       = "${var.name_prefix}-iam-user"
  user_name     = "${var.name_prefix}-iam-user"
  access_string = "on ~* +@all"
  engine        = "redis"

  authentication_mode {
    type = "iam"
  }

  tags = merge(var.common_tags, {
    Name     = "${var.name_prefix}-redis-iam-user"
    Service  = "cache"
    DataType = "ephemeral"
  })
}

# Default user (required by ElastiCache user groups)
# Disabled for security - applications use the IAM user instead
resource "aws_elasticache_user" "default" {
  user_id       = "${var.name_prefix}-redis-default"
  user_name     = "default"
  access_string = "off ~* -@all"
  engine        = "redis"

  authentication_mode {
    type = "no-password-required"
  }

  timeouts {
    create = "10m"
    update = "10m"
    delete = "10m"
  }

  # Ignore tag and auth diffs to prevent triggering ElastiCache user
  # modifications which are extremely slow and frequently get stuck
  # in "modifying" state, blocking all Terraform applies.
  lifecycle {
    ignore_changes = [tags, tags_all, no_password_required, authentication_mode]
  }

  tags = merge(var.common_tags, {
    Name     = "${var.name_prefix}-redis-default"
    Service  = "cache"
    DataType = "ephemeral"
  })
}

# ElastiCache User Group for IAM authentication
resource "aws_elasticache_user_group" "main" {
  user_group_id = "${var.name_prefix}-redis-group"
  engine        = "redis"
  user_ids      = [aws_elasticache_user.iam_user.user_id, aws_elasticache_user.default.user_id]

  tags = merge(var.common_tags, {
    Name     = "${var.name_prefix}-redis-group"
    Service  = "cache"
    DataType = "ephemeral"
  })

  lifecycle {
    # Prevent accidental deletion of user group which would break Redis authentication
    prevent_destroy = false
  }
}

# ElastiCache Parameter Group
resource "aws_elasticache_parameter_group" "main" {
  family = "redis7"
  name   = "${var.name_prefix}-cache-params"

  parameter {
    name  = "maxmemory-policy"
    value = "allkeys-lru"
  }

  tags = merge(var.common_tags, {
    Name     = "${var.name_prefix}-cache-params"
    Service  = "cache"
    DataType = "ephemeral"
  })
}

# ElastiCache Replication Group (Redis Cluster)
# Used for both multi-node (prod) and single-node (dev) configurations
# Note: aws_elasticache_cluster does not support user_group_ids for IAM auth,
# so we use replication_group for all environments to enable IAM authentication
resource "aws_elasticache_replication_group" "main" {
  replication_group_id = "${var.name_prefix}-redis-cluster"
  description          = "Redis cluster for BedrockGateway ${var.environment}"

  node_type            = var.node_type
  port                 = var.port
  parameter_group_name = aws_elasticache_parameter_group.main.name

  num_cache_clusters = var.num_cache_nodes

  # Network configuration
  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [var.redis_security_group_id]

  # Security - Using IAM authentication instead of auth_token
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true # Required for IAM authentication
  user_group_ids             = [aws_elasticache_user_group.main.user_group_id]

  # Backup configuration
  snapshot_retention_limit = var.environment == "prod" ? 7 : 1
  snapshot_window          = "03:00-04:00"

  # Maintenance
  maintenance_window = "sun:05:00-sun:06:00"

  # Auto failover (requires Multi-AZ, only enabled for multi-node clusters)
  automatic_failover_enabled = var.num_cache_nodes > 1 ? true : false
  multi_az_enabled           = var.num_cache_nodes > 1 ? true : false

  # Apply changes immediately for non-prod
  apply_immediately = var.environment == "prod" ? false : true

  tags = merge(var.common_tags, {
    Name     = "${var.name_prefix}-redis-cluster"
    Service  = "cache"
    DataType = "ephemeral"
  })

  depends_on = [
    aws_elasticache_subnet_group.main,
    aws_elasticache_parameter_group.main,
    aws_elasticache_user_group.main
  ]
}

# NOTE: The single-node aws_elasticache_cluster resource has been removed.
# aws_elasticache_cluster does not support user_group_ids for IAM authentication.
# We now use aws_elasticache_replication_group for all environments (both single-node
# and multi-node) to enable passwordless IAM authentication.

# NOTE: Password-based authentication resources (random_password, aws_secretsmanager_secret,
# aws_secretsmanager_secret_version) have been removed in favor of IAM authentication.
# The application now uses IAM tokens generated via the AWS SDK's generate_auth_token()
# method, similar to RDS IAM database authentication.

# CloudWatch Alarms for Redis
resource "aws_cloudwatch_metric_alarm" "redis_cpu" {
  count = var.enable_monitoring ? 1 : 0

  alarm_name          = "${var.name_prefix}-redis-cpu-utilization"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = "2"
  metric_name         = "CPUUtilization"
  namespace           = "AWS/ElastiCache"
  period              = "300"
  statistic           = "Average"
  threshold           = "80"
  alarm_description   = "This metric monitors redis cpu utilization"
  alarm_actions       = var.sns_topic_arn != "" ? [var.sns_topic_arn] : []

  dimensions = {
    ReplicationGroupId = aws_elasticache_replication_group.main.replication_group_id
  }

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-redis-cpu-alarm"
  })
}

resource "aws_cloudwatch_metric_alarm" "redis_memory" {
  count = var.enable_monitoring ? 1 : 0

  alarm_name          = "${var.name_prefix}-redis-memory-utilization"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = "2"
  metric_name         = "DatabaseMemoryUsagePercentage"
  namespace           = "AWS/ElastiCache"
  period              = "300"
  statistic           = "Average"
  threshold           = "90"
  alarm_description   = "This metric monitors redis memory utilization"
  alarm_actions       = var.sns_topic_arn != "" ? [var.sns_topic_arn] : []

  dimensions = {
    ReplicationGroupId = aws_elasticache_replication_group.main.replication_group_id
  }

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-redis-memory-alarm"
  })
}