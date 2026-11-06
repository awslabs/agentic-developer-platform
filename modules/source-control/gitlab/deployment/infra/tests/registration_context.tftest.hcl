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

run "no_implicit_context_or_instance_grant" {
  command = plan
  assert {
    condition     = length(aws_iam_role_policy.registration_context) == 0 && length(aws_iam_role_policy.reviewed_instance_update) == 0
    error_message = "Neither instance changes nor context writes follow from identity setup."
  }
}

run "context_is_one_immutable_workflow_object" {
  command = plan
  variables { registration_context_repository_id = 1234 }
  assert {
    condition = jsondecode(aws_iam_role_policy.registration_context[0].policy).Statement[0] == {
      Sid       = "PublishImmutableRegistrationContext", Effect = "Allow", Action = "s3:PutObject"
      Resource  = "arn:aws:s3:::adp-test-deployment-evidence-123456789012/deployment-evidence/v1/1234/*/*/context/gitlab-infra-apply.yml.json"
      Condition = { StringEquals = { "s3:if-none-match" = "*" } }
    }
    error_message = "Context publication must bind the repository, workflow and create-only object condition."
  }
  assert {
    condition     = length(aws_iam_role_policy.reviewed_instance_update) == 0
    error_message = "Context publication must not open the instance gate."
  }
}

run "instance_grant_only_changes_userdata_on_the_reviewed_instance" {
  command = plan
  variables {
    reviewed_instance_update = {
      instance_id = "i-0123456789abcdef0", source_revision = sha1("source")
      plan_sha256 = sha256("plan"), approval_ref = "reviewed-owner-decision"
      expires_at  = "2026-11-04T09:00:00Z"
    }
  }
  assert {
    condition = alltrue([for statement in jsondecode(aws_iam_role_policy.reviewed_instance_update[0].policy).Statement :
      statement.Resource == "arn:aws:ec2:us-west-2:123456789012:instance/i-0123456789abcdef0"
      && statement.Condition.DateLessThan["aws:CurrentTime"] == "2026-11-04T09:00:00Z"
    ]) && jsondecode(aws_iam_role_policy.reviewed_instance_update[0].policy).Statement[1].Condition.StringEquals == { "ec2:Attribute" = "UserData" }
    error_message = "Bind the exact instance and permit only its userData attribute mutation."
  }
  assert {
    condition = (!strcontains(aws_iam_role_policy.reviewed_instance_update[0].policy, "TerminateInstances") &&
      !strcontains(aws_iam_role_policy.reviewed_instance_update[0].policy, "RunInstances") &&
    !strcontains(aws_iam_role_policy.reviewed_instance_update[0].policy, "PassRole"))
    error_message = "Metadata maintenance must not grant replacement, deletion or PassRole."
  }
}

run "reject_missing_instance_approval_reference" {
  command = plan
  variables {
    reviewed_instance_update = {
      instance_id = "i-0123456789abcdef0", source_revision = sha1("source")
      plan_sha256 = sha256("plan"), approval_ref = ""
      expires_at  = "2026-11-04T09:00:00Z"
    }
  }
  expect_failures = [var.reviewed_instance_update]
}

run "reject_invalid_instance_expiry" {
  command = plan
  variables {
    reviewed_instance_update = {
      instance_id = "i-0123456789abcdef0", source_revision = sha1("source")
      plan_sha256 = sha256("plan"), approval_ref = "reviewed-owner-decision"
      expires_at  = "unbounded"
    }
  }
  expect_failures = [var.reviewed_instance_update]
}

run "diagnostics_are_private_immutable_and_repository_bound" {
  command = plan
  variables { registration_diagnostics_repository_id = 1234 }
  assert {
    condition = jsondecode(aws_iam_role_policy.registration_diagnostics[0].policy).Statement == [{
      Effect    = "Allow", Action = "s3:PutObject"
      Resource  = "arn:aws:s3:::adp-test-deployment-evidence-123456789012/gitlab-registration-diagnostics/repos/1234/runs/*/attempts/*/terraform-failure.json"
      Condition = { StringEquals = { "s3:x-amz-server-side-encryption" = "AES256", "s3:if-none-match" = "*" } }
    }]
    error_message = "Only immutable encrypted private provider-failure objects for the exact repository may be written."
  }
}
