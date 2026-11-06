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

run "no_unbound_registration_access" {
  command = plan
  assert {
    condition     = length(aws_iam_role_policy.registration_readback) == 0
    error_message = "Identity setup must not imply registration readback permissions."
  }
}

run "exact_readback_without_secret_values_or_mutable_evidence" {
  command = plan
  variables {
    registration_readback = {
      repository_id = 12345
      secret_policy_arns = [
        "arn:aws:secretsmanager:us-west-2:123456789012:secret:adp/test/gitlab-admin-token-abc123",
        "arn:aws:secretsmanager:us-west-2:123456789012:secret:adp/test/gitlab-runner/12-abc123",
        "arn:aws:secretsmanager:us-west-2:123456789012:secret:adp/test/gitlab-runner/60-abc123",
      ]
    }
  }
  assert {
    condition = (jsondecode(aws_iam_role_policy.registration_readback[0].policy).Statement[0].Action == ["secretsmanager:GetResourcePolicy"] &&
      !strcontains(aws_iam_role_policy.registration_readback[0].policy, "GetSecretValue") &&
    !strcontains(aws_iam_role_policy.registration_readback[0].policy, "DeleteObject"))
    error_message = "Readback must not grant credential reads or evidence deletion."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.registration_readback[0].policy).Statement[3].Condition == {
      StringEquals = { "s3:x-amz-server-side-encryption" = "AES256", "s3:if-none-match" = "*" }
    }
    error_message = "Registration writes require encryption and literal create-only preconditions."
  }
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.registration_readback[0].policy).Statement[2].Resource) == 6
    error_message = "Readback may name only the six accepted check objects."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.registration_readback[0].policy).Statement[4].Resource == [
      "arn:aws:s3:::adp-test-deployment-evidence-123456789012/deployment-evidence/v1/12345/*/*/delivery/delivery-evidence.json"
    ]
    error_message = "Publisher must bind the exact repository and named signed delivery object."
  }
}

run "reject_foreign_secret" {
  command = plan
  variables {
    registration_readback = {
      repository_id = 12345
      secret_policy_arns = [
        "arn:aws:secretsmanager:us-west-2:123456789012:secret:adp/test/gitlab-admin-token-abc123",
        "arn:aws:secretsmanager:us-west-2:123456789012:secret:adp/test/gitlab-runner/12-abc123",
        "arn:aws:secretsmanager:us-west-2:999999999999:secret:adp/test/gitlab-runner/60-abc123",
      ]
    }
  }
  expect_failures = [var.registration_readback]
}
