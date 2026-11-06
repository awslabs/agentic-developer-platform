mock_provider "aws" {
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/test" } }
  mock_resource "aws_kms_key" { defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001" } }
}

variables {
  aws_account_id              = "123456789012"
  tools_facade_role_arn       = "arn:aws:iam::123456789012:role/adp-dev-role-tools-facade"
  gateway_arn                 = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-example"
  knowledge_door_endpoint     = "https://fixtureapi.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/knowledge/mcp"
  knowledge_mediator_role_arn = "arn:aws:iam::123456789012:role/service/fixture-gateway-service"
  eks_oidc_provider_arn       = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/ABCDEF"
}

run "reject_knowledge_as_only_key_lifetime_anchor" {
  command = plan
  variables { eks_oidc_provider_arn = "" }
  expect_failures = [var.knowledge_mediator_role_arn]
}

run "exact_mediator_can_only_verify_exact_assertion_key" {
  command = apply
  assert {
    condition = (
      aws_iam_role_policy.knowledge_mediator_verify[0].role == "fixture-gateway-service" &&
      jsondecode(aws_iam_role_policy.knowledge_mediator_verify[0].policy).Statement == [{
        Effect = "Allow", Action = "kms:Verify", Resource = aws_kms_key.assertion[0].arn
      }]
    )
    error_message = "The mediator role must have only Verify on the exact assertion key, never signing or other keys."
  }
  assert {
    condition = [for s in jsondecode(aws_kms_key.assertion[0].policy).Statement : s if s.Sid == "VerifyFromKnowledgeMediatorOnly"] == [{
      Sid       = "VerifyFromKnowledgeMediatorOnly", Effect = "Allow",
      Principal = { AWS = var.knowledge_mediator_role_arn }, Action = "kms:Verify", Resource = "*"
    }]
    error_message = "The key itself must admit only the exact protected Gateway mediator principal for verification."
  }
  assert {
    condition = alltrue([for s in jsondecode(aws_kms_key.assertion[0].policy).Statement :
      s.Principal.AWS != var.knowledge_mediator_role_arn || s.Action == "kms:Verify"
    ])
    error_message = "No other assertion-key capability may be added to the mediator principal."
  }
}

run "reject_missing_mediator_role" {
  command = plan
  variables { knowledge_mediator_role_arn = "" }
  expect_failures = [var.knowledge_mediator_role_arn]
}

run "reject_foreign_account_role" {
  command = plan
  variables { knowledge_mediator_role_arn = "arn:aws:iam::999999999999:role/fixture-gateway-service" }
  expect_failures = [var.knowledge_mediator_role_arn]
}

run "reject_assumed_role_session" {
  command = plan
  variables { knowledge_mediator_role_arn = "arn:aws:sts::123456789012:assumed-role/fixture-gateway-service/session" }
  expect_failures = [var.knowledge_mediator_role_arn]
}

run "reject_facade_as_mediator" {
  command = plan
  variables { knowledge_mediator_role_arn = "arn:aws:iam::123456789012:role/adp-dev-role-tools-facade" }
  expect_failures = [var.knowledge_mediator_role_arn]
}

run "reject_agentcore_target_as_mediator" {
  command = plan
  variables { knowledge_mediator_role_arn = "arn:aws:iam::123456789012:role/adp-tools-dev-gateway" }
  expect_failures = [var.knowledge_mediator_role_arn]
}

run "empty_endpoint_has_no_mediator_authority" {
  command = apply
  variables {
    knowledge_door_endpoint = ""
  }
  assert {
    condition = (
      length(aws_iam_role_policy.knowledge_mediator_verify) == 0 &&
      length([for s in jsondecode(aws_kms_key.assertion[0].policy).Statement : s if s.Sid == "VerifyFromKnowledgeMediatorOnly"]) == 0
    )
    error_message = "Disabled Knowledge must create no role or key-policy verification grant, even with a configured role."
  }
}
