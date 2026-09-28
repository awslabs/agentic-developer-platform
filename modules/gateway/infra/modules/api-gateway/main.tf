# =============================================================================
# API Gateway REST API Module (Issue #236, Issue #260, Issue #42)
# =============================================================================
# Creates an API Gateway REST API with response streaming as an alternate route
# to the internal ALB. This provides 15-minute timeout support for long-running
# LLM requests (vs CloudFront's 60s hard limit).
#
# Architecture (Issue #42 — VPC Link v2 + ALB direct):
# Client -> API Gateway REST API (regional) -> VPC Link v2 -> ALB -> EKS
#
# The VPC Link v2 (apigatewayv2 namespace) connects directly to the ALB via
# subnets + security groups. No NLB is required. The ALB target is set at
# integration time via `--integration-target`, not at VPC Link creation time.
#
# Per: https://aws.amazon.com/blogs/compute/build-scalable-rest-apis-using-
# amazon-api-gateway-private-integration-with-application-load-balancer/
#
# This route coexists with the existing CloudFront -> VPC Origin -> ALB path.
# Clients choose which endpoint to use based on their needs.
#
# Issue #260: Dual-Path Architecture
# - /{proxy+}        -> NONE auth (humans with JWT -- FastAPI validates)
# - /agent/{proxy+}  -> AWS_IAM auth (agents with SigV4 -- API Gateway validates)
#
# NOTE on integrationTarget: Despite initial expectations, the OpenAPI
# `x-amazon-apigateway-integration` extension DOES accept `integrationTarget`
# when using a VPC Link v2. API Gateway requires it — put-rest-api rejects
# v2 VPC Link integrations that lack integrationTarget with the error:
# "IntegrationTarget is required for VpcLinkV2 <id>". This was discovered
# during deployment (see PR #46 description). Using inline integrationTarget
# is cleaner than a null_resource + local-exec post-deploy approach.
# =============================================================================

# =============================================================================
# VPC Link v2 for Private ALB Integration (Issue #42)
# =============================================================================
# Uses aws_apigatewayv2_vpc_link (v2 namespace) which takes subnets + SGs,
# NOT target_arns. This allows direct ALB integration without an NLB.
# The v2 VPC Link ID is referenced by the REST API (v1) integrations via
# connectionId, and the ALB is bound via --integration-target at integration
# creation time.

resource "aws_apigatewayv2_vpc_link" "main" {
  name               = "${var.name_prefix}-vpc-link-v2"
  subnet_ids         = var.private_subnet_ids
  security_group_ids = [aws_security_group.vpc_link.id]

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-vpc-link-v2"
    Service = "api-gateway"
    Purpose = "alb-integration-v2"
  })
}

# =============================================================================
# Security Group for VPC Link v2 (Issue #42)
# =============================================================================
# Egress to the ALB's security group on port 80 (HTTP).
# The ALB SG must allow inbound from this SG — handled by the ingress rule below.

resource "aws_security_group" "vpc_link" {
  name_prefix = "${var.name_prefix}-vpc-link-v2-"
  description = "API Gateway VPC Link v2 to ALB (Issue #42)"
  vpc_id      = var.vpc_id

  # Egress to ALB SG(s) — only created when ALB SG IDs are provided.
  # On initial deploy (before ALB exists), alb_security_group_ids is []
  # and no egress rules are created. The deploy workflow adds the SG IDs
  # once the EKS Ingress ALB is provisioned.
  dynamic "egress" {
    for_each = length(var.alb_security_group_ids) > 0 ? [1] : []
    content {
      description     = "Allow VPC Link to reach ALB on port 80"
      from_port       = 80
      to_port         = 80
      protocol        = "tcp"
      security_groups = var.alb_security_group_ids
    }
  }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-vpc-link-v2-sg"
    Service = "api-gateway"
    Purpose = "vpc-link-v2-security"
  })
}

# Allow ALB to accept inbound traffic from the VPC Link SG
resource "aws_security_group_rule" "alb_from_vpc_link" {
  count = length(var.alb_security_group_ids)

  description              = "Allow inbound from API Gateway VPC Link v2 (Issue #42)"
  type                     = "ingress"
  from_port                = 80
  to_port                  = 80
  protocol                 = "tcp"
  security_group_id        = var.alb_security_group_ids[count.index]
  source_security_group_id = aws_security_group.vpc_link.id
}

