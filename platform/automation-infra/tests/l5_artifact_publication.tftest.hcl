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
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/test", id = "test" } }
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

run "artifact_publication_is_opt_in" {
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
  override_data {
    target = data.aws_kms_key.gateway_backend_parameters[0]
    values = { arn = "arn:aws:kms:us-east-1:123456789012:key/0a0a0a0a-0a0a-0a0a-0a0a-0a0a0a0a0a0a" }
  }

  assert {
    condition     = length(aws_iam_role_policy.l5_artifact_publication) == 0 && length(local.gateway_l5_release_objects) == 0
    error_message = "Existing deployments must gain no artifact authority without opting in."
  }
}

run "artifact_publication_has_exact_storage_scope" {
  # Mocked apply resolves the role ID and proves the attachment uses that role.
  command = apply
  variables { enable_l5_artifact_publication = true }
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
  override_data {
    target = data.aws_kms_key.gateway_backend_parameters[0]
    values = { arn = "arn:aws:kms:us-east-1:123456789012:key/0a0a0a0a-0a0a-0a0a-0a0a-0a0a0a0a0a0a" }
  }

  assert {
    condition = (
      toset(jsondecode(aws_iam_role_policy.l5_artifact_publication[0].policy).Statement[0].Action) == toset(["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"]) &&
      jsondecode(aws_iam_role_policy.l5_artifact_publication[0].policy).Statement[0].Resource == ["arn:aws:s3:::adp-test-deployment-evidence-123456789012/l5/security/*"] &&
      aws_iam_role_policy.l5_artifact_publication[0].role == aws_iam_role.gateway_backend[0].id &&
      output.l5_artifact_publication_bucket == "adp-test-deployment-evidence-123456789012"
    )
    error_message = "Publication must grant only exact-key reads and writes within the existing shared L5 prefix."
  }
  assert {
    condition = (
      toset(one([for s in jsondecode(aws_iam_policy.gateway_backend_ceiling[0].policy).Statement : s if try(s.Sid, "") == "DenyOtherObservations"]).NotResource) == toset(concat(local.gateway_observer_resources, ["arn:aws:s3:::adp-test-deployment-evidence-123456789012/l5/security/*"])) &&
      contains(one([for s in jsondecode(aws_iam_policy.gateway_backend_ceiling[0].policy).Statement : s if try(s.Sid, "") == "DenyOtherUploads"]).NotResource, "arn:aws:s3:::adp-test-deployment-evidence-123456789012/l5/security/*") &&
      one([for s in jsondecode(aws_iam_policy.gateway_backend_ceiling[0].policy).Statement : s if try(s.Sid, "") == "DenyOtherEvidencePrefixes"]).Condition.StringNotLike["s3:prefix"] == "deployment-evidence/v1/*" &&
      length(aws_iam_policy.gateway_backend_ceiling[0].policy) <= 6144
    )
    error_message = "The ceiling must admit L5 object access without granting bucket enumeration or exceeding IAM limits."
  }
  assert {
    condition = (
      !can(regex("DeleteObject|PutBucket|DeleteBucket|lambda:|iam:|secretsmanager:", aws_iam_role_policy.l5_artifact_publication[0].policy)) &&
      jsondecode(aws_iam_role.gateway_backend[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:aws-e/adp:environment:adp-gateway-deploy-test"
    )
    error_message = "The artifact profile must preserve protected OIDC trust and grant no provider execution or configuration authority."
  }
}
