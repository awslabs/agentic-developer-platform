# =============================================================================
# Gateway service role: consolidated customer managed policies
# =============================================================================
# The gateway service role (adp-<env>-role-gateway-service, created by
# platform/infra/modules/eks/main.tf) is shared by four stacks: platform/infra,
# this module, modules/agent-factory (infra + webhook-ingress) and the Superplane
# domain-app stack. IAM caps that role at 10,240 non-whitespace characters of
# INLINE policy in aggregate and at 20 ATTACHED managed policies (the account
# quota is already at its 20 maximum). This module used to add up to twenty
# inline policies of its own; on dev the aggregate reached 10,144 bytes and every
# inline change failed with LimitExceeded.
#
# This file owns every grant this module gives the gateway role. The grants are
# grouped by concern into three customer managed policies, each well under the
# 6,144-character managed-policy limit:
#
#   adp-<env>-policy-gateway-identity  Cognito pool reads and lifecycle, the
#                                      identity / user-identity index tables,
#                                      user-scoped AgentCore OAuth, agent client
#                                      credentials.
#   adp-<env>-policy-gateway-data      Vault secrets and their CMK, CFN template
#                                      bucket, chat-log / PII / X-Ray sinks, RDS
#                                      and ElastiCache IAM auth, ingestion queue,
#                                      shared Jira credential workload setup.
#   adp-<env>-policy-gateway-agents    Model invocation on the gateway's own
#                                      credentials (mantle, pool roles), the tool
#                                      catalogue, run transcripts, crew run
#                                      bindings and checkpoints, AgentCore memory,
#                                      shared Jira credential provider setup.
#
# Statement semantics are unchanged: every statement keeps its Sid, actions,
# resources and conditions from the inline policy it came from. Two things are
# deliberately different from a mechanical merge:
#   - the identical "DynamoDBKMSAccess" statement that both identity-index
#     policies carried appears once (IAM requires unique Sids per document);
#   - statements that had no Sid were given one so the upgrade gate in
#     platform/scripts/upgrade-plan-policy.py can name them.
# modules/gateway/tests/infra/test_gateway_role_iam_budget.py renders the three
# documents and the other stacks' inline grants and fails if any managed policy
# exceeds 6,144 characters, the role's inline aggregate exceeds 8,192 or the
# attached count can exceed 20.
#
# -----------------------------------------------------------------------------
# Upgrading an environment that still has the inline policies (two passes)
# -----------------------------------------------------------------------------
# A single apply would destroy the inline grants and create the managed ones
# concurrently, so the role could briefly lack a permission. Instead:
#
#   pass 1  gateway_role_policy_cutover_keep_legacy = true and
#           gateway_role_policy_cutover_inline_documents = { <suffix> = <live JSON> }
#           (modules/gateway/scripts/gateway-role-policy-cutover.py writes this
#           file from the live role). The three managed policies are created and
#           attached; every live inline policy is retained byte-for-byte (no
#           PutRolePolicy, which is what fails at the limit) under the
#           aws_iam_role_policy.gateway_role_legacy["<suffix>"] addresses the
#           `moved` blocks below map it to; the retired managed policies stay.
#           gateway_role_policy_cutover_managed_attachments records which of
#           their attachments exist, so pass 1 retains them without adding any.
#   pass 2  defaults. The legacy inline policies and the two retired managed
#           policies are destroyed. upgrade-plan-policy.py treats those destroys
#           as routine only when the attached consolidated policy that replaces
#           each one is already in state and contains every retired statement.
#
# Fresh environments never set the cutover variables and get only the three
# managed policies. See docs/adp-platform-deployment/deployment-reference.md
# ("Gateway service role IAM budget") for the operator steps.
# =============================================================================

variable "gateway_role_policy_cutover_keep_legacy" {
  type        = bool
  default     = false
  description = "Upgrade-only first pass: keep the retired inline and managed gateway-role policies while the consolidated managed policies are created and attached."
}

