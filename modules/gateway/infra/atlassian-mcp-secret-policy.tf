# AgentCore reads an EXTERNAL client secret using its own service principal.
# The gateway's role policy cannot grant that service access. See:
# https://aws.amazon.com/blogs/machine-learning/reference-your-own-aws-secrets-manager-secrets-in-amazon-bedrock-agentcore-identity/
# This policy grants no adapter access and applies only to the new MCP client.
resource "aws_secretsmanager_secret_policy" "atlassian_mcp_client" {
  secret_arn          = aws_secretsmanager_secret.atlassian_mcp_client.arn
  block_public_policy = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AtlassianMcpIdentityClientRead"
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.atlassian_mcp_client.arn
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = data.aws_caller_identity.current.account_id
        }
      }
    }]
  })
}
