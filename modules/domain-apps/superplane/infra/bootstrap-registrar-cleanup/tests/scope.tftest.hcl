mock_provider "aws" {}
variables {
  account_id          = "123456789012"
  region              = "us-west-2"
  installer_role_name = "adp-dev-superplane-installer-0123456789abcdef"
  entry_arn           = "arn:aws:eks:us-west-2:123456789012:access-entry/workspace/role/123456789012/registrar/immutable"
  expires_at          = timeadd(timestamp(), "1h")
  operation_id        = "d97086a7-d263-552c-a304-7660104ca5d5"
}
run "exact_entry_only" {
  command = plan
  assert {
    condition     = jsondecode(aws_iam_role_policy.cleanup.policy).Statement[0].Resource == var.entry_arn
    error_message = "Cleanup must be restricted to the immutable entry ARN."
  }
  assert {
    condition     = toset(jsondecode(aws_iam_role_policy.cleanup.policy).Statement[0].Action) == toset(["eks:DeleteAccessEntry", "eks:DisassociateAccessPolicy"])
    error_message = "Cleanup must never gain create, associate or role trust actions."
  }
}
run "foreign_account_refused" {
  command = plan
  variables {
    account_id = "999999999999"
  }
  expect_failures = [aws_iam_role_policy.cleanup]
}
run "wildcard_refused" {
  command = plan
  variables {
    entry_arn = "arn:aws:eks:us-west-2:123456789012:access-entry/workspace/*"
  }
  expect_failures = [var.entry_arn]
}
run "standing_grant_refused" {
  command = plan
  variables {
    expires_at = timeadd(timestamp(), "3h")
  }
  expect_failures = [aws_iam_role_policy.cleanup]
}
