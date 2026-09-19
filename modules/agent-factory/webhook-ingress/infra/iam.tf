# =============================================================================
# IAM — Lambda Execution Role
# =============================================================================
# Grants the webhook Lambda: CloudWatch Logs, SQS SendMessage, DDB access,
# and Secrets Manager read for the webhook secret.
# =============================================================================

resource "aws_iam_role" "lambda_execution" {
  name = "${local.name_prefix}-webhook-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })
}

# CloudWatch Logs
resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ENI management, only when the Lambda is VPC-attached. Without this the
# function is created but every invocation fails to initialise, because Lambda
# cannot create the ENI it needs — a failure that looks like a code problem in
# the logs, not a permissions one.
resource "aws_iam_role_policy_attachment" "lambda_vpc_access" {
  count      = length(local.webhook_lambda_subnet_ids) > 0 ? 1 : 0
  role       = aws_iam_role.lambda_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# SQS SendMessage
resource "aws_iam_policy" "lambda_sqs" {
  name        = "${local.name_prefix}-webhook-lambda-sqs"
  description = "Allow webhook Lambda to send messages to agent-submit FIFO queue"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:GetQueueUrl",
          "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.agent_submit.arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_sqs" {
  role       = aws_iam_role.lambda_execution.name
  policy_arn = aws_iam_policy.lambda_sqs.arn
}

# DynamoDB access
resource "aws_iam_policy" "lambda_dynamodb" {
  name        = "${local.name_prefix}-webhook-lambda-ddb"
  description = "Allow webhook Lambda to read/write DynamoDB tables"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:UpdateItem"
        ]
        Resource = [
          aws_dynamodb_table.tenant_registry.arn,
          aws_dynamodb_table.webhook_events.arn,
          "${aws_dynamodb_table.webhook_events.arn}/index/*",
          aws_dynamodb_table.rate_limits.arn,
        ]
      },
      {
        Sid    = "IdentityIndexReadWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:PutItem"
        ]
        Resource = var.identity_index_table_arn != "" ? [var.identity_index_table_arn] : ["arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/adp-${var.environment}-identity-index"]
      },
      {
        Sid    = "UserIdentityIndexRead"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem"
        ]
        Resource = "arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/adp-${var.environment}-user-identity-index"
      },
      {
        Sid    = "CorrelationPointersReadWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem"
        ]
        Resource = aws_dynamodb_table.correlation_pointers.arn
      },
      {
        Sid    = "DynamoDBKMSAccess"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        # Two keys: this module's own KMS (encrypts tenant-registry,
        # webhook-events, rate-limits) plus the gateway's KMS (encrypts
        # identity-index + user-identity-index, which the Lambda READS to
        # resolve installation_id → tenant and sender_id → user). Without
        # the gateway key, every GetItem on those tables returns
        # AccessDeniedException and webhooks 403 with outcome=unknown_installation.
        Resource = [
          aws_kms_key.dynamodb.arn,
          data.aws_kms_alias.gateway_dynamodb.target_key_arn,
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_dynamodb" {
  role       = aws_iam_role.lambda_execution.name
  policy_arn = aws_iam_policy.lambda_dynamodb.arn
}

# Secrets Manager read/write
resource "aws_iam_policy" "lambda_secrets" {
  name        = "${local.name_prefix}-webhook-lambda-secrets"
  description = "Allow webhook Lambda to read webhook secret and manage per-tenant GitHub App secrets"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Sid    = "ReadWebhookSecret"
          Effect = "Allow"
          Action = [
            "secretsmanager:GetSecretValue"
          ]
          Resource = aws_secretsmanager_secret.webhook_secret.arn
        },
        {
          Sid    = "ReadPlatformGitHubAppSecrets"
          Effect = "Allow"
          Action = [
            "secretsmanager:GetSecretValue"
          ]
          Resource = [
            aws_secretsmanager_secret.github_app_id.arn,
            aws_secretsmanager_secret.github_app_key.arn,
          ]
        },
        {
          Sid    = "WritePerTenantGitHubAppSecrets"
          Effect = "Allow"
          Action = [
            "secretsmanager:CreateSecret",
            "secretsmanager:TagResource"
          ]
          Resource = "arn:aws:secretsmanager:${var.aws_region}:${local.account_id}:secret:adp/${var.environment}/tenants/*"
        },
        {
          # Issue #2567: The webhook secret, GitHub App ID, and GitHub App key
          # are encrypted with aws_kms_key.secrets (CMK). Without kms:Decrypt
          # on this key, GetSecretValue returns AccessDeniedException even though
          # the secretsmanager:GetSecretValue permission is granted above.
          Sid    = "SecretsKMSDecrypt"
          Effect = "Allow"
          Action = [
            "kms:Decrypt",
            "kms:DescribeKey"
          ]
          Resource = local.webhook_secrets_kms_key_arn
        }
      ],
      var.internal_api_key_arn != "" ? [
        {
          Sid      = "ReadInternalApiKey"
          Effect   = "Allow"
          Action   = ["secretsmanager:GetSecretValue"]
          Resource = [var.internal_api_key_arn]
        }
      ] : [],
      # Issue #3324: GitLab webhook secret read permission
      var.gitlab_webhook_enabled ? [
        {
          Sid      = "ReadGitLabWebhookSecret"
          Effect   = "Allow"
          Action   = ["secretsmanager:GetSecretValue"]
          Resource = [aws_secretsmanager_secret.gitlab_webhook_secret[0].arn]
        }
      ] : []
    )
  })
}

