locals {
  oauth_provider_vendors = {
    github    = "GithubOauth2"
    atlassian = "AtlassianOauth2"
    slack     = var.slack_user_oauth ? "CustomOauth2" : "SlackOauth2"
  }
  oauth_target_names = {
    github    = "github"
    atlassian = "jira"
    slack     = "slack"
  }
  oauth_target_scopes = {
    github    = ["read:user"]
    atlassian = ["read:jira-work", "read:me", "offline_access"]
    slack     = ["users:read"]
  }
}

ephemeral "aws_secretsmanager_secret_version" "oauth_client" {
  provider  = aws.oauth_secrets
  for_each  = var.oauth_provider_secret_ids
  secret_id = each.value
}

resource "aws_bedrockagentcore_oauth2_credential_provider" "user_connection" {
  for_each                   = var.oauth_provider_secret_ids
  name                       = "adp-tools-${var.environment}-${each.key}-user"
  credential_provider_vendor = local.oauth_provider_vendors[each.key]

  oauth2_provider_config {
    dynamic "github_oauth2_provider_config" {
      for_each = each.key == "github" ? [each.value] : []
      content {
        client_id_wo                  = jsondecode(ephemeral.aws_secretsmanager_secret_version.oauth_client[each.key].secret_string).client_id
        client_credentials_wo_version = var.oauth_provider_credentials_version
        client_secret_source          = "EXTERNAL"
        client_secret_config {
          secret_id = github_oauth2_provider_config.value
          json_key  = "client_secret"
        }
      }
    }
    dynamic "atlassian_oauth2_provider_config" {
      for_each = each.key == "atlassian" ? [each.value] : []
      content {
        client_id_wo                  = jsondecode(ephemeral.aws_secretsmanager_secret_version.oauth_client[each.key].secret_string).client_id
        client_credentials_wo_version = var.oauth_provider_credentials_version
        client_secret_source          = "EXTERNAL"
        client_secret_config {
          secret_id = atlassian_oauth2_provider_config.value
          json_key  = "client_secret"
        }
      }
    }
    dynamic "slack_oauth2_provider_config" {
      for_each = each.key == "slack" && !var.slack_user_oauth ? [each.value] : []
      content {
        client_id_wo                  = jsondecode(ephemeral.aws_secretsmanager_secret_version.oauth_client[each.key].secret_string).client_id
        client_credentials_wo_version = var.oauth_provider_credentials_version
        client_secret_source          = "EXTERNAL"
        client_secret_config {
          secret_id = slack_oauth2_provider_config.value
          json_key  = "client_secret"
        }
      }
    }
    dynamic "custom_oauth2_provider_config" {
      for_each = each.key == "slack" && var.slack_user_oauth ? [each.value] : []
      content {
        client_id_wo                  = jsondecode(ephemeral.aws_secretsmanager_secret_version.oauth_client[each.key].secret_string).client_id
        client_credentials_wo_version = var.oauth_provider_credentials_version
        client_secret_source          = "EXTERNAL"
        client_authentication_method  = "CLIENT_SECRET_BASIC"
        client_secret_config {
          secret_id = custom_oauth2_provider_config.value
          json_key  = "client_secret"
        }
        oauth_discovery {
          authorization_server_metadata {
            issuer                      = "https://slack.com"
            authorization_endpoint      = "https://slack.com/oauth/v2_user/authorize"
            token_endpoint              = "https://slack.com/api/oauth.v2.user.access"
            response_types              = ["code"]
            token_endpoint_auth_methods = ["client_secret_basic"]
          }
        }
      }
    }
  }
}

resource "aws_bedrockagentcore_workload_identity" "oauth_user" {
  count                               = length(var.oauth_provider_secret_ids) == 0 ? 0 : 1
  name                                = "adp-tools-${var.environment}-oauth-user"
  allowed_resource_oauth2_return_urls = [var.oauth_return_url]

  lifecycle {
    precondition {
      condition     = var.oauth_return_url != ""
      error_message = "Register an HTTPS OAuth return URL with the workload identity when provider apps are configured."
    }
  }
}
