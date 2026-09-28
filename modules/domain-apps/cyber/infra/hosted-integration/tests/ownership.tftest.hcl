mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111122223333:role/mock-cyber" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::111122223333:policy/mock-cyber" }
  }
}
mock_provider "kubernetes" {}

variables {
  environment = "dev"
  aws_region  = "us-east-1"
  account_id  = "111122223333"
  images = {
    cyber-browser = "111122223333.dkr.ecr.us-east-1.amazonaws.com/adp-cyber-browser@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
}

override_data {
  target = data.aws_caller_identity.current
  values = { account_id = "111122223333" }
}
override_data {
  target = data.aws_eks_cluster.platform
  values = {
    endpoint              = "https://eks.example.com"
    certificate_authority = [{ data = "dGVzdA==" }]
  }
}
override_data {
  target = data.aws_eks_cluster_auth.platform
  values = { token = "test-token" }
}
override_data {
  target = data.terraform_remote_state.platform
  values = {
    outputs = {
      eks_oidc_provider_arn = "arn:aws:iam::111122223333:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
      eks_oidc_issuer       = "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
    }
  }
}
override_data {
  target = data.terraform_remote_state.webhook
  values = {
    outputs = {
      agent_scaledjob_namespace = "adp-agents"
      agent_scaledjob_role_name = "adp-dev-agent-scaledjob-role"
    }
  }
}

run "cyber_root_owns_hosted_resources" {
  command = plan
  assert {
    condition = (
      module.cyber.worker_environment.URL_ANALYSIS_BROWSER_MODE == "broker" &&
      module.cyber.worker_environment.URL_ANALYSIS_BROWSER_BROKER == "http://url-analysis-browser-broker.adp-agents.svc.cluster.local:8765" &&
      length(module.cyber.worker_artifact_resources) == 1
    )
    error_message = "Cyber's own root must publish the hosted worker configuration."
  }
}

run "broker_refuses_missing_selected_account_image" {
  command = plan
  variables { images = {} }
  expect_failures = [terraform_data.browser_image_guard]
}

run "hosted_task_routes_come_from_cyber_state" {
  command = plan
  variables {
    settings = {
      browser_mode           = "native"
      browser_broker_enabled = "false"
      tools_endpoint         = "https://example.execute-api.us-east-1.amazonaws.com/dev/tools/cyber"
    }
    tool_invoke_resources = ["arn:aws:execute-api:us-east-1:111122223333:example/dev/POST/tools/cyber"]
  }
  assert {
    condition = (
      output.worker_environment.ADP_OPTIONAL_TASK_AGENTS == "cyber" &&
      length(output.worker_task_persona_tools["agent-task-cyber"]) == 3 &&
      length(output.worker_tool_invoke_resources) == 1
    )
    error_message = "Cyber must publish its opt-in Task registration and exact route from its own state."
  }
}