variable "gateway_role_policy_cutover_inline_documents" {
  type        = map(string)
  default     = {}
  description = "Upgrade-only first pass: live inline policy documents to retain unchanged, keyed by the suffix of their bedrockgw-<env>-[policy-]gateway-<suffix> name."

  validation {
    condition     = alltrue([for document in values(var.gateway_role_policy_cutover_inline_documents) : can(jsondecode(document))])
    error_message = "Every retained gateway-role inline policy must be a JSON document."
  }
}

variable "gateway_role_policy_cutover_managed_attachments" {
  type        = set(string)
  default     = []
  description = "Upgrade-only first pass: names of the retired managed policies already attached to the gateway role. The cutover helper inventories these so absent legacy attachments are never recreated."
}

locals {
  gateway_role_policy_region  = data.aws_region.current.name
  gateway_role_policy_account = data.aws_caller_identity.current.account_id
  gateway_cognito_pool_arn    = "arn:aws:cognito-idp:${local.gateway_role_policy_region}:${local.gateway_role_policy_account}:userpool/${module.cognito.cognito_user_pool_id}"
  gateway_secret_arn_prefix   = "arn:aws:secretsmanager:${local.gateway_role_policy_region}:${local.gateway_role_policy_account}:secret"

  # ---------------------------------------------------------------------------
  # identity
  # ---------------------------------------------------------------------------
  gateway_role_identity_statements = concat(
    [
      {
        # Read: onboarding identity lookup, admin group/user listing.
        # Write (AdminUpdateUserAttributes): onboarding approval syncs the
        # approved user's role/org onto their Cognito custom: attributes so the
        # pre-token Lambda emits them in the access token (see
        # admin/onboarding/approval.py). Without this the approved user logs in
        # with an empty role/org -> broken SPA nav + dashboard. Scoped to this
        # pool only.
        Sid    = "CognitoReadUsers"
        Effect = "Allow"
        Action = [
          "cognito-idp:ListUsers",
          "cognito-idp:ListGroups",
          "cognito-idp:ListUsersInGroup",
          "cognito-idp:AdminGetUser",
          "cognito-idp:AdminUpdateUserAttributes"
        ]
        Resource = local.gateway_cognito_pool_arn
      },
      {
        # Native identity lifecycle (#5010). Keep every write scoped to the
        # gateway pool; GetGroup is needed for idempotent CreateGroup retries.
        Sid    = "CognitoIdentityLifecycle"
        Effect = "Allow"
        Action = [
          "cognito-idp:AdminCreateUser",
          "cognito-idp:AdminDeleteUser",
          "cognito-idp:AdminAddUserToGroup",
          "cognito-idp:AdminRemoveUserFromGroup",
          "cognito-idp:AdminListGroupsForUser",
          "cognito-idp:CreateGroup",
          "cognito-idp:GetGroup",
          "cognito-idp:DeleteGroup"
        ]
        Resource = local.gateway_cognito_pool_arn
      },
      {
        # Authorized machine-agent lifecycle (#5624). Cognito client creation,
        # protected credential delivery and terminal retirement all use this
        # existing gateway pool. This is static gateway authority, not worker
        # or per-task permission, and grants no client update/secret rotation.
        Sid    = "CognitoMachineClientLifecycle"
        Effect = "Allow"
        Action = [
          "cognito-idp:CreateUserPoolClient",
          "cognito-idp:DescribeUserPoolClient",
          "cognito-idp:DeleteUserPoolClient"
        ]
        Resource = local.gateway_cognito_pool_arn
      },
      {
        # Web CLI login (/auth/cli): mints tokens on the CLI app client the
        # same way the github-auth-broker does -- fresh random permanent
        # password + admin auth. Only invoked after the signed-in browser
        # user approves, and only for broker-provisioned GitHub_* users
        # (who never hold a real password). Scoped to this pool only.
        Sid    = "CognitoCliLoginMint"
        Effect = "Allow"
        Action = [
          "cognito-idp:AdminSetUserPassword",
          "cognito-idp:AdminInitiateAuth",
          "cognito-idp:AdminRespondToAuthChallenge"
        ]
        Resource = local.gateway_cognito_pool_arn
      },
      {
        Sid    = "IdentityIndexReadWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:ConditionCheckItem",
          "dynamodb:BatchWriteItem",
          "dynamodb:DeleteItem",
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:Query",
          "dynamodb:UpdateItem"
        ]
        Resource = [
          aws_dynamodb_table.identity_index.arn
        ]
      },
      {
        Sid      = "AgentClientRegistryRead"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = [module.cognito.agent_clients_table_arn]
      },
      {
        # Both index tables are SSE-KMS with this module's DynamoDB key. The
        # former identity-index and user-identity-index inline policies each
        # carried this exact statement; it appears once here.
        Sid    = "DynamoDBKMSAccess"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = [aws_kms_key.dynamodb.arn]
      },
      {
        # Secrets Manager read access for agent Cognito credentials (Issue #33).
        # The gateway (and E2E tests running in-cluster) need to read the agent
        # client_credentials secret to obtain M2M tokens.
        Sid    = "SecretsManagerReadAgentCreds"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret"
        ]
        Resource = "${local.gateway_secret_arn_prefix}:bedrockgw-*"
      }
    ],
    local.gateway_user_identity_index_statements,
    local.gateway_user_oauth_statements,
    local.gateway_atlassian_mcp_statements,
  )

  # ---------------------------------------------------------------------------
  # data
  # ---------------------------------------------------------------------------
  gateway_role_data_statements = concat(
    [
      {
        # Vault credentials -- full CRUD across all 4 ownership scopes. The
        # gateway is the sole writer/reader of user-vault secrets; agent pods
        # have no direct Secrets Manager access and must route through
        # /internal/v1/proxy-request (docs/user-identity-and-credentials-design.md).
        #
        # Namespaces (issue #440 scope relaxation):
        #   adp/users/<cognito_sub>/*           -- user-owned
        #   adp/teams/<team_id>/*               -- team-owned
        #   adp/orgs/<org_id>/*                 -- tenant-owned
        #   adp/domain-apps/<app>/<org_id>/*    -- domain-app, per-tenant install
        Sid    = "VaultSecretsCRUD"
        Effect = "Allow"
        Action = [
          "secretsmanager:CreateSecret",
          "secretsmanager:GetSecretValue",
          "secretsmanager:PutSecretValue",
          "secretsmanager:UpdateSecret",
          "secretsmanager:DescribeSecret",
          "secretsmanager:DeleteSecret",
          "secretsmanager:TagResource",
          "secretsmanager:UntagResource"
        ]
        Resource = [
          "${local.gateway_secret_arn_prefix}:adp/users/*",
          "${local.gateway_secret_arn_prefix}:adp/teams/*",
          "${local.gateway_secret_arn_prefix}:adp/orgs/*",
          "${local.gateway_secret_arn_prefix}:adp/domain-apps/*",
          "${local.gateway_secret_arn_prefix}:adp/*/github-app/*",
          # Issue #3473: per-tenant GitHub App credentials seeded by
          # connections/tenant_secret.py at adp/<env>/tenants/<org_id>/github-app.
          "${local.gateway_secret_arn_prefix}:adp/*/tenants/*/github-app*",
          # Issue #2708: the register flow writes the broker's OAuth client_id/
          # secret here so "Sign in with GitHub" works right after registration,
          # and get_app_status reads it to report login_enabled. Narrow to this
          # exact secret family -- NOT adp/*/cognito/*.
          "${local.gateway_secret_arn_prefix}:adp/*/cognito/github-oauth-credentials*",
          # Issue #2824: the register flow writes the manifest-conversion
          # webhook_secret here so webhooks from a UI-registered App pass HMAC
          # validation in the webhook-ingress Lambda. Terraform seeds this secret
          # with a placeholder and never updates it. Narrow to this exact secret.
          "${local.gateway_secret_arn_prefix}:adp/*/webhook-ingress/github-webhook-secret*",
          # Issue #3529: the gateway resolver needs to read ops-App credentials
          # (gh-app-ops-id / gh-app-ops-key) so it resolves installation_ids
          # using the SAME App the ingestion worker mints tokens with.
          "${local.gateway_secret_arn_prefix}:adp/*/gh-app-ops-*"
        ]
      },
      {
        # #5634: staged supplied-key activation moves only this deployment's
        # GitHub App key version. Never grant version-stage writes to the vault.
        Sid      = "GitHubAppKeyActivation"
        Effect   = "Allow"
        Action   = ["secretsmanager:UpdateSecretVersionStage"]
        Resource = "${local.gateway_secret_arn_prefix}:adp/${var.environment}/github-app/adp-agent-platform-key-??????"
      },
      {
        # ListSecrets is account-wide by necessity (no resource-level scoping).
        # The gateway uses it to enumerate its own vault inventory (orphan
        # sweeper, admin listings, per-user quota checks).
        Sid      = "VaultSecretsList"
        Effect   = "Allow"
        Action   = ["secretsmanager:ListSecrets"]
        Resource = "*"
      },
      {
        # Issue #2652 / #2797 / #3789: the github-app secrets are encrypted with
        # the webhook-ingress CMK (alias/adp-<env>-webhook-secrets, owned by
        # platform infra). Read AND write material because the register-app flow
        # writes those secrets. Referenced by alias so rotation keeps working.
        Sid    = "WebhookSecretsKMSReadWrite"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
          "kms:Encrypt",
          "kms:GenerateDataKey*"
        ]
        Resource = [data.aws_kms_alias.webhook_secrets.target_key_arn]
      },
      {
        Sid      = "InsightsResultsRead"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "arn:aws:s3:::adp-${var.environment}-insights-evidence/results/v1/*"
      },
      {
        Sid      = "InsightsResultsDecrypt"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "arn:aws:kms:${local.gateway_role_policy_region}:${local.gateway_role_policy_account}:key/*"
        Condition = {
          "ForAnyValue:StringEquals" = { "kms:ResourceAliases" = "alias/adp-${var.environment}-insights-evidence" }
        }
      },
      {
        # CFN template read (issue #562): the gateway pre-signs an S3 GET URL
        # for the CloudFormation template hosted in the frontend bucket.
        Sid      = "CfnTemplateGet"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${module.frontend_s3.bucket_arn}/cfn-templates/*"
      },
      {
        # The SDK/console fetch path also performs s3:ListBucket on the bucket
        # (bucket-level, so scoped by prefix here rather than object ARN).
        Sid      = "CfnTemplateList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = module.frontend_s3.bucket_arn
        Condition = {
          StringLike = {
            "s3:prefix" = ["cfn-templates/*"]
          }
        }
      }
    ],
    var.enable_chat_logging ? [
      {
        Sid    = "ChatLogsS3Write"
        Effect = "Allow"
        # #9026: the owner's "My conversations" turns are read back from the same scrubbed logs the gateway
        # writes (dev 2026-10-27: HeadObject 403 left every conversation as metadata-only). Same bucket,
        # same prefix, still no List or Delete.
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = "${module.s3_chat_logs[0].bucket_arn}/*"
      },
      {
        Sid      = "ChatLogsS3BucketAccess"
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation"]
        Resource = module.s3_chat_logs[0].bucket_arn
      }
    ] : [],
    var.enable_chat_logging && var.chat_logging_scrub_level == "standard" ? [
      {
        Sid      = "ComprehendPiiDetection"
        Effect   = "Allow"
        Action   = ["comprehend:DetectPiiEntities"]
        Resource = "*"
      }
    ] : [],
    var.enable_rds_iam_auth ? [
      {
        Sid      = "RdsIamConnect"
        Effect   = "Allow"
        Action   = ["rds-db:connect"]
        Resource = "arn:aws:rds-db:${local.gateway_role_policy_region}:${local.gateway_role_policy_account}:dbuser:*/${var.rds_username}"
      }
    ] : [],
    var.enable_redis && var.enable_elasticache_iam_auth ? [
      {
        Sid    = "ElastiCacheIamConnect"
        Effect = "Allow"
        Action = ["elasticache:Connect"]
        Resource = [
          "arn:aws:elasticache:${local.gateway_role_policy_region}:${local.gateway_role_policy_account}:replicationgroup:${module.redis[0].replication_group_id}",
          "arn:aws:elasticache:${local.gateway_role_policy_region}:${local.gateway_role_policy_account}:user:${module.redis[0].redis_iam_user_id}"
        ]
      }
    ] : [],
    var.enable_xray_tracing ? [
      {
        Sid    = "XRayTracing"
        Effect = "Allow"
        Action = [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets"
        ]
        Resource = "*"
      }
    ] : [],
    var.enable_agent_context_sqs ? [
      {
        # Issue #1797: Phase 1 inline ingestion dispatch. The gateway publishes
        # to the agent-context ingestion queue when a user registers or
        # reindexes a knowledge asset.
        Sid    = "IngestionSQSPublish"
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:GetQueueUrl"
        ]
        Resource = var.agent_context_ingestion_queue_arn != "" ? var.agent_context_ingestion_queue_arn : "arn:${data.aws_partition.current.partition}:sqs:${var.aws_region}:${local.gateway_role_policy_account}:${local.cluster_name}-context-ingestion"
      }
    ] : [],
    var.enable_l5_security ? [
      {
        Sid      = "L5SecuritySQSPublish"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = module.l5_security[0].work_queue_arn
      }
    ] : [],
    local.gateway_oauth_app_replacement_statements,
    local.gateway_atlassian_mcp_secret_statements,
    local.gateway_jira_system_workload_statements,
  )

  # ---------------------------------------------------------------------------
  # agents
  # ---------------------------------------------------------------------------
  gateway_role_agents_statements = concat(
    # Split the new workload/provider grants across existing data and agents
    # documents so all three policies retain their required growth room.
    local.gateway_jira_system_provider_statements,
    [
      {
        Sid       = "ProductEvaluatorRoleIdentity"
        Effect    = "Allow"
        Action    = ["iam:GetRole"]
        Resource  = "arn:aws:iam::${local.gateway_role_policy_account}:role/adp-*-instance"
        Condition = { StringEquals = { "iam:ResourceTag/adp:managed-by" = "evaluator-setup" } }
      },
    ],
    var.environment == "dev" ? [{
      Sid      = "ProductLegacyEvaluatorRoleIdentity"
      Effect   = "Allow"
      Action   = ["iam:GetRole"]
      Resource = "arn:aws:iam::${local.gateway_role_policy_account}:role/adp-cli-uplift-eval-instance"
      Condition = { StringEquals = {
        "iam:ResourceTag/Project" = "adp", "iam:ResourceTag/Environment" = "dev", "iam:ResourceTag/Purpose" = "cli-uplift-eval"
      } }
    }] : [],
    var.enable_mantle_passthrough ? [
      {
        # Bedrock InvokeModel for the bedrock-mantle / OpenAI passthrough
        # (Issue #2709). The route signs upstream requests with SigV4 using the
        # gateway pod's OWN credentials -- unlike the Claude proxy path, which
        # assumes a cross-account pool role.
        Sid    = "MantleBedrockInvoke"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]
        Resource = "*"
      },
      {
        # Vestigial since the route moved to bedrock-runtime (Issue #2817);
        # retained harmlessly for rollback to the preview host.
        Sid    = "MantleCreateInference"
        Effect = "Allow"
        Action = [
          "bedrock-mantle:CreateInference"
        ]
        Resource = "arn:aws:bedrock-mantle:${local.gateway_role_policy_region}:${local.gateway_role_policy_account}:project/default"
      }
    ] : [],
    length(var.pool_account_arns) > 0 ? [
      {
        # Cross-account Bedrock pool assume role (if pool accounts configured).
        Sid      = "BedrockPoolAssumeRole"
        Effect   = "Allow"
        Action   = ["sts:AssumeRole"]
        Resource = [for acct in var.pool_account_arns : "arn:aws:iam::${acct}:role/*BedrockGateway-Pool*"]
      }
    ] : [],
    [
      {
        # Admin catalogue reads the deployed tool gateway's target schemas;
        # invocation remains exclusively authorized for the separate tools
        # facade role.
        Sid      = "ToolsCatalogueListGateways"
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:ListGateways"]
        Resource = "*"
      },
      {
        Sid    = "ToolsCatalogueReadTargets"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:GetGateway",
          "bedrock-agentcore:ListGatewayTargets",
          "bedrock-agentcore:GetGatewayTarget"
        ]
        Resource = "arn:aws:bedrock-agentcore:${local.gateway_role_policy_region}:${local.gateway_role_policy_account}:gateway/*"
      },
      {
        # Agent run-logs transcripts -- read-only (Issue #3069 / #3105). GetObject
        # only -- no List, no Put. Scoped to this account's bucket by convention.
        Sid      = "AgentRunLogsRead"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "arn:aws:s3:::adp-${var.environment}-agent-run-logs-${local.gateway_role_policy_account}/*"
      }
    ],
    local.worker_events_table != "" && local.worker_events_key != "" ? [
      {
        # Persistent chat run bindings share the worker run table, but only the
        # gateway writes their lease-scoped rows; the supervisor has no direct
        # table permission.
        # CrewModelUsage.invoke (src/crew/model_usage.py) reserves a Dev Box model
        # call with ONE TransactWriteItems: ConditionCheck on the run row and on the
        # crew-lease marker plus a conditional Put of the receipt. DynamoDB authorizes
        # every ConditionCheck item as dynamodb:ConditionCheckItem; without it the
        # whole transaction is AccessDenied (#7983) and the gateway answered every
        # Dev Box model call with 401 crew_model_credential_denied (#8223 cause 32,
        # dev 2026-10-20: the first real model call from a box).
        Sid      = "CrewRunBindingTable"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:ConditionCheckItem"]
        Resource = ["arn:aws:dynamodb:${local.gateway_role_policy_region}:${local.gateway_role_policy_account}:table/${local.worker_events_table}"]
      },
      {
        Sid      = "CrewRunBindingKms"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"]
        Resource = [local.worker_events_key]
      }
    ] : [],
    local.gateway_crew_checkpoint_statements,
    local.gateway_agentcore_memory_statements,
    local.gateway_oauth_app_statements,
  )

  gateway_role_policy_statements = {
    identity = local.gateway_role_identity_statements
    data     = local.gateway_role_data_statements
    agents   = local.gateway_role_agents_statements
  }

  gateway_role_policy_descriptions = {
    identity = "Gateway Cognito pool, identity index tables, user OAuth and agent client credentials"
    data     = "Gateway vault secrets, CFN template bucket, logging sinks and data-store IAM auth"
    agents   = "Gateway model invocation, tool catalogue, run transcripts, crew bindings and memory"
  }
}

