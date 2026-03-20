# =============================================================================
# API Gateway REST API Module (Issue #236, Issue #260)
# =============================================================================
# Creates an API Gateway REST API with response streaming as an alternate route
# to the internal ALB. This provides 15-minute timeout support for long-running
# LLM requests (vs CloudFront's 60s hard limit).
#
# Architecture:
# Client -> API Gateway REST API (regional) -> VPC Link -> Internal ALB -> EKS
#
# This route coexists with the existing CloudFront -> VPC Origin -> ALB path.
# Clients choose which endpoint to use based on their needs.
#
# Issue #260: Dual-Path Architecture
# - /{proxy+}        → NONE auth (humans with JWT — FastAPI validates)
# - /agent/{proxy+}  → AWS_IAM auth (agents with SigV4 — API Gateway validates)
#
# NOTE: The Terraform AWS provider does not yet support the
# `response_transfer_mode` attribute on aws_api_gateway_integration.
# We use an OpenAPI body definition with x-amazon-apigateway-integration
# to set responseTransferMode: STREAM, which enables 15-minute timeouts.
# =============================================================================

# =============================================================================
# VPC Link for Private ALB Integration
# =============================================================================
# VPC Link connects API Gateway directly to the internal ALB.
# Created conditionally — the ALB ARN is dynamic (created by EKS Ingress).
# The backend-deploy workflow will create/update this after the ALB exists.

resource "aws_api_gateway_vpc_link" "main" {
  count = var.internal_alb_arn != "" ? 1 : 0

  name        = "${var.name_prefix}-vpc-link"
  description = "VPC Link to internal ALB for API Gateway (Issue #236)"
  target_arns = [var.internal_alb_arn]

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-api-gateway-vpc-link"
    Service = "api-gateway"
    Purpose = "alb-integration"
  })
}

# =============================================================================
# API Gateway REST API (Regional) — OpenAPI Definition
# =============================================================================
# Using OpenAPI body to set responseTransferMode: STREAM on integrations,
# since the Terraform provider doesn't expose this attribute natively.

resource "aws_api_gateway_rest_api" "main" {
  name        = "${var.name_prefix}-api"
  description = "Bedrock Gateway REST API with response streaming (Issue #236)"

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  # NOTE: The OpenAPI body is only applied when internal_alb_dns is provided.
  # When ALB DNS is not yet known (initial apply), we create a minimal API
  # without integrations. The backend-deploy workflow will configure the
  # integrations with responseTransferMode: STREAM via AWS CLI after the
  # EKS Ingress ALB is created.
  #
  # Issue #260: Dual-path architecture
  # - /{proxy+}        → NONE auth (humans with JWT — FastAPI validates)
  # - /agent/{proxy+}  → AWS_IAM auth (agents with SigV4 — API Gateway validates)
  body = var.internal_alb_dns != "localhost" && var.internal_alb_dns != "" ? jsonencode({
    openapi = "3.0.1"
    info = {
      title       = "${var.name_prefix}-api"
      version     = "2.0"
      description = "Issue #260: Dual-path API Gateway with NONE and AWS_IAM auth"
    }
    # Issue #260: Define AWS_IAM security scheme
    securityDefinitions = {
      sigv4 = {
        type                         = "apiKey"
        name                         = "Authorization"
        in                           = "header"
        "x-amazon-apigateway-authtype" = "awsSigv4"
      }
    }
    paths = {
      # Root path - NONE auth (humans)
      "/" = {
        x-amazon-apigateway-any-method = {
          security = []
          x-amazon-apigateway-integration = merge(
            {
              type                 = "http_proxy"
              httpMethod           = "ANY"
              uri                  = "http://${var.internal_alb_dns}/"
              timeoutInMillis      = var.integration_timeout_ms
              responseTransferMode = "STREAM"
              passthroughBehavior  = "when_no_match"
            },
            var.internal_alb_arn != "" ? {
              connectionType = "VPC_LINK"
              connectionId   = aws_api_gateway_vpc_link.main[0].id
            } : {
              connectionType = "INTERNET"
            }
          )
        }
      }
      # Proxy path - NONE auth (humans)
      "/{proxy+}" = {
        x-amazon-apigateway-any-method = {
          security = []
          parameters = [
            {
              name     = "proxy"
              in       = "path"
              required = true
              schema   = { type = "string" }
            }
          ]
          x-amazon-apigateway-integration = merge(
            {
              type                 = "http_proxy"
              httpMethod           = "ANY"
              uri                  = "http://${var.internal_alb_dns}/{proxy}"
              timeoutInMillis      = var.integration_timeout_ms
              responseTransferMode = "STREAM"
              passthroughBehavior  = "when_no_match"
              requestParameters = {
                "integration.request.path.proxy" = "method.request.path.proxy"
              }
              cacheKeyParameters = ["method.request.path.proxy"]
            },
            var.internal_alb_arn != "" ? {
              connectionType = "VPC_LINK"
              connectionId   = aws_api_gateway_vpc_link.main[0].id
            } : {
              connectionType = "INTERNET"
            }
          )
        }
      }
      # Issue #260: Agent root path - AWS_IAM auth (agents)
      "/agent" = {
        x-amazon-apigateway-any-method = {
          security = [{ sigv4 = [] }]
          x-amazon-apigateway-integration = merge(
            {
              type                 = "http_proxy"
              httpMethod           = "ANY"
              uri                  = "http://${var.internal_alb_dns}/"
              timeoutInMillis      = var.integration_timeout_ms
              responseTransferMode = "STREAM"
              passthroughBehavior  = "when_no_match"
              requestParameters = {
                "integration.request.header.X-Caller-Identity" = "context.identity.userArn"
              }
            },
            var.internal_alb_arn != "" ? {
              connectionType = "VPC_LINK"
              connectionId   = aws_api_gateway_vpc_link.main[0].id
            } : {
              connectionType = "INTERNET"
            }
          )
        }
      }
      # Issue #260: Agent proxy path - AWS_IAM auth (agents)
      "/agent/{proxy+}" = {
        x-amazon-apigateway-any-method = {
          security = [{ sigv4 = [] }]
          parameters = [
            {
              name     = "proxy"
              in       = "path"
              required = true
              schema   = { type = "string" }
            }
          ]
          x-amazon-apigateway-integration = merge(
            {
              type                 = "http_proxy"
              httpMethod           = "ANY"
              uri                  = "http://${var.internal_alb_dns}/{proxy}"
              timeoutInMillis      = var.integration_timeout_ms
              responseTransferMode = "STREAM"
              passthroughBehavior  = "when_no_match"
              requestParameters = {
                "integration.request.path.proxy"               = "method.request.path.proxy"
                "integration.request.header.X-Caller-Identity" = "context.identity.userArn"
              }
              cacheKeyParameters = ["method.request.path.proxy"]
            },
            var.internal_alb_arn != "" ? {
              connectionType = "VPC_LINK"
              connectionId   = aws_api_gateway_vpc_link.main[0].id
            } : {
              connectionType = "INTERNET"
            }
          )
        }
      }
    }
  }) : jsonencode({
    openapi = "3.0.1"
    info = {
      title   = "${var.name_prefix}-api"
      version = "1.0"
    }
    paths = {
      "/status" = {
        get = {
          x-amazon-apigateway-integration = {
            type = "MOCK"
            requestTemplates = {
              "application/json" = "{\"statusCode\": 200}"
            }
            responses = {
              default = {
                statusCode = "200"
                responseTemplates = {
                  "application/json" = "{\"status\":\"awaiting-backend\",\"message\":\"API Gateway created. Backend ALB not yet configured.\"}"
                }
              }
            }
          }
        }
      }
    }
  })

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-api-gateway"
    Service = "api-gateway"
    Purpose = "llm-streaming-alternate-route"
  })

  # The body will be updated by the backend-deploy workflow via AWS CLI
  # when the ALB DNS becomes available. No lifecycle ignore needed since
  # Terraform manages the initial creation and the deploy workflow handles
  # runtime updates via put-rest-api.
}

