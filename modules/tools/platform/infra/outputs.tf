output "gateway_id" {
  value = aws_bedrockagentcore_gateway.tools.gateway_id
}

output "execution_table_name" {
  value = "adp-tools-${var.environment}-executions"
}

output "execution_table_arn" {
  value = "arn:aws:dynamodb:${var.aws_region}:${var.aws_account_id}:table/adp-tools-${var.environment}-executions"
}

output "gateway_arn" {
  value = aws_bedrockagentcore_gateway.tools.gateway_arn
}

output "shared_tools_lambda_arn" {
  value = length(data.aws_lambda_function.shared_tools) > 0 ? data.aws_lambda_function.shared_tools[0].arn : ""
}

output "target_role_arn" {
  value = var.gateway_role_arn
}

output "gateway_url" {
  value = aws_bedrockagentcore_gateway.tools.gateway_url
}

output "caller_role_arn" {
  description = "The ADP gateway role allowed to invoke this internal tools gateway."
  value       = var.tools_facade_role_arn
}

output "kms_key_arn" {
  value = var.gateway_kms_key_arn
}

output "target_ids" {
  value = merge({ for name, target in aws_bedrockagentcore_gateway_target.tools : name => target.target_id }, { for vendor, target in aws_bedrockagentcore_gateway_target.user_connections : local.oauth_target_names[vendor] => target.target_id }, length(aws_bedrockagentcore_gateway_target.github_user) > 0 ? { github = aws_bedrockagentcore_gateway_target.github_user[0].target_id } : {}, length(aws_bedrockagentcore_gateway_target.jira_user) > 0 ? { jira = aws_bedrockagentcore_gateway_target.jira_user[0].target_id } : {}, length(aws_bedrockagentcore_gateway_target.slack_user) > 0 ? { slack = aws_bedrockagentcore_gateway_target.slack_user[0].target_id } : {}, length(aws_bedrockagentcore_gateway_target.credentials) > 0 ? { credentials = aws_bedrockagentcore_gateway_target.credentials[0].target_id } : {}, length(aws_bedrockagentcore_gateway_target.knowledge) > 0 ? { knowledge = aws_bedrockagentcore_gateway_target.knowledge[0].target_id } : {})
}

output "assertion_signing_key_arn" {
  description = "Asymmetric key for gateway-signed principal assertions; empty until the interceptor is configured."
  value       = var.assertion_signing_key_arn
}

output "oauth_workload_name" {
  description = "User-scoped Identity workload for ADP-managed third-party OAuth connections."
  value       = length(aws_bedrockagentcore_workload_identity.oauth_user) == 0 ? "" : aws_bedrockagentcore_workload_identity.oauth_user[0].name
}

output "gateway_oauth" {
  description = "Non-secret references for the bootstrap-owned outbound OAuth policy. Supply this object as bootstrap's gateway_oauth input before invoking vendor targets."
  value = length(var.oauth_provider_secret_ids) == 0 ? null : {
    workload_identity_arn = aws_bedrockagentcore_gateway.tools.workload_identity_details[0].workload_identity_arn
    providers = {
      for vendor, provider in aws_bedrockagentcore_oauth2_credential_provider.user_connection : vendor => {
        provider_arn = provider.credential_provider_arn
        secret_id    = var.oauth_provider_secret_ids[vendor]
      }
    }
  }
}

output "gateway_workload_identity_arn" {
  description = "The gateway-generated Identity workload, available before OAuth target enablement for bootstrap policy preparation."
  value       = aws_bedrockagentcore_gateway.tools.workload_identity_details[0].workload_identity_arn
}
