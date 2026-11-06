mock_provider "aws" {}

override_data {
  target = data.aws_iam_role.checks
  values = {
    arn       = "arn:aws:iam::000000000101:role/adp-dev-browser-trusted-checks"
    id        = "adp-dev-browser-trusted-checks"
    unique_id = "AROA11111111111111111"
  }
}

override_data {
  target = data.aws_iam_role.gateway
  values = {
    arn = "arn:aws:iam::000000000101:role/adp-dev-role-gateway-service"
    id  = "adp-dev-role-gateway-service"
  }
}

override_resource {
  target = aws_iam_role.retirement
  values = {
    arn       = "arn:aws:iam::000000000101:role/adp-dev-product-browser-retirement"
    id        = "adp-dev-product-browser-retirement"
    unique_id = "AROA22222222222222222"
  }
}

variables {
  config = {
    account             = "000000000101"
    region              = "us-east-1"
    environment         = "dev"
    checks_role_id      = "AROA11111111111111111"
    internal_invoke_url = "https://example123.execute-api.us-east-1.amazonaws.com/dev"
  }
}

run "only_selected_browser_can_assume_retirement" {
  command = apply
  assert {
    condition = jsondecode(aws_iam_role.retirement.assume_role_policy).Statement == [{
      Effect    = "Allow", Action = "sts:AssumeRole",
      Principal = { AWS = "arn:aws:iam::000000000101:role/adp-dev-browser-trusted-checks" }
    }]
    error_message = "Retirement trust must name only the selected browser identity."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.retire.policy).Statement == [{
      Effect   = "Allow", Action = ["execute-api:Invoke"],
      Resource = "arn:aws:execute-api:us-east-1:000000000101:example123/dev/POST/internal/v1/product-evaluation/retire"
    }]
    error_message = "Retirement may invoke only the exact protected route."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.checks_assume.policy).Statement == [{
      Effect   = "Allow", Action = ["sts:AssumeRole"],
      Resource = "arn:aws:iam::000000000101:role/adp-dev-product-browser-retirement"
    }]
    error_message = "Browser checks may assume only the new retirement role."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.gateway_identity.policy).Statement == [{
      Effect   = "Allow", Action = ["iam:GetRole"],
      Resource = "arn:aws:iam::000000000101:role/adp-dev-product-browser-retirement"
    }]
    error_message = "Gateway needs only an identity read on the exact role."
  }
  assert {
    condition = (
      jsondecode(output.registration_item).agent_id.S == "00000000-0000-0000-0000-000008243002" &&
      jsondecode(output.registration_item).iam_role_id.S == "AROA22222222222222222" &&
      jsondecode(output.registration_item).credential_scopes.SS == ["internal:product-fixture:retire"] &&
      output.runtime_config["browser-retirement-role-arn"] == jsondecode(output.registration_item).role_arn.S
    )
    error_message = "The registry and runtime must select the same fixed identity and current IAM role."
  }
}

run "changed_browser_identity_cannot_inherit_trust" {
  command = plan
  override_data {
    target = data.aws_iam_role.checks
    values = {
      arn       = "arn:aws:iam::000000000101:role/adp-dev-browser-trusted-checks"
      id        = "adp-dev-browser-trusted-checks"
      unique_id = "AROA33333333333333333"
    }
  }
  expect_failures = [aws_iam_role.retirement]
}
