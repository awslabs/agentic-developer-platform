# Operator-owned prerequisite. A normal FinOps release cannot change Gateway IAM.
data "aws_partition" "current" {}

resource "aws_iam_role_policy" "gateway_answer_read" {
  name = "adp-${var.environment}-l5-finops-answer-read"
  role = var.gateway_role_name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "FinOpsOriginalAnswerRead"
      Effect   = "Allow"
      Action   = ["s3:GetObjectVersion"]
      Resource = "arn:${data.aws_partition.current.partition}:s3:::${var.archive_bucket}/l5/finops/work/*/native/answers/original.txt"
    }]
  })
}

output "gateway_answer_policy_name" {
  value = aws_iam_role_policy.gateway_answer_read.name
}
