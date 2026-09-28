# =============================================================================
# URL Analysis — deny direct AgentCore Browser access
# =============================================================================
# URL analysis must cross the guarded broker boundary. The cyber worker handles
# untrusted tasks and must not create, enumerate, or control browser sessions.
#
# Keep an explicit service-wide deny so another identity policy cannot restore
# the obsolete direct-browser path.
# =============================================================================

resource "aws_iam_role_policy" "cyber_worker_agentcore_browser" {
  name = "deny-direct-agentcore-browser"
  role = aws_iam_role.cyber_worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DenyDirectAgentCoreBrowser"
        Effect   = "Deny"
        Action   = ["bedrock-agentcore:*"]
        Resource = "*"
      }
    ]
  })
}
