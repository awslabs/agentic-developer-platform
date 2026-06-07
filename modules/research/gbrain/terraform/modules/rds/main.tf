resource "random_password" "db" {
  length  = 24
  special = false
}

resource "aws_secretsmanager_secret" "db_credentials" {
  name                    = "adp/research/gbrain/db-credentials"
  description             = "RDS credentials for gbrain experimental database"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "db_credentials" {
  secret_id = aws_secretsmanager_secret.db_credentials.id
  secret_string = jsonencode({
    username = "gbrain"
    password = random_password.db.result
    host     = aws_db_instance.gbrain.endpoint
    port     = 5432
    dbname   = var.db_name
  })
}

resource "aws_db_subnet_group" "gbrain" {
  name       = "${var.name_prefix}-db"
  subnet_ids = var.subnet_ids

  tags = {
    Name = "${var.name_prefix}-db-subnet-group"
  }
}

resource "aws_security_group" "db" {
  name_prefix = "${var.name_prefix}-db-"
  description = "gbrain RDS - ingress from Fargate service only"
  vpc_id      = var.vpc_id

  ingress {
    description     = "PostgreSQL from Fargate"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [var.fargate_sg_id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "gbrain" {
  identifier = "${var.name_prefix}-db"

  engine         = "postgres"
  engine_version = "16.14"
  instance_class = var.instance_class

  allocated_storage = var.allocated_storage
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = var.db_name
  username = "gbrain"
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.gbrain.name
  vpc_security_group_ids = [aws_security_group.db.id]

  multi_az            = false
  publicly_accessible = false

  backup_retention_period = 1
  skip_final_snapshot     = true
  deletion_protection     = false

  tags = {
    Name = "${var.name_prefix}-db"
  }
}
