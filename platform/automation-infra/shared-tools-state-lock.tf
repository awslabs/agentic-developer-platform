variable "shared_tools_lock_table" {
  description = "Exact existing DynamoDB backend lock table; empty when the approved backend uses S3 lockfiles."
  type        = string
  default     = ""
  validation {
    condition     = var.shared_tools_lock_table == "" || can(regex("^[A-Za-z0-9_.-]+$", var.shared_tools_lock_table))
    error_message = "Select one exact approved existing backend lock table."
  }
}

resource "aws_iam_role_policy" "shared_tools_state_lock" {
  count = var.enable_shared_tools_deployment && var.shared_tools_lock_table != "" ? 1 : 0
  name  = "shared-tools-existing-state-lock"
  role  = aws_iam_role.shared_tools_deployment[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow"
    Action   = ["dynamodb:DescribeTable", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
    Resource = "arn:aws:dynamodb:${var.aws_region}:${local.shared_account}:table/${var.shared_tools_lock_table}"
  }] })
}
