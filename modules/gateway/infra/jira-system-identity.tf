# Admin-managed Jira credentials stay in AgentCore Identity. Core may register
# providers; only the separate adapter runtime may retrieve their API keys.
locals {
  gateway_jira_system_workload_statements = [
    {
      Sid    = "JiraSystemWorkloadSetup"
      Effect = "Allow"
      Action = ["bedrock-agentcore:GetWorkloadIdentity", "bedrock-agentcore:CreateWorkloadIdentity"]
      Resource = [
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default",
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default/workload-identity/adp-tools-${var.environment}-atlassian-mcp-system"
      ]
    }
  ]
  gateway_jira_system_provider_statements = [
    {
      Sid    = "JiraSystemProviderSetup"
      Effect = "Allow"
      Action = ["bedrock-agentcore:CreateApiKeyCredentialProvider", "bedrock-agentcore:UpdateApiKeyCredentialProvider", "bedrock-agentcore:DeleteApiKeyCredentialProvider"]
      Resource = [
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:token-vault/default",
        "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:token-vault/default/apikeycredentialprovider/adp-tools-${var.environment}-atlassian-mcp-system-*"
      ]
    }
  ]
}
