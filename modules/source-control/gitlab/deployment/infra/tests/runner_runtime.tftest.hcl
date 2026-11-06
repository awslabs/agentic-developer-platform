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
run "no_implicit_runner_authority" {
  command = plan
  assert {
    condition     = length(aws_iam_policy.runner_runtime_refresh) == 0 && length(aws_iam_policy.runner_runtime_evidence) == 0
    error_message = "Identity setup must add no runner runtime permissions."
  }
}
run "bound_runner_runtime" {
  command = plan
  variables {
    runner_runtime = {
      repository_id    = 123
      group_id         = 42
      cluster_name     = "example-cluster"
      access_entry_arn = "arn:aws:eks:us-west-2:123456789012:access-entry/example-cluster/role/123456789012/adp-test-gitlab-trusted-deployment/entry-id"
      input_versions = {
        "arn:aws:s3:::adp-test-deployment-evidence-123456789012/gitlab-runners/inputs/${sha256("plan")}/tfplan"            = "plan-version"
        "arn:aws:s3:::adp-test-deployment-evidence-123456789012/gitlab-runners/inputs/${sha256("lock")}/release-lock.json" = "lock-version"
      }
    }
  }
  assert {
    condition = alltrue([for forbidden in ["iam:PassRole", "iam:Put", "iam:Create", "ec2:RunInstances", "ec2:StopInstances", "ec2:StartInstances", "secretsmanager:GetSecretValue", "eks:CreateAccessEntry", "autoscaling:SetDesiredCapacity"] :
      !strcontains(aws_iam_policy.runner_runtime_refresh[0].policy, forbidden) && !strcontains(aws_iam_policy.runner_runtime_evidence[0].policy, forbidden)
    ])
    error_message = "Runtime verification grants no IAM, instance, capacity, secret-value or EKS access mutation."
  }
  assert {
    condition     = jsondecode(aws_iam_policy.runner_runtime_refresh[0].policy).Statement[8].Resource == "arn:aws:s3:::adp-terraform-state-123456789012/test/modules/gitlab-runners/terraform.tfstate.tflock" && jsondecode(aws_iam_policy.runner_runtime_refresh[0].policy).Statement[8].Action == "s3:DeleteObject"
    error_message = "Only the runner lock may be deleted, never its state or another module's objects."
  }
  assert {
    condition = jsondecode(aws_iam_policy.runner_runtime_refresh[0].policy).Statement[1].Resource == [
      "arn:aws:iam::123456789012:role/adp-test-gitlab-runner-42", "arn:aws:iam::123456789012:role/adp-test-gitlab-runners-reconciler"
    ]
    error_message = "Role metadata is limited to this product manager and module reconciler."
  }
  assert {
    condition = alltrue([for statement in jsondecode(aws_iam_policy.runner_runtime_evidence[0].policy).Statement :
      statement.Action != "s3:PutObject" || try(statement.Condition.StringEquals["s3:if-none-match"] == "*", false)
    ]) && length(jsondecode(aws_iam_policy.runner_runtime_evidence[0].policy).Statement[0].Resource) == 7
    error_message = "Write-once publication must retain all seven exact live check paths."
  }
  assert {
    condition     = length(aws_iam_policy.runner_runtime_refresh[0].policy) <= 6144 && length(aws_iam_policy.runner_runtime_evidence[0].policy) <= 6144
    error_message = "Each managed policy must fit the real IAM document limit."
  }
}
run "refuse_unversioned_runner_inputs" {
  command = plan
  variables {
    runner_runtime = {
      repository_id    = 123
      group_id         = 42
      cluster_name     = "example-cluster"
      access_entry_arn = "arn:aws:eks:us-west-2:123456789012:access-entry/example-cluster/role/123456789012/adp-test-gitlab-trusted-deployment/entry-id"
      input_versions = {
        "arn:aws:s3:::adp-test-deployment-evidence-123456789012/gitlab-runners/inputs/${sha256("plan")}/tfplan"            = "null"
        "arn:aws:s3:::adp-test-deployment-evidence-123456789012/gitlab-runners/inputs/${sha256("lock")}/release-lock.json" = "lock-version"
      }
    }
  }
  expect_failures = [var.runner_runtime]
}
