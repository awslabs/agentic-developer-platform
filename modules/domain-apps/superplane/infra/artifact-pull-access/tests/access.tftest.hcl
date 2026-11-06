mock_provider "aws" {}
mock_provider "aws" {
  alias = "consumer"
}

variables {
  source_registry = {
    account_id        = "123456789012"
    region            = "us-east-1"
    operator_role_arn = "arn:aws:iam::123456789012:role/source-owner"
    operator_role_id  = "AROA11111111111111111"
  }
  consumer = {
    account_id        = "000000000002"
    operator_role_arn = "arn:aws:iam::000000000002:role/consumer-owner"
    operator_role_id  = "AROA22222222222222222"
    node_role_arn     = "arn:aws:iam::000000000002:role/consumer-node"
    node_role_id      = "AROA33333333333333333"
  }
}

override_data {
  target = data.aws_caller_identity.source
  values = { account_id = "123456789012", user_id = "AROA11111111111111111:session" }
}
override_data {
  target = data.aws_iam_role.source_operator
  values = { arn = "arn:aws:iam::123456789012:role/source-owner", unique_id = "AROA11111111111111111" }
}
override_data {
  target = data.aws_ecr_repository.skypilot
  values = {
    name                 = "adp-superplane-skypilot"
    arn                  = "arn:aws:ecr:us-east-1:123456789012:repository/adp-superplane-skypilot"
    registry_id          = "123456789012"
    image_tag_mutability = "IMMUTABLE"
  }
}
override_data {
  target = data.aws_caller_identity.consumer
  values = { account_id = "000000000002", user_id = "AROA22222222222222222:session" }
}
override_data {
  target = data.aws_iam_role.consumer_operator
  values = { arn = "arn:aws:iam::000000000002:role/consumer-owner", unique_id = "AROA22222222222222222" }
}
override_data {
  target = data.aws_iam_role.node
  values = { arn = "arn:aws:iam::000000000002:role/consumer-node", unique_id = "AROA33333333333333333" }
}

run "only_selected_node_can_pull_one_repository" {
  command = plan
  assert {
    condition = (
      length(jsondecode(aws_ecr_repository_policy.skypilot_pull.policy).Statement) == 1 &&
      jsondecode(aws_ecr_repository_policy.skypilot_pull.policy).Statement[0].Effect == "Allow" &&
      jsondecode(aws_ecr_repository_policy.skypilot_pull.policy).Statement[0].Principal == { AWS = var.consumer.node_role_arn } &&
      !contains(keys(jsondecode(aws_ecr_repository_policy.skypilot_pull.policy).Statement[0]), "Resource") &&
      aws_ecr_repository_policy.skypilot_pull.repository == "adp-superplane-skypilot" &&
      output.pull_access.repository_arn == "arn:aws:ecr:us-east-1:123456789012:repository/adp-superplane-skypilot" &&
      toset(jsondecode(aws_ecr_repository_policy.skypilot_pull.policy).Statement[0].Action) == toset([
        "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"
      ])
    )
    error_message = "Artifact access must grant only the selected node the three pull actions on one repository."
  }
}

run "recreated_node_role_refused" {
  command = plan
  override_data {
    target = data.aws_iam_role.node
    values = { arn = "arn:aws:iam::000000000002:role/consumer-node", unique_id = "AROA44444444444444444" }
  }
  expect_failures = [aws_ecr_repository_policy.skypilot_pull]
}

run "different_source_session_refused" {
  command = plan
  override_data {
    target = data.aws_caller_identity.source
    values = { account_id = "123456789012", user_id = "AROA44444444444444444:session" }
  }
  expect_failures = [aws_ecr_repository_policy.skypilot_pull]
}

run "different_consumer_session_refused" {
  command = plan
  override_data {
    target = data.aws_caller_identity.consumer
    values = { account_id = "000000000002", user_id = "AROA44444444444444444:session" }
  }
  expect_failures = [aws_ecr_repository_policy.skypilot_pull]
}

run "different_repository_refused" {
  command = plan
  override_data {
    target = data.aws_ecr_repository.skypilot
    values = {
      name                 = "adp-superplane-skypilot"
      arn                  = "arn:aws:ecr:us-east-1:123456789012:repository/another-repository"
      registry_id          = "123456789012"
      image_tag_mutability = "IMMUTABLE"
    }
  }
  expect_failures = [aws_ecr_repository_policy.skypilot_pull]
}

run "wildcard_principal_refused" {
  command = plan
  variables {
    consumer = {
      account_id        = "000000000002"
      operator_role_arn = "arn:aws:iam::000000000002:role/consumer-owner"
      operator_role_id  = "AROA22222222222222222"
      node_role_arn     = "arn:aws:iam::000000000002:role/*"
      node_role_id      = "AROA33333333333333333"
    }
  }
  expect_failures = [var.consumer]
}
