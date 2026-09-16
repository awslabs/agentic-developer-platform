# =============================================================================
# Agent ScaledJob IAM Role (IRSA) — Issue #1204 (synthesis #1200 Section 2)
# =============================================================================
# Scoped permissions for the hosted agent worker pods. Replaces the prior
# multi-inline-policy approach with a single consolidated inline policy
# matching the audit synthesis output.
#
# Permissions:
#   - SQS: receive + delete messages from agent-submit.fifo
#   - Bedrock: invoke models for agent reasoning
#   - Bedrock AgentCore: ephemeral browser sessions (url-analysis skill)
#   - Secrets Manager: read GitHub App keys, tenant credentials
#     (explicitly DENIED on the four customer vault namespaces — #4130)
#   - STS: assume customer AWS roles for operations-persona tasks
#   - Execute API: invoke internal gateway endpoints via SigV4
#   - DynamoDB: update correlation pointers (UpdateItem, not PutItem — #1716)
#   - KMS: decrypt the marker-signing key only (condition-scoped — #4028)
#   - CloudWatch Logs: agent execution + bootstrap logging
#   - S3: beads state + url-analysis evidence + agent-run-logs
#   - Preflight: read-only checks (multiple services)
#
# Issue: #346, #1204, #4028, #4130
# =============================================================================

# -----------------------------------------------------------------------------
# The webhook-events write grant, and why it is conditional (#5028 AC4)
# -----------------------------------------------------------------------------
# This is the one grant in this policy that cannot be scoped correctly by IAM.
# Every agent worker assumes THIS SINGLE ROLE, and the row it needs to write is
# keyed (event_id, arrived_at) — both caller-supplied. So `UpdateItem` on
# `table/adp-*-webhook-events` is unavoidably "any worker may write any run's
# row", including another run's `control_address` and `control_token`, which
# redirects that run's control channel to a listener of the writer's choosing.
# No condition key fixes it: the row key is data, and "the run this pod is
# actually executing" does not exist at the IAM layer.
#
# The fix is therefore to remove the grant, not narrow it. When
# agent_authority_enabled is true the three writers
# (agent-worker-image/lib/invocation_status.py: update_status,
# register_control_endpoint, clear_control_endpoint) route through the gateway's
# /internal/v1/agent/self routes instead, which authenticate the invocation and
# attempt via the run credential + TokenReview-verified pod and derive the row
# key from the protected authority table the worker cannot write. The gateway
# holds the write grant in its place (iam.tf, WebhookEventsSelfWrite).
#
# Still granted when the flag is OFF, which is the default. The worker's gateway
# path deliberately has no DynamoDB fallback, so removing this grant while the
# flag is off would leave nothing writing the row at all and freeze every run's
# status at webhook_received — the #1455 gate failure. Code and permission move
# together, keyed off the same variable, so neither half can ship alone.
#
# correlation-pointers is a separate statement (DynamoDBTableMgmt) and is NOT
# touched: its advisory lineage is not an authority input and its writers have
# not moved. Authority-enabled roles also carry the explicit permissions
# boundary in agent-authority-boundary.tf: omission alone is insufficient when
# AdministratorAccess or another broad policy is attached to the worker role.
locals {
  agent_worker_events_write = var.agent_authority_enabled ? [] : [
    {
      Sid    = "DynamoDBWebhookEventsUpdate"
      Effect = "Allow"
      Action = [
        "dynamodb:UpdateItem"
      ]
      Resource = "arn:aws:dynamodb:us-east-1:*:table/adp-*-webhook-events"
    }
  ]
}

resource "aws_iam_role" "agent_scaledjob" {
  name = "${local.name_prefix}-agent-scaledjob-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # IRSA: agent pods in adp-agents namespace assume this role
        Effect = "Allow"
        Principal = {
          Federated = local.oidc_provider_arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${replace(local.oidc_issuer, "https://", "")}:sub" = "system:serviceaccount:adp-agents:agent-scaledjob-sa"
            "${replace(local.oidc_issuer, "https://", "")}:aud" = "sts.amazonaws.com"
          }
        }
      },
      {
        # KEDA operator chain-assumes this role for SQS queue-depth polling
        Effect = "Allow"
        Principal = {
          AWS = aws_iam_role.keda_operator.arn
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name      = "${local.name_prefix}-agent-scaledjob-role"
    Component = "hosted-agent-worker"
  }
}

