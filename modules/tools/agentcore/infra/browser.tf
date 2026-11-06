locals {
  browser_active = var.enabled && var.browser_http_enabled ? 1 : 0
}

resource "aws_sqs_queue" "browser_dlq" {
  count                     = local.browser_active
  name                      = "${var.name}-browser-dead.fifo"
  fifo_queue                = true
  sqs_managed_sse_enabled   = true
  message_retention_seconds = 3600
}
resource "aws_sqs_queue" "browser" {
  count                      = local.browser_active
  name                       = "${var.name}-browser.fifo"
  fifo_queue                 = true
  sqs_managed_sse_enabled    = true
  visibility_timeout_seconds = 240
  message_retention_seconds  = 3600
  redrive_policy             = jsonencode({ deadLetterTargetArn = aws_sqs_queue.browser_dlq[0].arn, maxReceiveCount = 3 })
}
resource "aws_dynamodb_table" "browser" {
  count                       = local.browser_active
  name                        = "${var.name}-browser-operations"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "event_id"
  range_key                   = "arrived_at"
  deletion_protection_enabled = true
  attribute {
    name = "event_id"
    type = "S"
  }
  attribute {
    name = "arrived_at"
    type = "S"
  }
  attribute {
    name = "cleanup_partition"
    type = "S"
  }
  attribute {
    name = "next_cleanup_at"
    type = "N"
  }
  global_secondary_index {
    name            = "cleanup-due"
    hash_key        = "cleanup_partition"
    range_key       = "next_cleanup_at"
    projection_type = "ALL"
  }
  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true }
}
resource "aws_iam_role" "browser_service" {
  count = local.browser_active
  name  = "${var.name}-browser-service"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = var.browser_oidc_provider_arn },
    Condition = { StringEquals = {
      "${replace(var.browser_oidc_issuer, "https://", "")}:sub" = "system:serviceaccount:${var.browser_namespace}:${var.name}-browser"
      "${replace(var.browser_oidc_issuer, "https://", "")}:aud" = "sts.amazonaws.com"
    } }
  }] })
}
resource "aws_iam_role_policy" "browser_service" {
  count = local.browser_active
  name  = "browser-operations"
  role  = aws_iam_role.browser_service[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = "execute-api:Invoke", Resource = sort(tolist(var.authority_invoke_arns)) },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:ConditionCheckItem"], Resource = aws_dynamodb_table.browser[0].arn },
    { Effect = "Allow", Action = "dynamodb:Query", Resource = "${aws_dynamodb_table.browser[0].arn}/index/cleanup-due" },
    { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage"], Resource = aws_sqs_queue.browser[0].arn },
    { Effect = "Allow", Action = ["bedrock-agentcore:StartBrowserSession", "bedrock-agentcore:GetBrowserSession", "bedrock-agentcore:ListBrowserSessions", "bedrock-agentcore:StopBrowserSession", "bedrock-agentcore:ConnectBrowserAutomationStream"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } } }
  ], var.browser_gateway_ready ? [{ Effect = "Allow", Action = "kms:Verify", Resource = var.tools_assertion_key_arn }] : []) })
}
resource "kubernetes_service_account" "browser" {
  count = local.browser_active
  metadata {
    name        = "${var.name}-browser"
    namespace   = var.browser_namespace
    annotations = { "eks.amazonaws.com/role-arn" = aws_iam_role.browser_service[0].arn }
  }
}
resource "kubernetes_deployment" "browser" {
  count = local.browser_active
  metadata {
    name      = "${var.name}-browser"
    namespace = var.browser_namespace
  }
  spec {
    replicas = 1
    strategy { type = "Recreate" }
    selector { match_labels = { "app.kubernetes.io/name" = "${var.name}-browser" } }
    template {
      metadata { labels = { "app.kubernetes.io/name" = "${var.name}-browser" } }
      spec {
        service_account_name             = kubernetes_service_account.browser[0].metadata[0].name
        termination_grace_period_seconds = 180
        security_context {
          run_as_non_root = true
          run_as_user     = 1001
          run_as_group    = 1001
        }
        container {
          name    = "browser"
          image   = var.browser_service_image
          command = ["python3", "-m", "agentcore_tools.browser_http"]
          env {
            name  = "AWS_REGION"
            value = var.aws_region
          }
          env {
            name  = "AWS_DEFAULT_REGION"
            value = var.aws_region
          }
          env {
            name  = "ADP_GATEWAY_BROWSER_READY"
            value = tostring(var.browser_gateway_ready)
          }
          env {
            name  = "ADP_TOOLS_ASSERTION_KEY_ARN"
            value = var.tools_assertion_key_arn
          }
          env {
            name  = "ADP_TOOLS_FACADE_ROLE_ARN"
            value = var.tools_facade_role_arn
          }
          env {
            name  = "CYBER_BROWSER_SESSION_SECONDS"
            value = tostring(var.browser_session_seconds)
          }
          env {
            name  = "BROWSER_OPERATIONS_TABLE"
            value = aws_dynamodb_table.browser[0].name
          }
          env {
            name  = "BROWSER_QUEUE_URL"
            value = aws_sqs_queue.browser[0].url
          }
          env {
            name  = "ADP_TASK_AUTHORITY_ENDPOINT"
            value = var.authority_endpoint
          }
          resources {
            requests = { cpu = "250m", memory = "512Mi" }
            limits   = { cpu = "2", memory = "2Gi" }
          }
          volume_mount {
            name       = "tmp"
            mount_path = "/tmp"
          }
        }
        volume {
          name = "tmp"
          empty_dir {}
        }
      }
    }
  }
}
resource "kubernetes_network_policy" "browser" {
  count = local.browser_active
  metadata {
    name      = "${var.name}-browser"
    namespace = var.browser_namespace
  }
  spec {
    pod_selector {
      match_labels = { "app.kubernetes.io/name" = "${var.name}-browser" }
    }
    policy_types = ["Ingress", "Egress"]
    egress {
      ports {
        port     = 443
        protocol = "TCP"
      }
    }
    egress {
      ports {
        port     = 53
        protocol = "UDP"
      }
      ports {
        port     = 53
        protocol = "TCP"
      }
    }
  }
}
output "browser_operations_table" { value = try(aws_dynamodb_table.browser[0].name, null) }
output "browser_deployment_name" { value = try(kubernetes_deployment.browser[0].metadata[0].name, null) }
output "browser_deployment_namespace" { value = try(kubernetes_deployment.browser[0].metadata[0].namespace, null) }
