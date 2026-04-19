# Data sources
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# Cognito User Pool
resource "aws_cognito_user_pool" "main" {
  name = "${var.name_prefix}-users"

  # Pre Token Generation Lambda Trigger (V2) - Issue #119
  # Injects custom claims into access tokens for both users and M2M clients
  lambda_config {
    pre_token_generation_config {
      lambda_arn     = aws_lambda_function.pre_token_generation.arn
      lambda_version = "V2_0" # Use V2 trigger for access token customization
    }
  }

  # Password policy - minimum 12 chars, require all character types
  password_policy {
    minimum_length                   = var.password_minimum_length
    require_uppercase                = true
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 7
  }

  # MFA configuration
  mfa_configuration = var.mfa_configuration

  software_token_mfa_configuration {
    enabled = var.enable_software_mfa
  }

  # Account recovery via email
  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # Auto-verified attributes
  auto_verified_attributes = ["email"]

  # Email verification
  verification_message_template {
    default_email_option = "CONFIRM_WITH_CODE"
    email_subject        = "Your Bedrock Gateway verification code"
    email_message        = "Your verification code is {####}"
  }

  # Standard schema attributes
  schema {
    name                     = "email"
    attribute_data_type      = "String"
    mutable                  = true
    required                 = true
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 1
      max_length = 256
    }
  }

  schema {
    name                     = "name"
    attribute_data_type      = "String"
    mutable                  = true
    required                 = true
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 1
      max_length = 256
    }
  }

  # Custom attributes for tenant mapping
  schema {
    name                     = "org_id"
    attribute_data_type      = "String"
    mutable                  = true
    required                 = false
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 0
      max_length = 255
    }
  }

  schema {
    name                     = "department_id"
    attribute_data_type      = "String"
    mutable                  = true
    required                 = false
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 0
      max_length = 255
    }
  }

  schema {
    name                     = "team_id"
    attribute_data_type      = "String"
    mutable                  = true
    required                 = false
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 0
      max_length = 255
    }
  }

  schema {
    name                     = "role"
    attribute_data_type      = "String"
    mutable                  = true
    required                 = false
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 0
      max_length = 64
    }
  }

  schema {
    name                     = "github_username"
    attribute_data_type      = "String"
    mutable                  = true
    required                 = false
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 0
      max_length = 256
    }
  }

  # Username configuration
  username_configuration {
    case_sensitive = false
  }

  # Admin create user configuration
  admin_create_user_config {
    allow_admin_create_user_only = true

    invite_message_template {
      email_subject = "Welcome to Bedrock Gateway"
      email_message = "Your username is {username} and temporary password is {####}. Please log in and change your password."
      sms_message   = "Your username is {username} and temporary password is {####}"
    }
  }

  # User attribute update settings
  user_attribute_update_settings {
    attributes_require_verification_before_update = ["email"]
  }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-user-pool"
    Service = "cognito"
    Purpose = "user-authentication"
  })

  # Cognito does not allow modifying or removing schema attributes after creation.
  # Custom attributes added via CLI (e.g., custom:github_username) cause drift that
  # cannot be reconciled via UpdateUserPool. Ignore schema changes to prevent failures.
  lifecycle {
    ignore_changes = [schema]
  }
}

# User Pool Client (public client for CLI usage - no secret)
resource "aws_cognito_user_pool_client" "main" {
  name         = "${var.name_prefix}-client"
  user_pool_id = aws_cognito_user_pool.main.id

  # Auth flows
  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_ADMIN_USER_PASSWORD_AUTH"
  ]

  # No client secret for public clients (CLI usage)
  generate_secret = false

  # Token validity
  access_token_validity  = var.access_token_validity
  refresh_token_validity = var.refresh_token_validity
  id_token_validity      = var.id_token_validity

  token_validity_units {
    access_token  = "minutes"
    refresh_token = "minutes"
    id_token      = "minutes"
  }

  # Callback and logout URLs for OAuth flows
  callback_urls = var.callback_urls
  logout_urls   = var.logout_urls

  # Supported identity providers
  supported_identity_providers = ["COGNITO"]

  # OAuth flows configuration
  # Use only authorization code flow for security (PKCE for SPA)
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["openid", "email", "profile"]

  # Prevent token revocation on refresh
  enable_token_revocation = true

  # Read/write attributes
  read_attributes = [
    "email",
    "name",
    "custom:org_id",
    "custom:department_id",
    "custom:team_id",
    "custom:role"
  ]

  write_attributes = [
    "email",
    "name",
    "custom:org_id",
    "custom:department_id",
    "custom:team_id",
    "custom:role"
  ]
}

