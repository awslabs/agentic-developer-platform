resource "aws_ecs_cluster" "gbrain" {
  name = var.name_prefix

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "gbrain" {
  cluster_name = aws_ecs_cluster.gbrain.name

  capacity_providers = ["FARGATE"]

  default_capacity_provider_strategy {
    base              = 1
    weight            = 100
    capacity_provider = "FARGATE"
  }
}

resource "aws_ecs_task_definition" "serve" {
  family                   = "${var.name_prefix}-serve"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.cpu
  memory                   = var.memory
  task_role_arn            = var.task_role_arn
  execution_role_arn       = var.execution_role_arn

  container_definitions = jsonencode([{
    name  = "gbrain"
    image = var.container_image

    essential = true

    portMappings = [{
      containerPort = 3000
      protocol      = "tcp"
    }]

    environment = [
      { name = "PORT", value = "3000" },
      { name = "GBRAIN_DB_HOST", value = split(":", var.db_endpoint)[0] },
      { name = "GBRAIN_DB_PORT", value = "5432" },
      { name = "GBRAIN_DB_NAME", value = "gbrain" },
      { name = "GBRAIN_S3_BUCKET", value = var.s3_bucket_name },
      { name = "AWS_REGION", value = "us-east-1" },
    ]

    secrets = [
      {
        name      = "GBRAIN_DB_PASSWORD"
        valueFrom = "${var.db_credentials_arn}:password::"
      },
      {
        name      = "GBRAIN_DB_USER"
        valueFrom = "${var.db_credentials_arn}:username::"
      },
      {
        name      = "GBRAIN_MCP_TOKEN"
        valueFrom = var.mcp_token_arn
      }
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = var.log_group_name
        "awslogs-region"        = "us-east-1"
        "awslogs-stream-prefix" = "gbrain"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "curl -f http://localhost:3000/health || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }
  }])
}

resource "aws_ecs_service" "mcp" {
  name            = "${var.name_prefix}-mcp"
  cluster         = aws_ecs_cluster.gbrain.id
  task_definition = aws_ecs_task_definition.serve.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  enable_execute_command = true

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [var.service_sg_id]
    assign_public_ip = false
  }

  # Allow Terraform to update tasks without forcing new deployment every time
  lifecycle {
    ignore_changes = [desired_count]
  }
}
