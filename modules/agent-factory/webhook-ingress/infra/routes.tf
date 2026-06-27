# =============================================================================
# API Gateway Routes (REST API v1)
# =============================================================================
# POST /github → Lambda proxy integration (HMAC auth inside Lambda)
# POST /agent/trigger → Lambda proxy integration (AWS_IAM auth at API GW)
# =============================================================================

resource "aws_api_gateway_resource" "github" {
  rest_api_id = aws_api_gateway_rest_api.webhook.id
  parent_id   = aws_api_gateway_rest_api.webhook.root_resource_id
  path_part   = "github"
}

resource "aws_api_gateway_method" "post_github" {
  rest_api_id   = aws_api_gateway_rest_api.webhook.id
  resource_id   = aws_api_gateway_resource.github.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "github_webhook" {
  rest_api_id             = aws_api_gateway_rest_api.webhook.id
  resource_id             = aws_api_gateway_resource.github.id
  http_method             = aws_api_gateway_method.post_github.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.github_webhook.invoke_arn
}

# =============================================================================
# POST /agent/trigger — IAM-authenticated agent-to-agent spawn (Issue #2152)
# =============================================================================
# AWS_IAM authorization: callers must SigV4-sign requests. The Lambda receives
# the caller's identity in event.requestContext.identity.userArn. Lineage is
# mandatory and server-resolved from the chain record.
#
# NOTE: When the future GitHub-IP resource-policy restriction lands, it MUST
# exclude execute-api:/*/*/agent/* — agents call from VPC IPs, not GitHub IPs.
# =============================================================================

resource "aws_api_gateway_resource" "agent" {
  rest_api_id = aws_api_gateway_rest_api.webhook.id
  parent_id   = aws_api_gateway_rest_api.webhook.root_resource_id
  path_part   = "agent"
}

resource "aws_api_gateway_resource" "agent_trigger" {
  rest_api_id = aws_api_gateway_rest_api.webhook.id
  parent_id   = aws_api_gateway_resource.agent.id
  path_part   = "trigger"
}

resource "aws_api_gateway_method" "post_agent_trigger" {
  rest_api_id   = aws_api_gateway_rest_api.webhook.id
  resource_id   = aws_api_gateway_resource.agent_trigger.id
  http_method   = "POST"
  authorization = "AWS_IAM"
}

resource "aws_api_gateway_integration" "agent_trigger" {
  rest_api_id             = aws_api_gateway_rest_api.webhook.id
  resource_id             = aws_api_gateway_resource.agent_trigger.id
  http_method             = aws_api_gateway_method.post_agent_trigger.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.github_webhook.invoke_arn
}
