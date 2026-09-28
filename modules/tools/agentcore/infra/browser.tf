locals {
  browser_active = var.enabled && var.browser_http_enabled ? 1 : 0
  browser_route  = "${var.api_execution_arn}/${var.stage_name}/POST/tools/browser"
}

resource "aws_api_gateway_resource" "browser" {
  count       = local.browser_active
  rest_api_id = var.rest_api_id
  parent_id   = var.tools_parent_resource_id
  path_part   = "browser"
}
resource "aws_api_gateway_method" "browser" {
  count         = local.browser_active
  rest_api_id   = var.rest_api_id
  resource_id   = aws_api_gateway_resource.browser[0].id
  http_method   = "POST"
  authorization = "AWS_IAM"
}
resource "aws_api_gateway_integration" "browser" {
  count                   = local.browser_active
  rest_api_id             = var.rest_api_id
  resource_id             = aws_api_gateway_resource.browser[0].id
  http_method             = "POST"
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.browser[0].invoke_arn
  timeout_milliseconds    = 29000
}
resource "aws_lambda_permission" "browser" {
  count          = local.browser_active
  statement_id   = "SharedApiBrowserRoute"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.browser[0].function_name
  principal      = "apigateway.amazonaws.com"
  source_arn     = local.browser_route
  source_account = var.aws_account_id
}
resource "aws_iam_role_policy" "browser_worker" {
  for_each = local.browser_active == 1 ? var.worker_role_arns : toset([])
  name     = "${var.name}-browser-invoke"
  role     = element(reverse(split("/", each.value)), 0)
  policy   = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = "execute-api:Invoke", Resource = local.browser_route }] })
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
  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true }
}
resource "aws_cloudwatch_log_group" "browser" {
  count             = local.browser_active
  name              = "/aws/lambda/${var.name}-browser"
  retention_in_days = 30
}
resource "aws_iam_role" "browser_gateway" {
  count = local.browser_active
  name  = "${var.name}-browser-gateway"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" }
  }] })
}
resource "aws_iam_role_policy" "browser_gateway" {
  count = local.browser_active
  name  = "browser-gateway"
  role  = aws_iam_role.browser_gateway[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.browser[0].arn}:*" },
    { Effect = "Allow", Action = "execute-api:Invoke", Resource = sort(tolist(var.authority_invoke_arns)) },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:TransactWriteItems"], Resource = aws_dynamodb_table.browser[0].arn },
    { Effect = "Allow", Action = "sqs:SendMessage", Resource = aws_sqs_queue.browser[0].arn }
  ] })
}
resource "aws_lambda_function" "browser" {
  count         = local.browser_active
  function_name = "${var.name}-browser"
  role          = aws_iam_role.browser_gateway[0].arn
  package_type  = "Image"
  image_uri     = var.image_uri
  image_config { command = ["agentcore_tools.browser_http.lambda_handler"] }
  timeout                        = 25
  memory_size                    = 512
  reserved_concurrent_executions = 10
  environment {
    variables = {
      ADP_TASK_BROWSER_HTTP_ENABLED = tostring(var.browser_admission_enabled)
      ADP_TASK_AUTHORITY_ENDPOINT   = var.authority_endpoint
      BROWSER_OPERATIONS_TABLE      = aws_dynamodb_table.browser[0].name
      BROWSER_QUEUE_URL             = aws_sqs_queue.browser[0].url
      CYBER_TOOLS_WORKER_ROLES      = join(",", sort(tolist(var.worker_role_arns)))
    }
  }
  lifecycle {
    precondition {
      condition     = var.browser_service_image != "" && var.browser_namespace != "" && var.browser_oidc_provider_arn != "" && var.browser_oidc_issuer != "" && var.authority_endpoint != ""
      error_message = "Browser requires a pinned service image, private EKS namespace/OIDC and Task authority."
    }
    precondition {
      condition     = !var.browser_admission_enabled || var.browser_http_enabled
      error_message = "Start admission requires the Browser HTTP stack."
    }
  }
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
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "execute-api:Invoke", Resource = sort(tolist(var.authority_invoke_arns)) },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"], Resource = aws_dynamodb_table.browser[0].arn },
    { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage"], Resource = aws_sqs_queue.browser[0].arn },
    { Effect = "Allow", Action = ["bedrock-agentcore:StartBrowserSession", "bedrock-agentcore:GetBrowserSession", "bedrock-agentcore:ListBrowserSessions", "bedrock-agentcore:StopBrowserSession", "bedrock-agentcore:ConnectBrowserAutomationStream"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } } }
  ] })
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
output "browser_route_execution_arn" { value = local.browser_active == 1 ? local.browser_route : null }
output "browser_operations_table" { value = try(aws_dynamodb_table.browser[0].name, null) }