# User Pool Domain (Cognito-hosted or custom)
resource "aws_cognito_user_pool_domain" "main" {
  # Cognito domains are globally unique across all AWS accounts. Appending an
  # account-id suffix to the default form ensures two installations in different
  # accounts don't collide. Override via var.custom_domain for a vanity domain.
  domain       = var.custom_domain != "" ? var.custom_domain : "${var.name_prefix}-auth-${substr(data.aws_caller_identity.current.account_id, 4, 8)}"
  user_pool_id = aws_cognito_user_pool.main.id
}

# Cognito Identity Pool
resource "aws_cognito_identity_pool" "main" {
  identity_pool_name               = "${var.name_prefix}-identity"
  allow_unauthenticated_identities = false
  allow_classic_flow               = false

  cognito_identity_providers {
    client_id               = aws_cognito_user_pool_client.main.id
    provider_name           = aws_cognito_user_pool.main.endpoint
    server_side_token_check = true
  }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-identity-pool"
    Service = "cognito"
    Purpose = "identity-federation"
  })
}

# IAM Role for authenticated Cognito users (gateway-caller)
resource "aws_iam_role" "gateway_caller" {
  name = "${var.name_prefix}-gateway-caller"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = "cognito-identity.amazonaws.com"
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "cognito-identity.amazonaws.com:aud" = aws_cognito_identity_pool.main.id
          }
          "ForAnyValue:StringLike" = {
            "cognito-identity.amazonaws.com:amr" = "authenticated"
          }
        }
      }
    ]
  })

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-gateway-caller"
    Service = "iam"
    Purpose = "cognito-authenticated-users"
  })
}

# Policy for gateway-caller role - NO Bedrock permissions, only gateway access
resource "aws_iam_role_policy" "gateway_caller_policy" {
  name = "${var.name_prefix}-gateway-caller-policy"
  role = aws_iam_role.gateway_caller.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowStsGetCallerIdentity"
        Effect = "Allow"
        Action = [
          "sts:GetCallerIdentity"
        ]
        Resource = "*"
      }
      # Note: No Bedrock permissions here - users call the gateway,
      # and the gateway assumes a role with Bedrock permissions
    ]
  })
}

# Policy for session tags from Cognito for tenant mapping
resource "aws_iam_role_policy" "gateway_caller_session_tags" {
  name = "${var.name_prefix}-cognito-session-tags"
  role = aws_iam_role.gateway_caller.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sts:TagSession"
        ]
        Resource = "*"
      }
    ]
  })
}

# IAM Role for unauthenticated users (denied access)
resource "aws_iam_role" "unauthenticated" {
  name = "${var.name_prefix}-cognito-unauth"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = "cognito-identity.amazonaws.com"
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "cognito-identity.amazonaws.com:aud" = aws_cognito_identity_pool.main.id
          }
          "ForAnyValue:StringLike" = {
            "cognito-identity.amazonaws.com:amr" = "unauthenticated"
          }
        }
      }
    ]
  })

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-cognito-unauth"
    Service = "iam"
    Purpose = "cognito-unauthenticated-users"
  })
}

# Empty policy for unauthenticated users (no permissions)
resource "aws_iam_role_policy" "unauthenticated_policy" {
  name = "${var.name_prefix}-unauth-policy"
  role = aws_iam_role.unauthenticated.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Deny"
        Action   = "*"
        Resource = "*"
      }
    ]
  })
}

