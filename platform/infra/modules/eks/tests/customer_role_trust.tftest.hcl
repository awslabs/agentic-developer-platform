# Issue #2944: the gateway IRSA role must be able to read the deploy-time SSM
# params that register_app_start reads (/adp/<env>/webhook-ingress/endpoint and
# /adp/<env>/gateway/apigw-invoke-url). Without ssm:GetParameter the reads get
# AccessDenied and the #2674 guard returns 422 "Webhook endpoint not configured".
#
# Plan-only test with mocked providers — asserts the policy is attached to the
# gateway IRSA role, grants the two read actions, and is scoped to this
# account's /adp/<env>/* prefix (not ssm:* / not a wildcard resource).

mock_provider "aws" {}
mock_provider "kubernetes" {}
mock_provider "tls" {}

# Keep the existing policy test valid with Pod Identity associations enabled.
override_resource {
  target = aws_iam_role.gateway_service_irsa
  values = {
    arn = "arn:aws:iam::123456789012:role/adp-dev-gateway-service-irsa"
  }
}

variables {
  environment             = "dev"
  name_prefix             = "adp-dev"
  vpc_id                  = "vpc-00000000000000000"
  private_subnet_ids      = ["subnet-00000000000000001", "subnet-00000000000000002"]
  eks_security_group_id   = "sg-00000000000000000"
  eks_cluster_role_arn    = "arn:aws:iam::123456789012:role/adp-dev-role-eks-cluster"
  node_group_role_arn     = "arn:aws:iam::123456789012:role/adp-dev-role-eks-node-group"
  eks_public_access_cidrs = ["10.0.0.0/8"]
}

run "customer_assume_disabled_by_default" {
  command = plan
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.gateway_sts.policy).Statement) == 1
    error_message = "Empty approvals must not grant customer role assumptions."
  }
}

run "customer_assume_uses_exact_approved_arns" {
  command = plan
  variables {
    gateway_customer_role_arns = ["arn:aws:iam::210987654321:role/ADP-Agent-example"]
  }
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012" }
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.gateway_sts.policy).Statement[1].Resource == ["arn:aws:iam::210987654321:role/ADP-Agent-example"]
    error_message = "AssumeRole must use only the exact approved customer ARN."
  }
}

run "wildcard_approvals_are_refused" {
  command = plan
  variables {
    gateway_customer_role_arns = ["arn:aws:iam::*:role/ADP-Agent-*"]
  }
  expect_failures = [var.gateway_customer_role_arns]
}

run "platform_account_approval_is_refused" {
  command = plan
  variables {
    gateway_customer_role_arns = ["arn:aws:iam::123456789012:role/ADP-Agent-platform"]
  }
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012" }
  }
  expect_failures = [aws_iam_role_policy.gateway_sts]
}
