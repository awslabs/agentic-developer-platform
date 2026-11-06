mock_provider "aws" {
  mock_data "aws_availability_zones" { defaults = { names = ["us-east-1a", "us-east-1b"] } }
  mock_data "aws_iam_policy_document" { defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" } }
}
mock_provider "kubernetes" {}
mock_provider "tls" {}
mock_provider "time" {}

variables {
  environment                        = "dev"
  shared_tools_browser_namespace     = "fictional-agents"
  shared_tools_browser_resource_name = "fictional-shared-tools"
}

# test_tools_release_plan_scope.py checks the actual saved plan emitted by this
# run. Keep the full real root/module configuration, with mocked providers, so a
# broad module dependency cannot pass by replacing the module with a stub.
run "fresh_tools_rbac_keeps_access_readiness_and_excludes_unrelated_workloads" {
  command = plan
  plan_options {
    target = [
      kubernetes_role_v1.tools_release,
      kubernetes_role_binding_v1.tools_release,
      kubernetes_role_v1.shared_tools_release,
      kubernetes_role_binding_v1.shared_tools_release,
      kubernetes_role_v1.tools_browser_observer,
      kubernetes_role_binding_v1.tools_browser_observer,
    ]
  }
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012", arn = "arn:aws:iam::123456789012:user/test-deployer" }
  }
  assert {
    condition = (
      length(null_resource.tools_release_namespace) == 1 &&
      kubernetes_role_binding_v1.tools_release[0].subject[0].name == "adp:tools-release-dev" &&
      kubernetes_role_binding_v1.tools_browser_observer[0].subject[0].name == "adp:tools-release-dev" &&
      kubernetes_role_binding_v1.shared_tools_release[0].subject[0].name == "adp:shared-tools-release-dev"
    )
    error_message = "All three role bindings and the fresh-install namespace prerequisite must remain in scope."
  }
}