resource "aws_iam_role_policy_attachment" "lambda_secrets" {
  role       = aws_iam_role.lambda_execution.name
  policy_arn = aws_iam_policy.lambda_secrets.arn
}

# CloudWatch PutMetricData — required by identity_resolver.py to emit
# IdentityResolver.CrossTenantMismatch on installation/user tenant disagreement
# (issue #537 follow-up; flagged by reviewer on PR #539).
#
# Narrow-scoped by namespace via the cloudwatch:namespace condition key so
# the Lambda can only publish under its own namespace, not overwrite others.
resource "aws_iam_policy" "lambda_cloudwatch_metrics" {
  name        = "${local.name_prefix}-webhook-lambda-cloudwatch-metrics"
  description = "Allow webhook Lambda to emit custom CloudWatch metrics under ADP/IdentityResolver and WebhookIngress"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = ["ADP/IdentityResolver", "WebhookIngress"]
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_cloudwatch_metrics" {
  role       = aws_iam_role.lambda_execution.name
  policy_arn = aws_iam_policy.lambda_cloudwatch_metrics.arn
}

# =============================================================================
# IAM — Gateway Activity read path (issue #2754)
# =============================================================================
# The gateway's /api/admin/agent-invocations and /api/me/agent-invocations
# endpoints Query the webhook-events table (incl. tenant-index) and must decrypt
# its SSE-KMS key. Both the table and the key are defined in this stack, so we
# reference them as in-state ARNs (scoped — no table/* or kms:* wildcards).
#
# The gateway role (adp-${var.environment}-role-gateway-service) is created by
# platform/infra (platform/infra/modules/eks/main.tf -> aws_iam_role
# .gateway_service_irsa), which always applies before this stack, so attaching
# an inline policy to it by name is safe.
#
# Single combined policy named to match the platform account's original
# hand-patch (adp-dev-policy-gateway-activity-read): PutRolePolicy is an upsert,
# so this converges on that account without an import. The account's second
# hand-patched policy (-activity-kms) becomes orphaned and is deleted manually
# post-apply (see issue #2754 Deployment section).
resource "aws_iam_role_policy" "gateway_activity_read" {
  name = "adp-${var.environment}-policy-gateway-activity-read"
  role = "adp-${var.environment}-role-gateway-service"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "WebhookEventsRead"
        Effect = "Allow"
        Action = [
          "dynamodb:Query",
          "dynamodb:GetItem",
        ]
        Resource = [
          aws_dynamodb_table.webhook_events.arn,
          "${aws_dynamodb_table.webhook_events.arn}/index/*",
        ]
      },
      {
        Sid    = "WebhookEventsKMSDecrypt"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
        ]
        Resource = [aws_kms_key.dynamodb.arn]
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# Gateway access to the agent authority table (#5028)
# -----------------------------------------------------------------------------
# The gateway is the only reader on the authorization path: src/agentauth/store.py
# loads execution state and grants, and reserves dispatch slots. Attached by
# literal role name for the same reason as gateway_activity_read above — the role
# is created in the platform stack, so this stack cannot reference it as a
# resource, and PutRolePolicy is an upsert.
#
# Write actions are included because two of them ARE authorization enforcement
# rather than provisioning:
#   - UpdateItem backs reserve_dispatch, a conditional atomic ADD. A dispatch
#     ceiling enforced by reading a count and then acting on it is a race: two
#     callers both read 2-of-3 and both dispatch. The gateway must be able to
#     claim the slot in the same operation it checks it.
#   - UpdateItem also backs revoke_grant and set_execution_status, so revocation
#     and cancellation take effect for in-flight credentials that are still
#     cryptographically valid.
# PutItem backs put_execution at dispatch. DeleteItem is deliberately NOT granted:
# nothing in the store deletes, and revocation is a state transition precisely so
# that it stays auditable. Granting delete would make "this authority was revoked"
# and "this authority never existed" indistinguishable after the fact.
#
# No worker role appears here, and no worker statement names this table. That
# absence is the boundary — see the comment on aws_dynamodb_table.agent_authority.
resource "aws_iam_role_policy" "lambda_agent_authority" {
  name = "adp-${var.environment}-policy-ingress-agent-authority"
  role = aws_iam_role.lambda_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "TrustedIngressAuthorityWrites"
      Effect   = "Allow"
      Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:ConditionCheckItem"]
      Resource = [aws_dynamodb_table.agent_authority.arn]
    }]
  })
}