resource "aws_iam_policy" "gateway_role" {
  for_each = local.gateway_role_policy_statements

  name        = "adp-${var.environment}-policy-gateway-${each.key}"
  description = local.gateway_role_policy_descriptions[each.key]
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = each.value
  })

  lifecycle {
    precondition {
      condition     = length(distinct([for statement in each.value : statement.Sid])) == length(each.value)
      error_message = "Every statement in a consolidated gateway-role policy needs a unique Sid."
    }
  }
}

resource "aws_iam_role_policy_attachment" "gateway_role" {
  for_each = aws_iam_policy.gateway_role

  role       = local.gateway_service_irsa_role_name
  policy_arn = each.value.arn
}

# -----------------------------------------------------------------------------
# Retired policies, retained only during pass 1 of an upgrade
# -----------------------------------------------------------------------------
locals {
  gateway_role_legacy_inline_names = {
    cognito-read          = "${local.name_prefix}-policy-gateway-cognito-read"
    cfn-template-read     = "${local.name_prefix}-policy-gateway-cfn-template-read"
    chat-logs-s3          = "${local.name_prefix}-policy-gateway-chat-logs-s3"
    run-logs-read         = "${local.name_prefix}-policy-gateway-run-logs-read"
    comprehend-pii        = "${local.name_prefix}-policy-gateway-comprehend-pii"
    mantle-bedrock-invoke = "${local.name_prefix}-policy-gateway-mantle-bedrock-invoke"
    xray-tracing          = "${local.name_prefix}-policy-gateway-xray-tracing"
    secretsmanager-read   = "${local.name_prefix}-policy-gateway-secretsmanager-read"
    vault-secrets         = "${local.name_prefix}-policy-gateway-vault-secrets"
    webhook-secrets-kms   = "${local.name_prefix}-policy-gateway-webhook-secrets-kms"
    cross-account         = "${local.name_prefix}-policy-gateway-cross-account"
    rds-iam-auth          = "${local.name_prefix}-policy-gateway-rds-iam-auth"
    elasticache-iam-auth  = "${local.name_prefix}-policy-gateway-elasticache-iam-auth"
    identity-index        = "${local.name_prefix}-policy-gateway-identity-index"
    ingestion-sqs-publish = "${local.name_prefix}-policy-gateway-ingestion-sqs-publish"
    user-identity-index   = "${local.name_prefix}-policy-gateway-user-identity-index"
    crew-checkpoints-s3   = "${local.name_prefix}-policy-gateway-crew-checkpoints-s3"
    crew-run-binding      = "${local.name_prefix}-gateway-crew-run-binding"
    user-oauth            = "${local.name_prefix}-gateway-user-oauth"
    agentcore-memory      = "${local.name_prefix}-gateway-agentcore-memory"
  }
}

