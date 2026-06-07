# EventBridge rule for the gbrain dream cycle (daily at 3am UTC)
resource "aws_cloudwatch_event_rule" "dream" {
  name                = "${var.name_prefix}-dream-cycle"
  description         = "Triggers gbrain dream cycle — synthesizes learnings daily"
  schedule_expression = "cron(0 3 * * ? *)"
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_target" "dream_task" {
  rule     = aws_cloudwatch_event_rule.dream.name
  arn      = var.cluster_arn
  role_arn = var.role_arn

  ecs_target {
    task_definition_arn = var.task_def
    task_count          = 1
    launch_type         = "FARGATE"

    network_configuration {
      subnets          = var.subnet_ids
      security_groups  = [var.sg_id]
      assign_public_ip = false
    }
  }

  input = jsonencode({
    containerOverrides = [{
      name    = "gbrain"
      command = ["gbrain", "dream", "--non-interactive"]
    }]
  })
}
