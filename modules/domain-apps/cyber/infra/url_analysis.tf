# =============================================================================
# URL Analysis — IAM permissions for AgentCore Browser
# =============================================================================
# Issue #484: Grants the cyber worker role access to Bedrock AgentCore Browser
# for isolated URL analysis sessions.
#
# Permissions are scoped to session lifecycle operations only.
# The worker creates ephemeral sessions, invokes browser actions, and stops
# sessions. No persistent profiles or stored state.
# =============================================================================

resource "aws_iam_role_policy" "cyber_worker_agentcore_browser" {
  name = "agentcore-browser-access"
  role = aws_iam_role.cyber_worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AgentCoreBrowserSessions"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:StartBrowserSession",
          "bedrock-agentcore:InvokeBrowser",
          "bedrock-agentcore:StopBrowserSession",
          "bedrock-agentcore:GetBrowserSession",
          "bedrock-agentcore:ListBrowserSessions",
          # Required for Playwright-over-CDP (DOM/network/form access).
          # Without this the CDP WebSocket returns 403.
          "bedrock-agentcore:ConnectBrowserAutomationStream",
          "bedrock-agentcore:UpdateBrowserStream",
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:RequestedRegion" = var.aws_region
          }
        }
      }
    ]
  })
}