# Pass 1 keeps each live inline policy exactly as it is: no PutRolePolicy at the
# limit, no permission gap. Pass 2 (empty map) destroys them.
resource "aws_iam_role_policy" "gateway_role_legacy" {
  for_each = var.gateway_role_policy_cutover_keep_legacy ? var.gateway_role_policy_cutover_inline_documents : {}

  name   = try(local.gateway_role_legacy_inline_names[each.key], "")
  role   = local.gateway_service_irsa_role_name
  policy = each.value

  lifecycle {
    precondition {
      condition     = contains(keys(local.gateway_role_legacy_inline_names), each.key)
      error_message = "Unknown retained gateway-role inline policy; keys must be one of the suffixes in local.gateway_role_legacy_inline_names."
    }
  }
}

moved {
  from = aws_iam_role_policy.gateway_cognito_read
  to   = aws_iam_role_policy.gateway_role_legacy["cognito-read"]
}
moved {
  from = aws_iam_role_policy.gateway_cfn_template_read
  to   = aws_iam_role_policy.gateway_role_legacy["cfn-template-read"]
}
moved {
  from = aws_iam_role_policy.gateway_chat_logs_s3[0]
  to   = aws_iam_role_policy.gateway_role_legacy["chat-logs-s3"]
}
moved {
  from = aws_iam_role_policy.gateway_run_logs_read
  to   = aws_iam_role_policy.gateway_role_legacy["run-logs-read"]
}
moved {
  from = aws_iam_role_policy.gateway_comprehend_pii[0]
  to   = aws_iam_role_policy.gateway_role_legacy["comprehend-pii"]
}
moved {
  from = aws_iam_role_policy.gateway_mantle_bedrock_invoke[0]
  to   = aws_iam_role_policy.gateway_role_legacy["mantle-bedrock-invoke"]
}
moved {
  from = aws_iam_role_policy.gateway_xray_tracing[0]
  to   = aws_iam_role_policy.gateway_role_legacy["xray-tracing"]
}
moved {
  from = aws_iam_role_policy.gateway_secretsmanager_read
  to   = aws_iam_role_policy.gateway_role_legacy["secretsmanager-read"]
}
moved {
  from = aws_iam_role_policy.gateway_vault_secrets
  to   = aws_iam_role_policy.gateway_role_legacy["vault-secrets"]
}
moved {
  from = aws_iam_role_policy.gateway_webhook_secrets_kms
  to   = aws_iam_role_policy.gateway_role_legacy["webhook-secrets-kms"]
}
moved {
  from = aws_iam_role_policy.gateway_cross_account[0]
  to   = aws_iam_role_policy.gateway_role_legacy["cross-account"]
}
moved {
  from = aws_iam_role_policy.gateway_rds_iam_auth[0]
  to   = aws_iam_role_policy.gateway_role_legacy["rds-iam-auth"]
}
moved {
  from = aws_iam_role_policy.gateway_elasticache_iam_auth[0]
  to   = aws_iam_role_policy.gateway_role_legacy["elasticache-iam-auth"]
}
moved {
  from = aws_iam_role_policy.gateway_identity_index
  to   = aws_iam_role_policy.gateway_role_legacy["identity-index"]
}
moved {
  from = aws_iam_role_policy.gateway_ingestion_sqs_publish[0]
  to   = aws_iam_role_policy.gateway_role_legacy["ingestion-sqs-publish"]
}
moved {
  from = aws_iam_role_policy.gateway_user_identity_index
  to   = aws_iam_role_policy.gateway_role_legacy["user-identity-index"]
}
moved {
  from = aws_iam_role_policy.gateway_crew_checkpoints_s3[0]
  to   = aws_iam_role_policy.gateway_role_legacy["crew-checkpoints-s3"]
}
moved {
  from = aws_iam_role_policy.gateway_crew_run_binding[0]
  to   = aws_iam_role_policy.gateway_role_legacy["crew-run-binding"]
}
moved {
  from = aws_iam_role_policy.gateway_user_oauth
  to   = aws_iam_role_policy.gateway_role_legacy["user-oauth"]
}
moved {
  from = aws_iam_role_policy.gateway_agentcore_memory[0]
  to   = aws_iam_role_policy.gateway_role_legacy["agentcore-memory"]
}

