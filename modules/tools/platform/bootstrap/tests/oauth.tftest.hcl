mock_provider "aws" {
  mock_data "aws_secretsmanager_secret" {
    defaults = {
      arn        = "arn:aws:secretsmanager:us-east-1:123456789012:secret:github-example-AbCdEf"
      kms_key_id = ""
    }
  }
  mock_data "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000002" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001" }
  }
}

variables {
  aws_account_id        = "123456789012"
  tools_facade_role_arn = "arn:aws:iam::123456789012:role/adp-dev-role-tools-facade"
  gateway_arn           = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-example"
  gateway_oauth = {
    workload_identity_arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:workload-identity-directory/default/workload-identity/adp-tools-example"
    providers = {
      github = {
        provider_arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/oauth2credentialprovider/adp-tools-dev-github-user"
        secret_id    = "github-example"
      }
      atlassian = {
        provider_arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/oauth2credentialprovider/adp-tools-dev-atlassian-user"
        secret_id    = "arn:aws:secretsmanager:us-east-1:123456789012:secret:jira-example-AbCdEf"
      }
      slack = {
        provider_arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/oauth2credentialprovider/adp-tools-dev-slack-user"
        secret_id    = "arn:aws:secretsmanager:us-east-1:123456789012:secret:slack-example-AbCdEf"
      }
    }
  }
}

run "disabled_oauth_has_no_token_or_secret_grants" {
  command = plan
  variables { gateway_oauth = null }
  assert {
    condition = (
      length(aws_iam_role_policy.gateway_oauth) == 0 &&
      length(data.aws_secretsmanager_secret.gateway_oauth) == 0 &&
      length(data.aws_kms_key.gateway_oauth_secret) == 0
    )
    error_message = "Without OAuth targets bootstrap must not resolve secrets or grant outbound credential access."
  }
}

run "enabled_oauth_grants_only_configured_resources" {
  command = apply
  assert {
    condition = (
      aws_iam_role_policy.gateway_oauth[0].role == aws_iam_role.gateway.id &&
      jsondecode(aws_iam_role_policy.gateway_oauth[0].policy).Statement == [
        {
          Sid    = "GatewayWorkloadToken"
          Effect = "Allow"
          Action = ["bedrock-agentcore:GetWorkloadAccessToken"]
          Resource = [
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:workload-identity-directory/default",
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:workload-identity-directory/default/workload-identity/adp-tools-example"
          ]
        },
        {
          Sid    = "VendorOauthTokens"
          Effect = "Allow"
          Action = ["bedrock-agentcore:GetResourceOauth2Token"]
          Resource = [
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/oauth2credentialprovider/adp-tools-dev-atlassian-user",
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/oauth2credentialprovider/adp-tools-dev-github-user",
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/oauth2credentialprovider/adp-tools-dev-slack-user"
          ]
        },
        {
          Sid    = "ExternalOauthClientSecrets"
          Effect = "Allow"
          Action = ["secretsmanager:GetSecretValue"]
          Resource = [
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:jira-example-AbCdEf",
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:github-example-AbCdEf",
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:slack-example-AbCdEf"
          ]
        }
      ]
    )
    error_message = "The gateway role needs only its workload token, three provider tokens and exact external secrets, not user-workload impersonation or wildcard access."
  }
}

run "customer_key_decryption_is_secret_bound" {
  command = plan
  override_data {
    target = data.aws_secretsmanager_secret.gateway_oauth["github"]
    values = {
      arn        = "arn:aws:secretsmanager:us-east-1:123456789012:secret:github-example-AbCdEf"
      kms_key_id = "alias/oauth-example"
    }
  }
  assert {
    condition = (
      length(jsondecode(aws_iam_role_policy.gateway_oauth[0].policy).Statement) == 4 &&
      jsondecode(aws_iam_role_policy.gateway_oauth[0].policy).Statement[3].Action == ["kms:Decrypt"] &&
      jsondecode(aws_iam_role_policy.gateway_oauth[0].policy).Statement[3].Resource == ["arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000002"] &&
      jsondecode(aws_iam_role_policy.gateway_oauth[0].policy).Statement[3].Condition.StringEquals == {
        "kms:ViaService" = "secretsmanager.us-east-1.amazonaws.com"
        "kms:EncryptionContext:SecretARN" = [
          "arn:aws:secretsmanager:us-east-1:123456789012:secret:jira-example-AbCdEf",
          "arn:aws:secretsmanager:us-east-1:123456789012:secret:github-example-AbCdEf",
          "arn:aws:secretsmanager:us-east-1:123456789012:secret:slack-example-AbCdEf"
        ]
      }
    )
    error_message = "A customer key may decrypt only configured external OAuth secrets through Secrets Manager."
  }
}

run "reject_foreign_workload" {
  command = plan
  variables {
    gateway_oauth = merge(var.gateway_oauth, {
      workload_identity_arn = "arn:aws:bedrock-agentcore:us-east-1:999999999999:workload-identity-directory/default/workload-identity/adp-tools-example"
    })
  }
  expect_failures = [var.gateway_oauth]
}

run "reject_foreign_provider" {
  command = plan
  variables {
    gateway_oauth = merge(var.gateway_oauth, {
      providers = { for vendor, provider in var.gateway_oauth.providers : vendor => merge(provider, {
        provider_arn = replace(provider.provider_arn, "123456789012", "999999999999")
      }) }
    })
  }
  expect_failures = [var.gateway_oauth]
}

run "reject_foreign_secret" {
  command = plan
  variables {
    gateway_oauth = merge(var.gateway_oauth, {
      providers = { for vendor, provider in var.gateway_oauth.providers : vendor => merge(provider, {
        secret_id = "arn:aws:secretsmanager:us-east-1:999999999999:secret:example-AbCdEf"
      }) }
    })
  }
  expect_failures = [var.gateway_oauth]
}

run "single_provider_does_not_grant_other_providers" {
  command = apply
  variables {
    gateway_oauth = merge(var.gateway_oauth, {
      providers = { atlassian = var.gateway_oauth.providers.atlassian }
    })
  }
  assert {
    condition = (
      length(data.aws_secretsmanager_secret.gateway_oauth) == 1 &&
      jsondecode(aws_iam_role_policy.gateway_oauth[0].policy).Statement[1].Resource == [
        "arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/oauth2credentialprovider/adp-tools-dev-atlassian-user"
      ]
    )
    error_message = "Jira must be configurable independently without granting GitHub or Slack access."
  }
}

run "reject_unknown_provider" {
  command = plan
  variables {
    gateway_oauth = merge(var.gateway_oauth, {
      providers = { unknown = var.gateway_oauth.providers.github }
    })
  }
  expect_failures = [var.gateway_oauth]
}