# =============================================================================
# API Gateway REST API (Regional) -- OpenAPI Definition
# =============================================================================
# Using Swagger 2.0 body to set responseTransferMode: STREAM on integrations
# and proper AWS_IAM auth via x-amazon-apigateway-auth at method level.
#
# Issue #42: integrationTarget (ALB ARN) IS supported in the OpenAPI body
# when using VPC Link v2. API Gateway requires it -- put-rest-api rejects
# v2 VPC Link integrations that lack integrationTarget. This was discovered
# during deployment when the body without integrationTarget was rejected with:
# "IntegrationTarget is required for VpcLinkV2 <id>"

resource "aws_api_gateway_rest_api" "main" {
  name        = "${var.name_prefix}-api"
  description = "Bedrock Gateway REST API with response streaming (Issue #236)"

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  # Issue #260: Dual-path architecture
  # - /{proxy+}        -> NONE auth (humans with JWT -- FastAPI validates)
  # - /agent/{proxy+}  -> AWS_IAM auth (agents with SigV4 -- API Gateway validates)
  #
  # Issue #42: Uses Swagger 2.0 for proper securityDefinitions + x-amazon-apigateway-auth.
  # VPC Link v2 connectionId + integrationTarget set in each integration block.
  body = var.internal_alb_dns != "localhost" && var.internal_alb_dns != "" ? jsonencode({
    swagger = "2.0"
    info = {
      title       = "${var.name_prefix}-api"
      version     = "3.0"
      description = "Issue #42: VPC Link v2 + ALB direct, dual-path auth"
    }
    # Swagger 2.0: securityDefinitions for AWS_IAM (SigV4)
    securityDefinitions = {
      sigv4 = {
        type                           = "apiKey"
        name                           = "Authorization"
        in                             = "header"
        "x-amazon-apigateway-authtype" = "awsSigv4"
      }
    }
    paths = merge({
      # Root path - NONE auth (humans)
      "/" = {
        x-amazon-apigateway-any-method = {
          "x-amazon-apigateway-auth" = { type = "NONE" }
          x-amazon-apigateway-integration = {
            type                 = "http_proxy"
            httpMethod           = "ANY"
            uri                  = "http://${var.internal_alb_dns}/"
            timeoutInMillis      = var.integration_timeout_ms
            responseTransferMode = "STREAM"
            passthroughBehavior  = "when_no_match"
            connectionType       = "VPC_LINK"
            connectionId         = aws_apigatewayv2_vpc_link.main.id
            integrationTarget    = var.internal_alb_arn
          }
        }
      }
      # Proxy path - NONE auth (humans)
      "/{proxy+}" = {
        x-amazon-apigateway-any-method = {
          "x-amazon-apigateway-auth" = { type = "NONE" }
          parameters = [
            {
              name     = "proxy"
              in       = "path"
              required = true
              type     = "string"
            }
          ]
          x-amazon-apigateway-integration = {
            type                 = "http_proxy"
            httpMethod           = "ANY"
            uri                  = "http://${var.internal_alb_dns}/{proxy}"
            timeoutInMillis      = var.integration_timeout_ms
            responseTransferMode = "STREAM"
            passthroughBehavior  = "when_no_match"
            connectionType       = "VPC_LINK"
            connectionId         = aws_apigatewayv2_vpc_link.main.id
            integrationTarget    = var.internal_alb_arn
            requestParameters = {
              "integration.request.path.proxy" = "method.request.path.proxy"
            }
            cacheKeyParameters = ["method.request.path.proxy"]
          }
        }
      }
      # Issue #260: Agent root path - AWS_IAM auth (agents)
      "/agent" = {
        x-amazon-apigateway-any-method = {
          security                   = [{ sigv4 = [] }]
          "x-amazon-apigateway-auth" = { type = "AWS_IAM" }
          x-amazon-apigateway-integration = {
            type                 = "http_proxy"
            httpMethod           = "ANY"
            uri                  = "http://${var.internal_alb_dns}/"
            timeoutInMillis      = var.integration_timeout_ms
            responseTransferMode = "STREAM"
            passthroughBehavior  = "when_no_match"
            connectionType       = "VPC_LINK"
            connectionId         = aws_apigatewayv2_vpc_link.main.id
            integrationTarget    = var.internal_alb_arn
            requestParameters = {
              "integration.request.header.X-Caller-Identity" = "context.identity.userArn"
            }
          }
        }
      }
      # Issue #260: Agent proxy path - AWS_IAM auth (agents)
      "/agent/{proxy+}" = {
        x-amazon-apigateway-any-method = {
          security                   = [{ sigv4 = [] }]
          "x-amazon-apigateway-auth" = { type = "AWS_IAM" }
          parameters = [
            {
              name     = "proxy"
              in       = "path"
              required = true
              type     = "string"
            }
          ]
          x-amazon-apigateway-integration = {
            type                 = "http_proxy"
            httpMethod           = "ANY"
            uri                  = "http://${var.internal_alb_dns}/{proxy}"
            timeoutInMillis      = var.integration_timeout_ms
            responseTransferMode = "STREAM"
            passthroughBehavior  = "when_no_match"
            connectionType       = "VPC_LINK"
            connectionId         = aws_apigatewayv2_vpc_link.main.id
            integrationTarget    = var.internal_alb_arn
            requestParameters = {
              "integration.request.path.proxy"               = "method.request.path.proxy"
              "integration.request.header.X-Caller-Identity" = "context.identity.userArn"
            }
            cacheKeyParameters = ["method.request.path.proxy"]
          }
        }
      }
      # Issue #1108: Internal platform route — AWS_IAM auth (deploy-runner)
      "/internal/{proxy+}" = {
        x-amazon-apigateway-any-method = {
          security                   = [{ sigv4 = [] }]
          "x-amazon-apigateway-auth" = { type = "AWS_IAM" }
          parameters = [
            {
              name     = "proxy"
              in       = "path"
              required = true
              type     = "string"
            }
          ]
          x-amazon-apigateway-integration = {
            type       = "http_proxy"
            httpMethod = "ANY"
            # Preserve the /internal prefix when forwarding to the gateway pod.
            # The Bedrock /agent proxy strips its prefix because the pod serves
            # Bedrock requests at root paths; but the /internal/v1/* routes are
            # registered with the prefix included, so 404s without it.
            uri                  = "http://${var.internal_alb_dns}/internal/{proxy}"
            timeoutInMillis      = var.integration_timeout_ms
            responseTransferMode = "STREAM"
            passthroughBehavior  = "when_no_match"
            connectionType       = "VPC_LINK"
            connectionId         = aws_apigatewayv2_vpc_link.main.id
            integrationTarget    = var.internal_alb_arn
            requestParameters = {
              "integration.request.path.proxy"               = "method.request.path.proxy"
              "integration.request.header.X-Caller-Identity" = "context.identity.userArn"
            }
            cacheKeyParameters = ["method.request.path.proxy"]
          }
        }
      }
      },
      # Issue #1011: GitHub Auth Broker route — Lambda proxy integration
      # Only included when broker_lambda_invoke_arn is provided.
      var.broker_lambda_invoke_arn != "" ? {
        "/auth/github/{proxy+}" = {
          x-amazon-apigateway-any-method = {
            "x-amazon-apigateway-auth" = { type = "NONE" }
            parameters = [
              {
                name     = "proxy"
                in       = "path"
                required = true
                type     = "string"
              }
            ]
            x-amazon-apigateway-integration = {
              type                = "aws_proxy"
              httpMethod          = "POST"
              uri                 = var.broker_lambda_invoke_arn
              passthroughBehavior = "when_no_match"
              contentHandling     = "CONVERT_TO_TEXT"
              timeoutInMillis     = 29000
            }
          }
        }
      } : {}
    )
    }) : jsonencode({
    swagger = "2.0"
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
}

# =============================================================================
# CloudWatch Log Group for Access Logging
# =============================================================================

resource "aws_cloudwatch_log_group" "api_gateway" {
  name              = "/aws/api-gateway/${var.name_prefix}-api"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.cloudwatch_kms_key_arn

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

# =============================================================================
# GitHub Auth Broker Lambda Permission (Issue #1011)
# =============================================================================
# Allows API Gateway to invoke the broker Lambda for /auth/github/* routes.

resource "aws_lambda_permission" "broker_api_gateway" {
  # Use the plan-time-known enable flag, NOT broker_lambda_invoke_arn — the
  # latter is the broker Lambda's computed invoke ARN (unknown until apply),
  # which makes count un-evaluable at plan time ("Invalid count argument").
  count = var.enable_broker_route ? 1 : 0

  statement_id  = "AllowAPIGatewayInvokeBroker"
  action        = "lambda:InvokeFunction"
  function_name = var.broker_lambda_function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/*/*"
}