# The two managed policies this module used to attach separately. Their
# statements now live in the agents and identity groups; they are retained in
# pass 1 and destroyed in pass 2 under the same superset gate.
moved {
  from = aws_iam_policy.gateway_tools_catalogue_read
  to   = aws_iam_policy.gateway_tools_catalogue_read[0]
}
moved {
  from = aws_iam_role_policy_attachment.gateway_tools_catalogue_read
  to   = aws_iam_role_policy_attachment.gateway_tools_catalogue_read[0]
}
moved {
  from = aws_iam_policy.gateway_cognito_lifecycle
  to   = aws_iam_policy.gateway_cognito_lifecycle[0]
}
moved {
  from = aws_iam_role_policy_attachment.gateway_cognito_lifecycle
  to   = aws_iam_role_policy_attachment.gateway_cognito_lifecycle[0]
}

resource "aws_iam_policy" "gateway_tools_catalogue_read" {
  count = var.gateway_role_policy_cutover_keep_legacy ? 1 : 0

  name = "${local.name_prefix}-gateway-tools-catalogue-read"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:ListGateways"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:GetGateway",
          "bedrock-agentcore:ListGatewayTargets",
          "bedrock-agentcore:GetGatewayTarget"
        ]
        Resource = "arn:aws:bedrock-agentcore:${local.gateway_role_policy_region}:${local.gateway_role_policy_account}:gateway/*"
      }
    ]
  })

  # Keep the live grants until pass 2 verifies the attached replacement. This
  # also preserves any drift for the superset gate to detect rather than hiding
  # it in a pass-1 policy update.
  lifecycle {
    ignore_changes = [policy]
  }
}

