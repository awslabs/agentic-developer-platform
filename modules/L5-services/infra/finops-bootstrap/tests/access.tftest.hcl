mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
}

variables {
  environment       = "dev"
  aws_region        = "us-east-1"
  aws_account_id    = "000000000000"
  gateway_role_name = "example-gateway"
  archive_bucket    = "example-private-archive"
}

run "original_answer_version_read" {
  command = plan

  assert {
    condition = jsondecode(aws_iam_role_policy.gateway_answer_read.policy).Statement == [{
      Sid      = "FinOpsOriginalAnswerRead"
      Effect   = "Allow"
      Action   = ["s3:GetObjectVersion"]
      Resource = "arn:aws:s3:::example-private-archive/l5/finops/work/*/native/answers/original.txt"
    }]
    error_message = "Gateway may read only versioned original FinOps answers, not current objects or other service artifacts."
  }

  assert {
    condition     = aws_iam_role_policy.gateway_answer_read.role == var.gateway_role_name
    error_message = "The bootstrap grant must target the selected existing Gateway role."
  }
}
