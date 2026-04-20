# IAM Database Authentication is enabled for passwordless authentication.
# The app will use its IAM role to generate short-lived auth tokens instead
# of storing passwords in Secrets Manager.
# Auth tokens auto-expire after 15 minutes and are refreshed on each connection.
#
# The master user password is managed by AWS Secrets Manager via
# `manage_master_user_password = true`. AWS creates the secret, rotates it,
# and never surfaces a plaintext value to Terraform state. Bootstrap tasks
# that need to run one-time SQL (e.g. `GRANT rds_iam TO bgadmin`) read the
# current value just-in-time via `aws secretsmanager get-secret-value`.

# DB Subnet Group
resource "aws_db_subnet_group" "main" {
  name       = "${var.name_prefix}-db-subnet-group"
  subnet_ids = var.private_subnet_ids

  tags = merge(var.common_tags, {
    Name     = "${var.name_prefix}-db-subnet-group"
    Service  = "database"
    DataType = "tenant-data"
  })
}

# DB Parameter Group
resource "aws_db_parameter_group" "main" {
  family = "postgres16"
  name   = "${var.name_prefix}-db-params"

  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements"
    apply_method = "pending-reboot"
  }

  parameter {
    name  = "log_statement"
    value = "all"
  }

  parameter {
    name  = "log_duration"
    value = "on"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }

  tags = merge(var.common_tags, {
    Name     = "${var.name_prefix}-db-params"
    Service  = "database"
    DataType = "tenant-data"
  })
}

# RDS Instance
resource "aws_db_instance" "main" {
  identifier = "${var.name_prefix}-postgres"

  # Engine configuration
  engine         = "postgres"
  engine_version = "16.6"
  instance_class = var.instance_class

  # Storage configuration
  allocated_storage     = var.allocated_storage
  max_allocated_storage = var.max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true

  # Database configuration
  db_name  = var.db_name
  username = var.username

  # IAM database authentication (passwordless)
  # The app generates short-lived auth tokens using generate_db_auth_token()
  iam_database_authentication_enabled = true

  # AWS-managed master password. A secret is created in Secrets Manager,
  # rotated automatically, and the plaintext value is never in Terraform
  # state. Use the `master_user_secret_arn` output to read it just-in-time
  # for bootstrap tasks. IAM auth remains the primary auth for runtime.
  manage_master_user_password = true

  # Network configuration
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [var.rds_security_group_id]
  publicly_accessible    = false

  # Parameter and option groups
  parameter_group_name = aws_db_parameter_group.main.name

  # Backup configuration
  backup_retention_period = var.backup_retention_period
  backup_window           = var.backup_window
  maintenance_window      = var.maintenance_window
  copy_tags_to_snapshot   = true

  # High availability
  multi_az = var.multi_az

  # Monitoring
  monitoring_interval = 60
  monitoring_role_arn = aws_iam_role.rds_enhanced_monitoring.arn

  # Performance Insights
  performance_insights_enabled          = true
  performance_insights_retention_period = 7

  # Deletion protection
  deletion_protection       = var.environment == "prod" ? true : false
  skip_final_snapshot       = var.environment == "prod" ? false : true
  final_snapshot_identifier = var.environment == "prod" ? "${var.name_prefix}-postgres-final-snapshot-${formatdate("YYYY-MM-DD-hhmm", timestamp())}" : null

  # Automated minor version upgrades
  auto_minor_version_upgrade = true

  # Apply changes immediately for non-prod environments
  apply_immediately = var.environment == "prod" ? false : true

  tags = merge(var.common_tags, {
    Name     = "${var.name_prefix}-postgres"
    Service  = "database"
    Backup   = "daily"
    DataType = "tenant-data"
  })

  depends_on = [
    aws_db_subnet_group.main,
    aws_db_parameter_group.main,
    aws_iam_role.rds_enhanced_monitoring
  ]
}

# IAM role for RDS enhanced monitoring
resource "aws_iam_role" "rds_enhanced_monitoring" {
  name = "${var.name_prefix}-rds-enhanced-monitoring"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "monitoring.rds.amazonaws.com"
        }
      }
    ]
  })

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-rds-enhanced-monitoring"
    Service = "database"
    Purpose = "enhanced-monitoring"
  })
}

resource "aws_iam_role_policy_attachment" "rds_enhanced_monitoring" {
  role       = aws_iam_role.rds_enhanced_monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

# Read replica for production environment (optional)
resource "aws_db_instance" "read_replica" {
  count = var.environment == "prod" && var.create_read_replica ? 1 : 0

  identifier = "${var.name_prefix}-postgres-replica"

  # Replicate from main instance
  replicate_source_db = aws_db_instance.main.identifier

  # Instance configuration (can be different from main)
  instance_class = var.replica_instance_class != "" ? var.replica_instance_class : var.instance_class

  # Monitoring
  monitoring_interval = 60
  monitoring_role_arn = aws_iam_role.rds_enhanced_monitoring.arn

  # Performance Insights
  performance_insights_enabled          = true
  performance_insights_retention_period = 7

  # Network configuration (inherited from source but can be overridden)
  publicly_accessible = false

  # Auto minor version upgrade
  auto_minor_version_upgrade = true

  tags = merge(var.common_tags, {
    Name     = "${var.name_prefix}-postgres-replica"
    Service  = "database"
    Role     = "read-replica"
    DataType = "tenant-data"
  })
}

# CloudWatch log group for RDS logs
resource "aws_cloudwatch_log_group" "rds_log_group" {
  name              = "/aws/rds/instance/${aws_db_instance.main.identifier}/postgresql"
  retention_in_days = 30

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-rds-logs"
    Service = "database"
  })
}