# Keep this grant separate: runner_services is near IAM's managed-policy size
# limit. Its actions must also be allowed by runner_boundary in main.tf.
resource "aws_iam_role_policy" "bedrock_invocation_logging" {
  name = "bedrock-invocation-logging-deploy"
  role = aws_iam_role.deployment.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # These regional control-plane actions have no resource-level ARN.
        Sid    = "BedrockInvocationLogging"
        Effect = "Allow"
        Action = [
          "bedrock:GetModelInvocationLoggingConfiguration",
          "bedrock:PutModelInvocationLoggingConfiguration",
          "bedrock:DeleteModelInvocationLoggingConfiguration",
          "logs:DescribeLogGroups"
        ]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      },
      {
        Sid    = "BedrockInvocationLogGroups"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup", "logs:DeleteLogGroup",
          "logs:ListTagsForResource", "logs:ListTagsLogGroup",
          "logs:PutRetentionPolicy", "logs:DeleteRetentionPolicy",
          "logs:AssociateKmsKey", "logs:DisassociateKmsKey",
          "logs:TagLogGroup", "logs:TagResource",
          "logs:UntagLogGroup", "logs:UntagResource"
        ]
        Resource = [
          "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock/${var.name_prefix}/model-invocations",
          "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock/${var.name_prefix}/model-invocations:*"
        ]
      },
      {
        Sid      = "BedrockLogBucketOwnership"
        Effect   = "Allow"
        Action   = ["s3:GetBucketOwnershipControls", "s3:PutBucketOwnershipControls", "s3:DeleteBucketOwnershipControls"]
        Resource = "arn:aws:s3:::${var.name_prefix}-bedrock-logs-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
      }
    ]
  })
}
