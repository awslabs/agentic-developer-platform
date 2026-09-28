mock_provider "aws" {}

variables {
  environment  = "test"
  aws_region   = "us-east-1"
  name_prefix  = "adp-test-agent-context"
  state_bucket = "adp-terraform-state-123456789012"
  codebuild_service_role_arns = {
    ingestion         = "arn:aws:iam::123456789012:role/codebuild-ingestion"
    codegraph-context = "arn:aws:iam::123456789012:role/codebuild-codegraph-context"
    litellm-proxy     = "arn:aws:iam::123456789012:role/codebuild-litellm-proxy"
    deepwiki          = "arn:aws:iam::123456789012:role/codebuild-deepwiki"
    context-mcp       = "arn:aws:iam::123456789012:role/codebuild-context-mcp"
  }
}

run "each_image_selects_its_own_role" {
  command = apply

  assert {
    condition = alltrue([
      for key, project in aws_codebuild_project.agent_context_images :
      project.service_role == var.codebuild_service_role_arns[key]
    ])
    error_message = "An agent-context CodeBuild project does not select the dedicated role with the same image key."
  }

  assert {
    condition     = length(distinct(values(var.codebuild_service_role_arns))) == length(local.images)
    error_message = "Two agent-context CodeBuild projects share a service role."
  }
}

run "missing_image_role_is_rejected" {
  command = plan

  variables {
    codebuild_service_role_arns = {
      ingestion = "arn:aws:iam::123456789012:role/codebuild-ingestion"
    }
  }

  expect_failures = [var.codebuild_service_role_arns]
}