# =============================================================================
# CloudWatch Log Group for Access Logging
# =============================================================================

resource "aws_cloudwatch_log_group" "api_gateway" {
  name              = "/aws/api-gateway/${var.name_prefix}-api"
  retention_in_days = var.log_retention_days

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-api-gateway-logs"
    Service = "cloudwatch"
    Purpose = "api-gateway-access-logs"
  })
}

# =============================================================================
# API Gateway Deployment
# =============================================================================

resource "aws_api_gateway_deployment" "main" {
  rest_api_id = aws_api_gateway_rest_api.main.id

  triggers = {
    redeployment = sha1(jsonencode(coalesce(aws_api_gateway_rest_api.main.body, "initial")))
  }

  lifecycle {
    create_before_destroy = true
  }
}

# =============================================================================
# API Gateway Stage
# =============================================================================

resource "aws_api_gateway_stage" "main" {
  deployment_id = aws_api_gateway_deployment.main.id
  rest_api_id   = aws_api_gateway_rest_api.main.id
  stage_name    = var.environment

  xray_tracing_enabled = var.enable_xray_tracing

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gateway.arn
    format = jsonencode({
      requestId               = "$context.requestId"
      sourceIp                = "$context.identity.sourceIp"
      requestTime             = "$context.requestTime"
      protocol                = "$context.protocol"
      httpMethod              = "$context.httpMethod"
      resourcePath            = "$context.resourcePath"
      status                  = "$context.status"
      responseLength          = "$context.responseLength"
      integrationErrorMessage = "$context.integrationErrorMessage"
      integrationLatency      = "$context.integrationLatency"
      responseLatency         = "$context.responseLatency"
    })
  }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-api-gateway-stage"
    Service = "api-gateway"
    Purpose = "deployment-stage"
  })

  depends_on = [aws_cloudwatch_log_group.api_gateway]
}

# =============================================================================
# API Gateway Method Settings (Throttling and Metrics)
# =============================================================================

resource "aws_api_gateway_method_settings" "all" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  stage_name  = aws_api_gateway_stage.main.stage_name
  method_path = "*/*"

  settings {
    metrics_enabled    = true
    logging_level      = "INFO"
    data_trace_enabled = var.environment != "prod"

    throttling_burst_limit = var.throttle_burst_limit
    throttling_rate_limit  = var.throttle_rate_limit

    caching_enabled = false
  }
}

# =============================================================================
# IAM Role for API Gateway CloudWatch Logging
# =============================================================================

data "aws_iam_policy_document" "api_gateway_assume_role" {
  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["apigateway.amazonaws.com"]
    }
    actions = ["sts:AssumeRole"]
  }
}

resource "aws_iam_role" "api_gateway_cloudwatch" {
  name               = "${var.name_prefix}-api-gateway-cloudwatch"
  assume_role_policy = data.aws_iam_policy_document.api_gateway_assume_role.json

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-api-gateway-cloudwatch-role"
    Service = "iam"
    Purpose = "api-gateway-logging"
  })
}

resource "aws_iam_role_policy_attachment" "api_gateway_cloudwatch" {
  role       = aws_iam_role.api_gateway_cloudwatch.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

resource "aws_api_gateway_account" "main" {
  cloudwatch_role_arn = aws_iam_role.api_gateway_cloudwatch.arn
  depends_on          = [aws_iam_role_policy_attachment.api_gateway_cloudwatch]
}
