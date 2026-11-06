mock_provider "aws" {
  mock_data "aws_partition" { defaults = { partition = "aws" } }
  mock_resource "aws_ecr_repository" {
    defaults = {
      arn            = "arn:aws:ecr:us-east-1:000000000000:repository/adp-dev-l5-finops-executor"
      repository_url = "000000000000.dkr.ecr.us-east-1.amazonaws.com/adp-dev-l5-finops-executor"
    }
  }
}

variables {
  aws_region     = "us-east-1"
  aws_account_id = "000000000000"
  environment    = "dev"
}

run "private_immutable_executor_repository" {
  command = apply
  assert {
    condition     = aws_ecr_repository.executor.name == "adp-dev-l5-finops-executor" && aws_ecr_repository.executor.image_tag_mutability == "IMMUTABLE"
    error_message = "FinOps image publication requires a service-owned immutable repository."
  }
  assert {
    condition     = jsondecode(aws_ecr_repository_policy.executor.policy).Statement[0].Condition.ArnEquals["aws:SourceArn"] == "arn:aws:lambda:us-east-1:000000000000:function:adp-dev-l5-finops-executor"
    error_message = "Only the selected private FinOps Lambda may pull the image."
  }
}
