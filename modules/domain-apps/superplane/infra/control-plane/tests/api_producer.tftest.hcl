mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

variables {
  environment          = "dev"
  aws_region           = "us-east-1"
  account_id           = "111122223333"
  cors_allowed_origins = ["https://superplane.dev.adp.internal"]
  database_secret_name = "adp/dev/superplane/database"
  jwt_secret_name      = "adp/dev/superplane/jwt-signing-key"
}

override_data {
  target = data.aws_caller_identity.current
  values = {
    account_id = "111122223333"
  }
}

override_data {
  target = data.terraform_remote_state.platform
  values = {
    outputs = {
      eks_oidc_provider_arn = "arn:aws:iam::111122223333:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E"
      eks_oidc_issuer       = "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E"
    }
  }
}

run "producer_omitted_by_default" {
  command = plan
  assert {
    condition     = length(aws_iam_role.api_producer) == 0
    error_message = "Default plans must not create a producer role."
  }
}

run "producer_exact_api_identity_and_routes" {
  command = plan
  variables {
    api_producer_role = { api_id = "abcdefghij", stage = "dev" }
  }
  assert {
    condition     = aws_iam_role.api_producer[0].name == "adp-dev-superplane-api-producer" && aws_iam_role.api_producer[0].path == "/"
    error_message = "Producer identity must have the exact domain-owned name and path."
  }
  assert {
    condition     = jsondecode(aws_iam_role.api_producer[0].assume_role_policy) == jsondecode(local.api_producer_trust)
    error_message = "Producer trust must be the exact reviewed OIDC document."
  }
  assert {
    condition     = jsondecode(aws_iam_role.api_producer[0].assume_role_policy).Statement[0].Condition.StringEquals["${local.oidc_issuer}:sub"] == "system:serviceaccount:superplane:superplane-api" && jsondecode(aws_iam_role.api_producer[0].assume_role_policy).Statement[0].Condition.StringEquals["${local.oidc_issuer}:aud"] == "sts.amazonaws.com"
    error_message = "Only the selected API service account and STS audience may assume this role."
  }
  assert {
    condition     = length(aws_iam_role.api_producer[0].managed_policy_arns) == 0 && length(aws_iam_role.api_producer[0].inline_policy) == 1
    error_message = "Producer role must own exactly one inline policy and no attached grants."
  }
  assert {
    condition = jsondecode(one(aws_iam_role.api_producer[0].inline_policy).policy).Statement[0].Action == "execute-api:Invoke" && toset(jsondecode(one(aws_iam_role.api_producer[0].inline_policy).policy).Statement[0].Resource) == toset([
      "arn:aws:execute-api:us-east-1:111122223333:abcdefghij/dev/POST/internal/v1/controller-execution/producer-readiness",
      "arn:aws:execute-api:us-east-1:111122223333:abcdefghij/dev/POST/internal/v1/controller-execution/verify-run",
      "arn:aws:execute-api:us-east-1:111122223333:abcdefghij/dev/POST/internal/v1/controller-execution/dispatch"
    ])
    error_message = "Invoke authority must be exactly the three selected POST routes."
  }
}