# =============================================================================
# Consolidated agent-worker policy (synthesis #1200 Section 2)
# =============================================================================
# Size: ~2.2 KB — well within the inline policy limit.
# =============================================================================

locals {
  agent_worker_scoped_policy = {
    Version = "2012-10-17"
    # concat, so the webhook-events write grant can be dropped entirely rather
    # than narrowed in place (#5028 AC4 — see local.agent_worker_events_write).
    Statement = concat(local.agent_worker_events_write, [
      {
        Sid    = "BedrockModelInvoke"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]
        Resource = [
          "arn:aws:bedrock:*:*:inference-profile/*",
          "arn:aws:bedrock:*::foundation-model/*"
        ]
      },
      {
        # Region restriction prevents a misconfigured skill from spinning up
        # browser sessions in other regions (cost + audit containment).
        Sid    = "BedrockAgentCoreBrowser"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:ConnectBrowserAutomationStream",
          "bedrock-agentcore:GetBrowserSession",
          "bedrock-agentcore:InvokeBrowser",
          "bedrock-agentcore:ListBrowserSessions",
          "bedrock-agentcore:StartBrowserSession",
          "bedrock-agentcore:StopBrowserSession",
          "bedrock-agentcore:UpdateBrowserStream"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "aws:RequestedRegion" = var.aws_region
          }
        }
      },
      {
        # UpdateItem, NOT PutItem (issue #4028). The worker's write_pointer()
        # switched to update_item in #1716 so it only SETs the attributes it
        # owns, leaving webhook-managed fields (e.g. last_triggered_persona)
        # intact — a PutItem would wipe them. PutItem is therefore deliberately
        # NOT granted: it is dead for this role and re-granting it would let the
        # worker reintroduce the #1716 bug.
        # See agent-worker-image/lib/correlation_store.py:77-112.
        Sid    = "DynamoDBTableMgmt"
        Effect = "Allow"
        Action = [
          "dynamodb:UpdateItem"
        ]
        Resource = "arn:aws:dynamodb:us-east-1:*:table/adp-*-correlation-pointers"
      },
      {
        # The webhook-events (and correlation-pointers) tables are encrypted
        # with the customer-managed CMK aws_kms_key.dynamodb. Writing to a
        # CMK-encrypted table requires kms:Decrypt + kms:GenerateDataKey on the
        # key, not just the dynamodb action. Without this, the worker's
        # UpdateItem (lib/invocation_status.update_status) fails with
        # KMS AccessDeniedException, which is swallowed fail-soft, leaving the
        # invocation row frozen at webhook_received (issue #1455 Gate failure).
        # Mirrors the lambda role grant in iam.tf.
        Sid    = "DynamoDBKMSDecrypt"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = aws_kms_key.dynamodb.arn
      },
      {
        Sid    = "ExecuteAPIInvoke"
        Effect = "Allow"
        Action = [
          "execute-api:Invoke"
        ]
        # Two routes, deliberately enumerated rather than collapsed to `/*`:
        #   /agent/*    — the Bedrock proxy the agent calls for chat/model work.
        #   /internal/* — the gateway control plane (GitHub-token gatekeeper
        #                 #4272, credential raw-read / assume-role). Added by
        #                 issue #4343: those calls used to be addressed as
        #                 /agent/internal/... and so were covered by the grant
        #                 above, but that path egresses through the EDGE ALB
        #                 where #4010's deny returns 403, so the clients now
        #                 target /internal/{proxy+} (internal-plane ALB) and
        #                 need this ARN to pass execute-api authz.
        # Keep this list narrow: widening to `.../*` would let the worker SigV4
        # any gateway route, which is privilege creep beyond what it calls.
        Resource = [
          "arn:aws:execute-api:us-east-1:*:*/*/*/agent/*",
          "arn:aws:execute-api:us-east-1:*:*/*/*/internal/*"
        ]
      },
      {
        # Primary agent logging (issue #4221). Scoped to the TF-managed,
        # env-scoped group aws_cloudwatch_log_group.agent_logs (cloudwatch.tf).
        #
        # This previously named /github-ccsdk-agent/logs — a group that no
        # Terraform resource created and that existed in no account, so the
        # worker's entire primary log stream was silently discarded. Keep this
        # Resource list and the log-group resource in lockstep: a grant that
        # names a group nobody writes to is the exact defect that was fixed
        # here, and it fails without any error surfacing anywhere.
        #
        # Neither Node nor Python workers create log groups. Terraform owns
        # the groups; workers create streams and append events only.
        #
        # PutRetentionPolicy is deliberately absent for the same reason as the
        # bootstrap grant: TF owns retention (14 days, this module's
        # convention). See the extended note on the BootstrapLogging statement.
        #
        # Deriving both ARNs from the resource (rather than repeating the name as
        # a literal, as BootstrapLogging must since it wildcards the env) is what
        # makes grant/group drift structurally impossible here. Note the log-group
        # `arn` attribute has the API's trailing ":*" trimmed by the provider, so
        # the bare arn is the group itself and the ":*" form below is the
        # log-streams-within-group ARN that PutLogEvents needs — appending ":*"
        # is correct and does not double up.
        Sid    = "CloudWatchLogGroups"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = [
          aws_cloudwatch_log_group.agent_logs.arn,
          "${aws_cloudwatch_log_group.agent_logs.arn}:*"
        ]
      },
      {
        # Terraform provisions this exact group. The worker only creates streams
        # and appends events; bootstrap_logger.py never creates infrastructure.
        Sid    = "BootstrapLogging"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = [
          aws_cloudwatch_log_group.agent_bootstrap.arn,
          "${aws_cloudwatch_log_group.agent_bootstrap.arn}:*"
        ]
      },
      {
        Sid      = "ProvenanceMetrics"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = { "cloudwatch:namespace" = "ADP/Provenance" }
        }
      },
      {
        Sid    = "Multiple"
        Effect = "Allow"
        Action = [
          "bedrock:ListFoundationModels",
          "codebuild:ListProjects",
          "cognito-idp:ListUserPools",
          "dynamodb:ListTables",
          "ecr:DescribeRepositories",
          "eks:ListClusters",
          "iam:GetUser",
          "iam:ListRoles",
          "s3:ListAllMyBuckets",
          "secretsmanager:ListSecrets"
        ]
        Resource = "*"
      },
      {
        Sid    = "S3Combined"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject"
        ]
        Resource = [
          "arn:aws:s3:::adp-*-agent-beads-state-*/*",
          "arn:aws:s3:::adp-*-agent-run-logs-*/*",
          "arn:aws:s3:::adp-*-url-analysis-evidence-v2-*/*"
        ]
      },
      {
        Sid    = "SecretsManagerOps"
        Effect = "Allow"
        Action = [
          "secretsmanager:DescribeSecret",
          "secretsmanager:GetSecretValue"
        ]
        Resource = "arn:aws:secretsmanager:us-east-1:*:secret:adp/*"
      },
      {
        # Marker-signing key decrypt (issue #4028, for #3178 marker signing).
        # aws_secretsmanager_secret.marker_signing_key is encrypted with the
        # platform webhook-secrets CMK (secrets.tf:77), but the only KMS grant on
        # this role was the DynamoDB key — so GetSecretValue returned
        # AccessDeniedException and marker_signing.py degraded to unsigned
        # markers ("Markers will be unsigned", marker_signing.py:67).
        #
        # SCOPING IS LOAD-BEARING — do NOT relax this to a bare kms:Decrypt on
        # the CMK, and do NOT mirror the Lambda-role statement at iam.tf:172-184
        # (#2567) verbatim. That CMK also encrypts the platform GitHub App
        # private key (adp/<env>/github-app/adp-agent-platform-key, org-wide
        # impersonation), the webhook HMAC secret, and the GitLab webhook secret.
        # SecretsManagerOps above already grants GetSecretValue on secret:adp/*,
        # so the absence of kms:Decrypt is the ONLY control stopping this role
        # from reading those. This role runs semi-trusted, agent-authored code,
        # so an unconditioned grant would be a privilege escalation.
        #
        # #2567 is not a precedent here: that role is the webhook Lambda, which
        # legitimately needs all five secrets and runs no untrusted code. Same
        # statement shape, different threat model.
        #
        # Secrets Manager passes SecretARN in the KMS encryption context on every
        # GetSecretValue, so ViaService + EncryptionContext:SecretARN permits
        # exactly the marker-signing-key read and nothing else on this key.
        Sid    = "MarkerSigningKeyKMSDecrypt"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey"
        ]
        Resource = local.webhook_secrets_kms_key_arn
        Condition = {
          StringEquals = {
            "kms:ViaService"                  = "secretsmanager.${var.aws_region}.amazonaws.com"
            "kms:EncryptionContext:SecretARN" = aws_secretsmanager_secret.marker_signing_key.arn
          }
        }
      },
      {
        # Cross-tenant vault lockout (issue #4130, #4073 finding #4).
        #
        # SecretsManagerOps above grants GetSecretValue + DescribeSecret on
        # secret:adp/*, and Multiple grants ListSecrets on *. The customer vault
        # lives under the SAME adp/ prefix — gateway/src/shared/services/
        # secrets_manager.py:92-100 mints vault secrets as adp/users/<sub>/…,
        # adp/teams/<id>/…, adp/orgs/<id>/… and adp/domain-apps/<app>/<org>/….
        # So without this statement a run triggered by one customer can read
        # every other customer's stored API keys, DB passwords and third-party
        # tokens, and the read is indistinguishable from ordinary worker traffic.
        #
        # This is the SOLE control for that exposure. #4028's CMK scoping does
        # not reach vault secrets: they are created at runtime with the
        # AWS-managed key (secrets_manager.py:186-196), not a platform CMK, so
        # there is no kms:Decrypt grant to withhold. Mirrors the
        # DenyTenantAwsAccess precedent at agent-factory/infra/gateway-main.tf.
        #
        # DENY, NOT A NARROWED ALLOW — deliberate. The worker legitimately reads
        # its own tenant's github-app secret and the tenant is not known at plan
        # time, so a narrowed Allow would either break that read or require
        # runtime policy generation. An explicit Deny always beats any Allow, so
        # this is both simpler and stronger.
        #
        # PATH SHAPE IS LOAD-BEARING — do NOT "normalise" these to
        # adp/${var.environment}/*. Vault paths have NO env segment (compare
        # gateway/infra/main.tf:474-477, which grants the gateway CRUD on
        # exactly these four env-less namespaces). An env-segmented Deny would
        # match nothing at all while reading, in review, as though it closed
        # this hole — the worst possible failure mode for a security control.
        #
        # Both actions are required: a GetSecretValue-only Deny still lets the
        # pod enumerate other tenants' secret names and metadata.
        #
        # Equally, do NOT broaden this to adp/* to "be safe": that would deny
        # the worker's own adp/<env>/tenants/<tenant>/github-app read
        # (agent-worker-image/lib/vault_client.py:35-37) and break GitHub
        # authentication on EVERY agent run. The four namespaces below do not
        # overlap that path.
        Sid    = "DenyTenantVaultSecrets"
        Effect = "Deny"
        Action = [
          "secretsmanager:DescribeSecret",
          "secretsmanager:GetSecretValue"
        ]
        Resource = [
          "arn:aws:secretsmanager:*:${local.account_id}:secret:adp/users/*",
          "arn:aws:secretsmanager:*:${local.account_id}:secret:adp/teams/*",
          "arn:aws:secretsmanager:*:${local.account_id}:secret:adp/orgs/*",
          "arn:aws:secretsmanager:*:${local.account_id}:secret:adp/domain-apps/*"
        ]
      },
      {
        Sid    = "SQSQueueMgmt"
        Effect = "Allow"
        Action = [
          "sqs:ChangeMessageVisibility",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:ReceiveMessage"
        ]
        Resource = "arn:aws:sqs:us-east-1:*:adp-*-agent-submit.fifo"
      },
      {
        # ExternalId condition prevents a compromised pod from assuming arbitrary
        # cross-account roles. Customer-vault roles must be configured to require
        # this ExternalId; without the condition, the agent could assume any role
        # that trusts the agent-worker principal in any account.
        Sid    = "STSIdentityAndAssume"
        Effect = "Allow"
        Action = [
          "sts:AssumeRole",
          "sts:GetCallerIdentity"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "sts:ExternalId" = "${local.name_prefix}-hosted-agent"
          }
        }
      }
    ])
  }
}

resource "aws_iam_role_policy" "agent_scaledjob_permissions" {
  name   = "agent-worker-scoped-permissions"
  role   = aws_iam_role.agent_scaledjob.id
  policy = jsonencode(local.agent_worker_scoped_policy)
}
