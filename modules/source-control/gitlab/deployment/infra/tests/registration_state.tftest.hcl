mock_provider "aws" {
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
}

variables {
  account_id  = "123456789012"
  aws_region  = "us-west-2"
  environment = "test"
  repository  = "example/platform"
}

run "no_implicit_backend_or_input_access" {
  command = plan
  assert {
    condition     = length(aws_iam_role_policy.registration_state) == 0 && length(aws_iam_role_policy.registration_inputs) == 0
    error_message = "Identity setup must not imply state or artifact access."
  }
}

run "only_original_state_and_lock_rows" {
  command = plan
  variables { registration_state_access = true }
  assert {
    condition = jsondecode(aws_iam_role_policy.registration_state[0].policy).Statement[1].Resource == [
      "arn:aws:s3:::adp-terraform-state-123456789012/test/modules/gitlab/terraform.tfstate",
      "arn:aws:s3:::adp-terraform-state-123456789012/test/platform/terraform.tfstate",
    ]
    error_message = "Read only the GitLab state and its platform remote-state dependency."
  }
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.registration_state[0].policy).Statement[2].Resource == "arn:aws:s3:::adp-terraform-state-123456789012/test/modules/gitlab/terraform.tfstate" &&
      !strcontains(aws_iam_role_policy.registration_state[0].policy, "s3:DeleteObject") &&
      !strcontains(aws_iam_role_policy.registration_state[0].policy, "gitlab-deployment/")
    )
    error_message = "Never permit deletion, platform-state writes or access to the policy-ownership backend."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.registration_state[0].policy).Statement[4].Condition == {
      "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = [
        "adp-terraform-state-123456789012/test/modules/gitlab/terraform.tfstate",
        "adp-terraform-state-123456789012/test/modules/gitlab/terraform.tfstate-md5",
      ] }
      Null = { "dynamodb:LeadingKeys" = "false" }
    }
    error_message = "Lock-table access must require only the two GitLab lock/checksum rows."
  }
}

run "version_bound_inputs" {
  command = plan
  variables {
    registration_input_versions = {
      "arn:aws:s3:::adp-test-deployment-evidence-123456789012/gitlab-registration/inputs/${sha256("plan")}/tfplan"                = "plan-version"
      "arn:aws:s3:::adp-test-deployment-evidence-123456789012/gitlab-registration/inputs/${sha256("vars")}/variables.tfvars.json" = "vars-version"
      "arn:aws:s3:::adp-test-deployment-evidence-123456789012/gitlab-registration/inputs/${sha256("root")}/approved-root.pem"     = "root-version"
    }
  }
  assert {
    condition = alltrue([for statement in jsondecode(aws_iam_role_policy.registration_inputs[0].policy).Statement :
      statement.Action == "s3:GetObjectVersion" && statement.Condition.StringEquals["s3:VersionId"] == var.registration_input_versions[statement.Resource]
    ])
    error_message = "Each input must bind its own exact object version, with no unversioned or write access."
  }
}

run "reject_unversioned_input" {
  command = plan
  variables {
    registration_input_versions = {
      "arn:aws:s3:::adp-test-deployment-evidence-123456789012/gitlab-registration/inputs/${sha256("plan")}/tfplan"                = "null"
      "arn:aws:s3:::adp-test-deployment-evidence-123456789012/gitlab-registration/inputs/${sha256("vars")}/variables.tfvars.json" = "vars-version"
      "arn:aws:s3:::adp-test-deployment-evidence-123456789012/gitlab-registration/inputs/${sha256("root")}/approved-root.pem"     = "root-version"
    }
  }
  expect_failures = [var.registration_input_versions]
}

run "reject_three_plans_without_root_or_vars" {
  command = plan
  variables {
    registration_input_versions = {
      "arn:aws:s3:::adp-test-deployment-evidence-123456789012/gitlab-registration/inputs/${sha256("one")}/tfplan"   = "one"
      "arn:aws:s3:::adp-test-deployment-evidence-123456789012/gitlab-registration/inputs/${sha256("two")}/tfplan"   = "two"
      "arn:aws:s3:::adp-test-deployment-evidence-123456789012/gitlab-registration/inputs/${sha256("three")}/tfplan" = "three"
    }
  }
  expect_failures = [var.registration_input_versions]
}
