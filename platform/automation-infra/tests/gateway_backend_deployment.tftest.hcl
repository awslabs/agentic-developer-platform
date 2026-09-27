mock_provider "external" {
  mock_data "external" { defaults = { result = { verified = "true" } } }
}
mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_data "aws_ssm_parameter" { defaults = { value = "mock" } }
  mock_data "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/gateway/mock-AbCd12" }
  }
  mock_data "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012" }
  }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/test" } }
  mock_resource "aws_iam_policy" { defaults = { arn = "arn:aws:iam::123456789012:policy/test" } }
}

variables {
  name_prefix                       = "adp-test"
  environment                       = "test"
  aws_region                        = "us-east-1"
  cluster_name                      = "adp-test-eks-cluster"
  repository                        = "aws-e/adp"
  build_project_names               = ["adp-test-gateway-build"]
  enable_gateway_backend_deployment = true
}

run "gateway_backend_profile_is_bounded" {
  command = plan
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "adp-test-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E1234567890" }
  }
  override_data {
    target = data.aws_ssm_parameter.gateway_backend_api_url[0]
    values = { value = "https://abc123def4.execute-api.us-east-1.amazonaws.com/test" }
  }
  override_data {
    target = data.aws_kms_key.gateway_backend_lambda[0]
    values = { arn = "arn:aws:kms:us-east-1:123456789012:key/87654321-4321-4321-4321-210987654321" }
  }
  assert {
    condition = jsondecode(aws_iam_role.gateway_backend[0].assume_role_policy).Statement[0].Condition.StringEquals == {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub" = "repo:aws-e/adp:environment:adp-gateway-deploy-test"
    }
    error_message = "Gateway deployment trust must name only its own environment."
  }
  assert {
    condition     = aws_eks_access_policy_association.gateway_backend[0].access_scope[0].type == "namespace" && toset(aws_eks_access_policy_association.gateway_backend[0].access_scope[0].namespaces) == toset(["adp-gateway"])
    error_message = "Gateway deployment must not get cluster-wide Kubernetes access."
  }
  assert {
    condition = (
      length(aws_iam_role_policy.gateway_backend[0].policy) <= 10240 &&
      length(aws_iam_policy.gateway_backend_ceiling[0].policy) <= 6144
    )
    error_message = "Gateway profile exceeds IAM policy size limits."
  }
  assert {
    condition = (
      !can(regex("iam:PassRole|iam:CreateRole|sts:AssumeRole|secretsmanager:PutSecretValue|secretsmanager:CreateSecret", aws_iam_role_policy.gateway_backend[0].policy)) &&
      !contains(jsondecode(aws_iam_policy.gateway_backend_ceiling[0].policy).Statement[1].NotAction, "iam:PassRole")
    )
    error_message = "Gateway profile must not mutate identities or secrets or chain roles."
  }
  assert {
    condition = (
      contains([for s in jsondecode(aws_iam_role_policy.gateway_backend[0].policy).Statement : s.Resource if s.Action == "codebuild:StartBuild" && s.Effect == "Allow"], "arn:aws:codebuild:us-east-1:123456789012:project/adp-test-gateway-build") &&
      !can(regex("codebuild:UpdateProject|lambda:CreateFunction", aws_iam_role_policy.gateway_backend[0].policy))
    )
    error_message = "Gateway profile must dispatch only the existing gateway build."
  }
  assert {
    condition = (
      contains([for s in jsondecode(aws_iam_role_policy.gateway_backend[0].policy).Statement : s.Resource if s.Action == "codebuild:BatchGetBuilds"], "arn:aws:codebuild:us-east-1:123456789012:project/adp-test-gateway-build") &&
      contains([for s in jsondecode(aws_iam_policy.gateway_backend_ceiling[0].policy).Statement : s.NotResource if try(s.Sid, "") == "DenyOtherBuildEvidence"], "arn:aws:codebuild:us-east-1:123456789012:project/adp-test-gateway-build")
    )
    error_message = "Polling a build ID requires access to the exact CodeBuild project ARN."
  }
  assert {
    condition = (
      contains(local.gateway_backend_lambdas, "arn:aws:lambda:us-east-1:123456789012:function:bedrockgw-test-pricing-refresh:$LATEST") &&
      contains(local.gateway_backend_lambdas, "arn:aws:lambda:us-east-1:123456789012:function:bedrockgw-test-budget-usage-tracker:$LATEST") &&
      !contains(local.gateway_backend_lambdas, "arn:aws:lambda:us-east-1:123456789012:function:bedrockgw-test-pricing-refresh:*")
    )
    error_message = "Gateway Lambda code updates require exact $LATEST resource ARNs."
  }
  assert {
    condition = (
      contains(jsondecode(aws_iam_policy.gateway_backend_ceiling[0].policy).Statement[1].NotAction, "kms:Decrypt") &&
      one([for s in jsondecode(aws_iam_policy.gateway_backend_ceiling[0].policy).Statement : s if try(s.Sid, "") == "AllowGatewaySecretDecrypt"]).Resource == "arn:aws:kms:us-east-1:123456789012:key/12345678-1234-1234-1234-123456789012" &&
      one([for s in jsondecode(aws_iam_policy.gateway_backend_ceiling[0].policy).Statement : s if try(s.Sid, "") == "AllowGatewaySecretDecrypt"]).Condition.StringEquals["kms:ViaService"] == "secretsmanager.us-east-1.amazonaws.com" &&
      toset(one([for s in jsondecode(aws_iam_policy.gateway_backend_ceiling[0].policy).Statement : s if try(s.Sid, "") == "AllowGatewaySecretDecrypt"]).Condition.StringEquals["kms:EncryptionContext:SecretARN"]) == toset(local.gateway_backend_secret_arns) &&
      contains([for s in jsondecode(aws_iam_role_policy.gateway_backend[0].policy).Statement : s.Action if s.Effect == "Allow"], "kms:Decrypt")
    )
    error_message = "Gateway secret decryption must use the exact Secrets Manager key and secret encryption context."
  }
  assert {
    condition = (
      contains(jsondecode(aws_iam_policy.gateway_backend_ceiling[0].policy).Statement[1].NotAction, "kms:DescribeKey") &&
      one([for s in jsondecode(aws_iam_policy.gateway_backend_ceiling[0].policy).Statement : s if try(s.Sid, "") == "AllowLambdaKeyDescription"]).Resource == "arn:aws:kms:us-east-1:123456789012:key/87654321-4321-4321-4321-210987654321" &&
      contains([for s in jsondecode(aws_iam_role_policy.gateway_backend[0].policy).Statement : s.Resource if s.Action == "kms:DescribeKey"], "arn:aws:kms:us-east-1:123456789012:key/87654321-4321-4321-4321-210987654321")
    )
    error_message = "Updating the orchestration image may describe only the AWS-managed Lambda key."
  }
}
