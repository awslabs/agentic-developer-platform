mock_provider "aws" {}

variables {
  account_id             = "111122223333"
  region                 = "us-west-2"
  environment            = "fixture"
  cluster_name           = "selected-cluster"
  namespace              = "superplane"
  oidc_issuer            = "https://oidc.eks.us-west-2.amazonaws.com/id/fixture"
  installation_id        = "aaaaaaaaaaaaaaaaaaaaaaaa"
  api_id                 = "abcdefghij"
  api_stage              = "prod"
  keda_operator_role_arn = "arn:aws:iam::111122223333:role/existing-keda-operator"
  operator_role_arn      = "arn:aws:iam::111122223333:role/installation-operator"
}

override_data {
  target = data.aws_caller_identity.current
  values = { account_id = "111122223333", arn = "arn:aws:sts::111122223333:assumed-role/installation-operator/session" }
}

override_data {
  target = data.aws_eks_cluster.selected
  values = {
    arn      = "arn:aws:eks:us-west-2:111122223333:cluster/selected-cluster"
    identity = [{ oidc = [{ issuer = "https://oidc.eks.us-west-2.amazonaws.com/id/fixture" }] }]
  }
}

override_data {
  target = data.aws_iam_openid_connect_provider.selected
  values = {
    arn = "arn:aws:iam::111122223333:oidc-provider/oidc.eks.us-west-2.amazonaws.com/id/fixture"
    url = "oidc.eks.us-west-2.amazonaws.com/id/fixture"
  }
}

override_data {
  target = data.aws_iam_role.keda_operator
  values = { arn = "arn:aws:iam::111122223333:role/existing-keda-operator" }
}

run "scope_and_identity" {
  command = plan
  assert {
    condition     = aws_sqs_queue.operations.name == "adp-fixture-superplane-domain-operations" && aws_sqs_queue.operations.fifo_queue == false && aws_sqs_queue.operations.sqs_managed_sse_enabled == true
    error_message = "Only a dedicated encrypted standard queue is allowed."
  }
  assert {
    condition     = aws_iam_role.worker.name == "adp-fixture-superplane-domain-worker" && aws_iam_role.observer.name == "adp-fixture-superplane-domain-observer"
    error_message = "Role names must match the closed installation plan."
  }
  assert {
    condition     = jsondecode(aws_iam_role.worker.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.us-west-2.amazonaws.com/id/fixture:sub"] == "system:serviceaccount:superplane:superplane-paid-worker"
    error_message = "Worker trust must be pinned to the paid worker ServiceAccount."
  }
  assert {
    condition     = jsondecode(aws_iam_role.observer.assume_role_policy).Statement[0].Principal.AWS == "arn:aws:iam::111122223333:role/existing-keda-operator"
    error_message = "Observer trust must be pinned to the existing KEDA role."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.observer.policy).Statement[0].Action == "sqs:GetQueueAttributes" && jsondecode(aws_iam_role_policy.observer.policy).Statement[0].Resource == local.queue_arn
    error_message = "Observer may only read attributes of this queue."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.worker.policy).Statement[0].Action == "execute-api:Invoke" && toset(jsondecode(aws_iam_role_policy.worker.policy).Statement[0].Resource) == toset([for route in local.worker_routes : "${local.gateway_prefix}${route}"])
    error_message = "Worker IAM must invoke only reviewed routes on the selected Gateway stage."
  }
}

run "refuse_different_account" {
  command = plan
  variables { account_id = "444455556666" }
  expect_failures = [aws_sqs_queue.operations, aws_iam_role.worker, aws_iam_role.observer]
}

run "refuse_different_cluster_issuer" {
  command = plan
  variables { oidc_issuer = "https://oidc.eks.us-west-2.amazonaws.com/id/different" }
  expect_failures = [aws_sqs_queue.operations, aws_iam_role.worker, aws_iam_role.observer]
}

run "refuse_wrong_operator" {
  command = plan
  variables { operator_role_arn = "arn:aws:iam::111122223333:role/different-operator" }
  expect_failures = [aws_sqs_queue.operations, aws_iam_role.worker, aws_iam_role.observer]
}
