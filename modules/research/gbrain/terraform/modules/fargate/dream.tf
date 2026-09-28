# Build both containers from configuration. Provider-added defaults in the
# registered serve task must not change the dream task during apply.
# Batch maintenance has its own command and lifecycle. It must not inherit the
# server command override or HTTP health check, or be pinned to an old serve ARN.
resource "aws_ecs_task_definition" "dream" {
  lifecycle {
    precondition {
      condition     = can(regex("@sha256:[0-9a-f]{64}$", var.container_image))
      error_message = "Scheduled tasks require a registry-verified image digest."
    }
  }

  family                   = "${var.name_prefix}-dream"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.cpu
  memory                   = var.memory
  task_role_arn            = var.task_role_arn
  execution_role_arn       = var.execution_role_arn

  container_definitions = jsonencode([merge(
    { for key, value in local.serve_container : key => value
      if !contains(["healthCheck", "portMappings", "command", "entryPoint", "secrets"], key)
    },
    {
      entryPoint = ["/bin/sh", "-c"]
      command    = [file("${path.module}/dream-command.sh")]
      # Maintenance needs database credentials, not the HTTP service token.
      secrets = [for secret in local.serve_container.secrets : secret
        if secret.name != "GBRAIN_MCP_TOKEN"
      ]
    }
  )])
}
