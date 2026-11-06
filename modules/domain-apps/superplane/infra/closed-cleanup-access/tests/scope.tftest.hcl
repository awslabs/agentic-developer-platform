mock_provider "aws" {
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::000000000000:policy/sp-closed-cleanup-synthetic" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "000000000000", user_id = "AROAOOOOOOOOOOOOOOOOO:synthetic" }
  }
  mock_data "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::000000000000:role/registrar", name = "registrar", unique_id = "AROARRRRRRRRRRRRRRRRR" }
  }
}
variables {
  permit = {
    kind                = "closed-bootstrap-cleanup-v1", status = "active", permit_id = "sp-cleanup:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    created_at          = "2099-01-01T00:00:00Z", expires_at = "2099-01-01T01:00:00Z"
    owner_role_id       = "AROAOOOOOOOOOOOOOOOOO"
    verifier_policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSAdminViewPolicy"
    registrar           = { role_arn = "arn:aws:iam::000000000000:role/registrar", role_id = "AROARRRRRRRRRRRRRRRRR" }
    operation           = { state = "unknown", budget_state = "retained" }
    authority           = { role_arn = "arn:aws:iam::000000000000:role/provider", account_id = "000000000000", region = "us-east-1", org_id = "org", workspace_id = "workspace" }
    qualification = {
      cluster_arn   = "arn:aws:eks:us-east-1:000000000000:cluster/synthetic", org_id = "org", workspace_id = "workspace", generation = "original-generation"
      retained_plan = { grants = [{ key = "registrar-entry", principal_arn = "arn:aws:iam::000000000000:role/registrar" }] }
    }
  }
}
run "only_original_registrar_and_group_free_reader" {
  command = apply
  assert {
    condition = (
      length(local.policy.Statement) == 6 &&
      local.policy.Statement[0].Resource == var.permit.qualification.cluster_arn &&
      local.policy.Statement[0].Condition.StringEquals["eks:principalArn"] == var.permit.authority.role_arn &&
      local.policy.Statement[0].Condition.Null["eks:kubernetesGroups"] == "true" &&
      local.policy.Statement[1].Condition.StringEquals["eks:policyArn"] == local.view &&
      local.policy.Statement[1].Condition.StringEquals["eks:accessScope"] == "cluster" &&
      alltrue([for s in local.policy.Statement : s.Condition.DateLessThan["aws:CurrentTime"] == var.permit.expires_at]) &&
      aws_iam_role_policy_attachment.verifier.role == "registrar"
    )
    error_message = "Verifier creation, view policy, expiry or attachment scope widened."
  }
}
run "reject_wrong_registrar_identity" {
  # Terraform 1.9 defers the combined timestamp guard until apply. The mocked
  # apply must evaluate that guard and refuse the mismatched immutable RoleId.
  command = apply
  override_data {
    target = data.aws_iam_role.registrar
    values = { arn = "arn:aws:iam::000000000000:role/registrar", name = "registrar", unique_id = "AROAXXXXXXXXXXXXXXXXX" }
  }
  expect_failures = [terraform_data.guard]
}
