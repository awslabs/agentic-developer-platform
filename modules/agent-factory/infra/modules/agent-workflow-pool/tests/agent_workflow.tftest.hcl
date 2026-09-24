mock_provider "kubernetes" {}
mock_provider "helm" {}
variables {
  github_org      = "example"
  github_repo     = "repo"
  runner_role_arn = "arn:aws:iam::123456789012:role/shared-runner"
}
run "existing_installation_is_unchanged_until_enabled" {
  command = apply
  assert {
    condition     = length(helm_release.agent_workflow) == 0 && length(kubernetes_service_account.agent_workflow) == 0
    error_message = "An existing install must opt into the additive pool before routing changes."
  }
}
run "separate_pool_cannot_inherit_shared_role" {
  command = apply
  variables {
    enable_agent_workflow_runner = true
    agent_workflow_role_arn      = "arn:aws:iam::123456789012:role/adp-test-agent-workflow"
  }
  assert {
    condition     = jsondecode(jsonencode(yamldecode(helm_release.agent_workflow[0].values[0]))).template.spec.serviceAccountName == "agent-workflow-sa"
    error_message = "The dedicated pool must execute with its distinct service account."
  }
  assert {
    condition     = kubernetes_service_account.agent_workflow[0].metadata[0].annotations["eks.amazonaws.com/role-arn"] == var.agent_workflow_role_arn && kubernetes_service_account.agent_workflow[0].metadata[0].annotations["eks.amazonaws.com/role-arn"] != var.runner_role_arn
    error_message = "The agent service account must not inherit shared runner credentials."
  }
  assert {
    condition     = helm_release.agent_workflow[0].name == "arc-runner-agent" && yamldecode(helm_release.agent_workflow[0].values[0]).runnerScaleSetName == "arc-runner-agent"
    error_message = "The pool must register a distinct routing label."
  }
  assert {
    condition     = yamldecode(helm_release.agent_workflow[0].values[0]).githubConfigUrl == "https://github.com/example/repo" && yamldecode(helm_release.agent_workflow[0].values[0]).minRunners == 0
    error_message = "Preserve repository registration and zero idle capacity."
  }
}
run "shared_role_configuration_refused" {
  command = plan
  variables {
    enable_agent_workflow_runner = true
    agent_workflow_role_arn      = "arn:aws:iam::123456789012:role/shared-runner"
  }
  expect_failures = [helm_release.agent_workflow[0]]
}