resource "aws_iam_role_policy_attachment" "gateway_tools_catalogue_read" {
  count = var.gateway_role_policy_cutover_keep_legacy && contains(var.gateway_role_policy_cutover_managed_attachments, "${local.name_prefix}-gateway-tools-catalogue-read") ? 1 : 0

  role       = local.gateway_service_irsa_role_name
  policy_arn = aws_iam_policy.gateway_tools_catalogue_read[0].arn
}

# The identity group replaces these grants. Existing standalone attachments
# must survive pass 1; accounts without one must not spend another managed-policy
# slot recreating it. The helper captures the live attachment inventory.
resource "aws_iam_policy" "gateway_cognito_lifecycle" {
  count = var.gateway_role_policy_cutover_keep_legacy ? 1 : 0

  name        = "${local.name_prefix}-policy-gateway-cognito-lifecycle"
  description = "Gateway identity, machine client and CLI login lifecycle in its Cognito pool"
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [for statement in local.gateway_role_identity_statements : statement if contains(["CognitoIdentityLifecycle", "CognitoMachineClientLifecycle", "CognitoCliLoginMint"], statement.Sid)]
  })

  lifecycle {
    ignore_changes = [policy]
  }
}

resource "aws_iam_role_policy_attachment" "gateway_cognito_lifecycle" {
  count = var.gateway_role_policy_cutover_keep_legacy && contains(var.gateway_role_policy_cutover_managed_attachments, "${local.name_prefix}-policy-gateway-cognito-lifecycle") ? 1 : 0

  role       = local.gateway_service_irsa_role_name
  policy_arn = aws_iam_policy.gateway_cognito_lifecycle[0].arn
}