# Identity Pool Role Attachment
resource "aws_cognito_identity_pool_roles_attachment" "main" {
  identity_pool_id = aws_cognito_identity_pool.main.id

  roles = {
    "authenticated"   = aws_iam_role.gateway_caller.arn
    "unauthenticated" = aws_iam_role.unauthenticated.arn
  }

  # Role mapping based on Cognito groups (for org-level isolation)
  role_mapping {
    identity_provider         = "${aws_cognito_user_pool.main.endpoint}:${aws_cognito_user_pool_client.main.id}"
    ambiguous_role_resolution = "AuthenticatedRole"
    type                      = "Rules"

    mapping_rule {
      claim      = "custom:role"
      match_type = "Equals"
      role_arn   = aws_iam_role.gateway_caller.arn
      value      = "admin"
    }

    mapping_rule {
      claim      = "custom:role"
      match_type = "Equals"
      role_arn   = aws_iam_role.gateway_caller.arn
      value      = "user"
    }
  }
}

# =============================================================================
# Cognito Resource Server for OAuth2 Scopes (Issue #119)
# =============================================================================

# Resource Server defines custom OAuth2 scopes for the API
# Used by client_credentials grant for machine-to-machine (M2M) authentication
resource "aws_cognito_resource_server" "gateway" {
  identifier   = "bedrockgw"
  name         = "Bedrock Gateway API"
  user_pool_id = aws_cognito_user_pool.main.id

  # Custom scopes for different access levels
  scope {
    scope_name        = "invoke"
    scope_description = "Invoke Bedrock models via the gateway"
  }

  scope {
    scope_name        = "admin"
    scope_description = "Admin API access for management operations"
  }
}

# =============================================================================
# Agent App Client with client_credentials grant (Issue #119)
# =============================================================================

# App Client for agents/services using client_credentials flow (M2M)
# This client type generates a secret and is used for automated/programmatic access
resource "aws_cognito_user_pool_client" "agent" {
  name         = "${var.name_prefix}-agent-client"
  user_pool_id = aws_cognito_user_pool.main.id

  # Generate a client secret for client_credentials flow
  generate_secret = true

  # Only client_credentials flow for M2M authentication
  allowed_oauth_flows                  = ["client_credentials"]
  allowed_oauth_flows_user_pool_client = true

  # Allow the invoke scope by default for agents
  allowed_oauth_scopes = [
    "${aws_cognito_resource_server.gateway.identifier}/invoke"
  ]

  # Use Cognito as the identity provider
  supported_identity_providers = ["COGNITO"]

  # Token validity for agent tokens (longer than human tokens)
  access_token_validity = 60 # 60 minutes for agent access tokens

  token_validity_units {
    access_token = "minutes"
  }

  # Enable token revocation
  enable_token_revocation = true

  depends_on = [aws_cognito_resource_server.gateway]
}

# =============================================================================
# Secrets Manager for Agent Credentials (Issue #124)
# =============================================================================
# Store agent client credentials in Secrets Manager so agents/services can
# retrieve them programmatically for M2M authentication

resource "aws_secretsmanager_secret" "agent_cognito_creds" {
  name        = "${var.name_prefix}-agent-cognito-credentials"
  description = "Cognito App Client credentials for agent/M2M authentication"

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-agent-cognito-credentials"
    Service = "secrets-manager"
    Purpose = "agent-authentication"
  })
}

resource "aws_secretsmanager_secret_version" "agent_cognito_creds" {
  secret_id = aws_secretsmanager_secret.agent_cognito_creds.id
  secret_string = jsonencode({
    client_id      = aws_cognito_user_pool_client.agent.id
    client_secret  = aws_cognito_user_pool_client.agent.client_secret
    token_endpoint = "https://${aws_cognito_user_pool_domain.main.domain}.auth.${data.aws_region.current.id}.amazoncognito.com/oauth2/token"
    scope          = "${aws_cognito_resource_server.gateway.identifier}/invoke"
  })

  depends_on = [aws_cognito_user_pool_client.agent]
}
