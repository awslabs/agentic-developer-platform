# Cognito User Pool Outputs
output "cognito_user_pool_id" {
  description = "ID of the Cognito User Pool"
  value       = aws_cognito_user_pool.main.id
}

output "cognito_user_pool_arn" {
  description = "ARN of the Cognito User Pool"
  value       = aws_cognito_user_pool.main.arn
}

output "cognito_user_pool_endpoint" {
  description = "Endpoint of the Cognito User Pool"
  value       = aws_cognito_user_pool.main.endpoint
}

# Cognito User Pool Client Outputs
output "cognito_user_pool_client_id" {
  description = "ID of the Cognito User Pool Client"
  value       = aws_cognito_user_pool_client.main.id
}

output "cognito_user_pool_client_name" {
  description = "Name of the Cognito User Pool Client"
  value       = aws_cognito_user_pool_client.main.name
}

# Cognito Identity Pool Outputs
output "cognito_identity_pool_id" {
  description = "ID of the Cognito Identity Pool"
  value       = aws_cognito_identity_pool.main.id
}

output "cognito_identity_pool_arn" {
  description = "ARN of the Cognito Identity Pool"
  value       = aws_cognito_identity_pool.main.arn
}

# Cognito Domain Output
output "cognito_domain" {
  description = "Cognito User Pool domain"
  value       = aws_cognito_user_pool_domain.main.domain
}

output "cognito_domain_cloudfront_distribution" {
  description = "CloudFront distribution for custom domain (if using custom domain)"
  value       = aws_cognito_user_pool_domain.main.cloudfront_distribution
}

output "cognito_hosted_ui_url" {
  description = "URL for the Cognito Hosted UI"
  value       = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${data.aws_region.current.id}.amazoncognito.com"
}

# IAM Role Outputs
output "gateway_caller_role_arn" {
  description = "ARN of the gateway-caller IAM role for authenticated users"
  value       = aws_iam_role.gateway_caller.arn
}

output "gateway_caller_role_name" {
  description = "Name of the gateway-caller IAM role"
  value       = aws_iam_role.gateway_caller.name
}

output "unauthenticated_role_arn" {
  description = "ARN of the unauthenticated IAM role"
  value       = aws_iam_role.unauthenticated.arn
}

# Configuration Summary
output "cognito_config_summary" {
  description = "Summary of Cognito configuration for application use"
  value = {
    user_pool_id     = aws_cognito_user_pool.main.id
    client_id        = aws_cognito_user_pool_client.main.id
    identity_pool_id = aws_cognito_identity_pool.main.id
    region           = data.aws_region.current.id
    hosted_ui_url    = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${data.aws_region.current.id}.amazoncognito.com"
  }
}

# OAuth Configuration for Frontend
output "cognito_oauth_config" {
  description = "OAuth configuration needed for frontend PKCE flow"
  value = {
    user_pool_id  = aws_cognito_user_pool.main.id
    client_id     = aws_cognito_user_pool_client.main.id
    domain        = aws_cognito_user_pool_domain.main.domain
    region        = data.aws_region.current.id
    authorize_url = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${data.aws_region.current.id}.amazoncognito.com/oauth2/authorize"
    token_url     = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${data.aws_region.current.id}.amazoncognito.com/oauth2/token"
    logout_url    = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${data.aws_region.current.id}.amazoncognito.com/logout"
    jwks_url      = "https://cognito-idp.${data.aws_region.current.id}.amazonaws.com/${aws_cognito_user_pool.main.id}/.well-known/jwks.json"
    issuer        = "https://cognito-idp.${data.aws_region.current.id}.amazonaws.com/${aws_cognito_user_pool.main.id}"
  }
}

# =============================================================================
# Resource Server Outputs (Issue #119)
# =============================================================================

output "resource_server_identifier" {
  description = "Identifier of the Cognito Resource Server"
  value       = aws_cognito_resource_server.gateway.identifier
}

output "resource_server_scopes" {
  description = "Available OAuth2 scopes from the Resource Server"
  value       = aws_cognito_resource_server.gateway.scope_identifiers
}

# =============================================================================
# Agent App Client Outputs (Issue #119)
# =============================================================================

output "agent_client_id" {
  description = "ID of the Agent App Client (for client_credentials flow)"
  value       = aws_cognito_user_pool_client.agent.id
}

output "agent_client_name" {
  description = "Name of the Agent App Client"
  value       = aws_cognito_user_pool_client.agent.name
}

# Note: Client secret should be retrieved via AWS CLI or Console
# Do NOT output the secret in Terraform state
output "agent_client_secret_warning" {
  description = "Warning about agent client secret"
  value       = "Client secret must be retrieved via AWS CLI: aws cognito-idp describe-user-pool-client --user-pool-id ${aws_cognito_user_pool.main.id} --client-id ${aws_cognito_user_pool_client.agent.id}"
}

# =============================================================================
# Token Endpoint for M2M Authentication (Issue #119)
# =============================================================================

output "token_endpoint" {
  description = "OAuth2 token endpoint for client_credentials flow"
  value       = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${data.aws_region.current.id}.amazoncognito.com/oauth2/token"
}

# =============================================================================
# Secrets Manager Outputs (Issue #124)
# =============================================================================

output "agent_credentials_secret_arn" {
  description = "ARN of the Secrets Manager secret containing agent credentials"
  value       = aws_secretsmanager_secret.agent_cognito_creds.arn
}

output "agent_credentials_secret_name" {
  description = "Name of the Secrets Manager secret containing agent credentials"
  value       = aws_secretsmanager_secret.agent_cognito_creds.name
}