resource "aws_iam_role_policy" "gateway_agent_authority" {
  name = "adp-${var.environment}-policy-gateway-agent-authority"
  role = "adp-${var.environment}-role-gateway-service"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AgentAuthorityReadWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:ConditionCheckItem",
        ]
        # No /index/* entry: the store performs single-item GetItem calls only.
        # There is no Query grant either, deliberately — a Query could match more
        # than one item, and every authorization lookup here must resolve to the
        # exact principal or invocation the verified credential named.
        Resource = [aws_dynamodb_table.agent_authority.arn]
      },
      {
        # The table is encrypted with the customer-managed CMK, so the dynamodb
        # action alone is not sufficient: without these the store's reads fail
        # with a KMS AccessDeniedException. Since the policy fails closed on a
        # store error, a missing KMS grant would refuse every delegated action
        # rather than degrade quietly — noisy, but it would look like an
        # authorization bug rather than a missing permission.
        Sid    = "AgentAuthorityKMS"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
        ]
        Resource = [aws_kms_key.dynamodb.arn]
      },
    ]
  })
}

# -----------------------------------------------------------------------------
# Gateway writes the worker's own status / control registration (#5028 AC4)
# -----------------------------------------------------------------------------
# The permission half of removing the worker's table-wide webhook-events write.
# The worker held `dynamodb:UpdateItem` on `table/adp-*-webhook-events` with no
# key or attribute condition, on a role EVERY agent worker shares, while the row
# key (event_id, arrived_at) is caller-supplied — so any run could rewrite
# another run's `control_address`/`control_token` and take over its control
# channel. See the note on local.agent_worker_events_write in scaledjob-iam.tf
# for why no condition key can fix that in place.
#
# It moves here because the gateway is the only place the write can be bounded:
# src/agentauth/registration.py verifies the run credential and the presenting
# pod, derives the row key from the protected authority table the worker cannot
# write, and conditions the update on the live attempt. The worker sends field
# values and no row key at all.
#
# Deliberately NOT gated on var.agent_authority_enabled, unlike the worker-side
# grant it replaces. The gateway runs platform code rather than agent-authored
# code, already holds Query/GetItem on this table for the Activity views, and
# owns the authority table that decides these writes — so this adds no authority
# an attacker could reach while the flag is off, and keeping it unconditional
# means enabling the flag cannot half-apply into a worker that authenticates
# fine and then 503s on every status write.
#
# GetItem rather than Query is load-bearing: the write must land on the ONE row
# the verified credential names, and a Query can match a second row planted under
# the same event_id. PutItem is not granted — the row is created by the ingress
# Lambda and this path only ever SETs fields on an existing one, so PutItem could
# only serve to fabricate a run. DeleteItem is not granted for the same reason it
# is withheld on the authority table: a status history that can vanish is not an
# audit trail. No /index/* — nothing on this path reads a GSI.
#
# Separate policy rather than another statement in gateway_activity_read: that
# one is the read path for the Activity views and is named for it. A write grant
# buried in a policy called "-activity-read" is the kind of thing a reviewer
# scanning names would miss.
resource "aws_iam_role_policy" "gateway_agent_self_write" {
  name = "adp-${var.environment}-policy-gateway-agent-self-write"
  role = "adp-${var.environment}-role-gateway-service"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "WebhookEventsSelfWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:UpdateItem",
        ]
        Resource = [aws_dynamodb_table.webhook_events.arn]
      },
      {
        # Same CMK as the authority table. Without it every write fails with a KMS
        # AccessDeniedException, which the route surfaces as a 503 — retryable, so
        # a missing grant here would look like a gateway outage rather than a
        # permission gap.
        Sid    = "WebhookEventsSelfWriteKMS"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
        ]
        Resource = [aws_kms_key.dynamodb.arn]
      },
    ]
  })
}

# Human service-policy approval compares the registered service row atomically
# with its protected authority write. It cannot approve a different tenant/repo
# while a concurrent identity-registration edit changes that mapping.
resource "aws_iam_role_policy" "gateway_service_approval_read" {
  name = "adp-${var.environment}-policy-gateway-service-approval"
  role = "adp-${var.environment}-role-gateway-service"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:GetItem", "dynamodb:ConditionCheckItem"]
      Resource = var.identity_index_table_arn != "" ? [var.identity_index_table_arn] : ["arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/adp-${var.environment}-identity-index"]
    }]
  })
}
