# A shared worker role may also carry AdministratorAccess (#1619). Removing one
# inline Allow cannot restrict it. When authority is enabled, intersect EVERY
# identity policy with this boundary; reject privilege escalation and sensitive
# resource access explicitly, including resource-policy grants to role sessions.
# Flag-off deployments retain their existing permissions and bootstrap path.
locals {
  agent_worker_role_arn = var.agent_authority_enabled ? aws_iam_role.agent_authority_worker[0].arn : aws_iam_role.agent_scaledjob.arn
  agent_worker_sa_name  = var.agent_authority_enabled ? kubernetes_service_account.agent_authority_worker[0].metadata[0].name : kubernetes_service_account.agent_scaledjob_sa.metadata[0].name
  # Issue #5663 (A09): the exact attribute set the agent-worker role may write on
  # adp-*-correlation-pointers. Enumerated from the real writers — see the long note
  # on the "CorrelationUpdates" statement below for the per-file derivation and for
  # why the (unread) `latest_*` names must stay in the list.
  #
  # The three names that are deliberately ABSENT are the point of the whole list:
  # root_human_id, is_human_rooted, chain_depth. #4129 removed them from the Python
  # writer's signature; this makes the credential itself unable to set them, so the
  # property survives a future code change or a compromised pod. The absence is
  # asserted by tests/test_correlation_pointer_attribute_boundary_5663.py, which
  # fails if any of the three is ever added here.
  correlation_pointer_worker_attributes = [
    # Primary key (dynamodb:Attributes must include the key being addressed).
    "channel_key",
    # Python writer — agent-worker-image/lib/correlation_store.py
    "correlation_id",
    "updated_at",
    "expires_at",
    "triggering_invocation_id",
    "last_triggered_persona",
    # Node writer — agent/src/lib/correlationStore.ts (no consumer reads these)
    "latest_correlation_id",
    "latest_root_human_id",
    "latest_is_human_rooted",
  ]
  agent_authority_api_resources = concat([
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/*/agent/*",
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/*/internal/v1/agent/*",
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/POST/internal/v1/github-installation-token",
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/POST/internal/v1/credential-assume-role",
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/POST/internal/v1/worker-task-credentials",
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/POST/internal/v1/credential-raw-read",
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/POST/internal/v1/proxy-request",
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/POST/internal/v1/credential-materialize",
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/GET/internal/v1/user-credentials",
    "arn:aws:execute-api:${var.aws_region}:${local.account_id}:*/*/POST/internal/v1/provenance*",
  ], var.task_tool_invoke_resources)
  # Fixed queue-consumer permissions are scoped to this deployment input queue.
  # Run authorization and archives continue to use the authenticated gateway.
  agent_authority_boundary_allow = concat([
    for statement in local.agent_worker_scoped_policy.Statement : statement
    if contains([
      "CloudWatchLogGroups", "BootstrapLogging", "ProvenanceMetrics"
    ], statement.Sid)
    ], module.cyber.worker_browser_permissions, [
    {
      Sid      = "InputQueueConsumer"
      Effect   = "Allow"
      Action   = ["sqs:ReceiveMessage", "sqs:ChangeMessageVisibility", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
      Resource = aws_sqs_queue.agent_submit.arn
    },
    {
      # Issue #5663 (A09). The grant was `UpdateItem` on the whole table with no
      # Condition, so the worker could set ANY attribute on ANY channel_key —
      # including `root_human_id` / `is_human_rooted` / `chain_depth`, the three the
      # #4129 code change stopped it from SENDING. Code that no longer writes a field
      # is not the same as a credential that cannot.
      #
      # dynamodb:Attributes + "SpecificAttributes" is the enforcement that makes the
      # removal durable: DynamoDB refuses the whole request when the UpdateExpression
      # names an attribute outside this list, so the three authority fields become
      # unwritable by this credential regardless of what the pod's code does.
      #
      # THE LIST IS DERIVED FROM THE REAL WRITERS, not from what the policy wishes
      # they sent. Verified against every worker-role write path in the repo:
      #
      #   agent-worker-image/lib/correlation_store.py:98-120 (the only Python writer;
      #   entrypoint.py and lib/seed_trigger_pointer.py both funnel through it)
      #     -> channel_key (the key), correlation_id, updated_at, expires_at,
      #        triggering_invocation_id (conditional), last_triggered_persona
      #        (conditional)
      #
      #   agent/src/lib/correlationStore.ts:40-51 (the Node writer)
      #     -> channel_key (the key), latest_correlation_id, latest_root_human_id,
      #        latest_is_human_rooted, updated_at, expires_at
      #
      # The `latest_*` names are included deliberately, and they are NOT a widening:
      # nothing reads them. The Lambda's reader
      # (lambda/common/correlation_store.py:119-124) reads `correlation_id` /
      # `root_human_id` / `is_human_rooted`, so `latest_root_human_id` lands in a
      # column no consumer consults and confers no authority. Excluding them would
      # make this policy break a live writer — a fail-soft one whose
      # AccessDeniedException is swallowed by `catch` at correlationStore.ts:52, i.e.
      # a SILENT breakage. Renaming the TS writer's attributes (or applying #4129 to
      # it, which was never done) is the right follow-up and is out of scope here;
      # this boundary must match the writers that exist today.
      #
      # ORDERING: this is a source-only proposal and it is safe in either order,
      # because it removes only attributes no current writer sends. It must NOT be
      # applied live before the TS writer is reconciled if that reconciliation
      # renames anything in this list — a boundary narrower than its writers fails
      # closed and silently.
      Sid      = "CorrelationUpdates"
      Effect   = "Allow"
      Action   = ["dynamodb:UpdateItem"]
      Resource = "arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/${local.name_prefix}-correlation-pointers"
      #
      # Only `dynamodb:Attributes` is asserted here. `dynamodb:Select` is NOT included
      # even though it appears in many fine-grained-access examples: it is evaluated
      # for Query and Scan only, so on an UpdateItem-only grant it would be an
      # inert clause that reads like a second control. `dynamodb:ReturnValues` is
      # likewise omitted — the residual it would cover is read-back of the row the
      # worker may already write to, and neither writer sets ReturnValues (both
      # default to NONE), so asserting it would constrain nothing that exists.
      Condition = {
        "ForAllValues:StringEquals" = {
          "dynamodb:Attributes" = local.correlation_pointer_worker_attributes
        }
      }
    },
    {
      Sid      = "CorrelationEncryption"
      Effect   = "Allow"
      Action   = ["kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"]
      Resource = aws_kms_key.dynamodb.arn
      Condition = {
        StringEquals = { "kms:ViaService" = "dynamodb.${var.aws_region}.amazonaws.com" }
      }
    },
    {
      Sid      = "AuthenticatedGateway"
      Effect   = "Allow"
      Action   = ["execute-api:Invoke"]
      Resource = local.agent_authority_api_resources
    },
    {
      Sid      = "Identity"
      Effect   = "Allow"
      Action   = ["sts:GetCallerIdentity"]
      Resource = "*"
    }
  ])
  agent_authority_boundary = {
    Version = "2012-10-17"
    Statement = concat(local.agent_authority_boundary_allow, [
      {
        # Protected model requests must cross the gateway's current policy and
        # shared spend checks. Explicit denial also covers resource-policy grants
        # to a role session and future additions to the inherited Allow list.
        Sid      = "DenyDirectModelInvocation"
        Effect   = "Deny"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:StartAsyncInvoke"]
        Resource = "*"
      },
      {
        # Blocks IAM/boundary changes, STS role chaining/federation, EKS access
        # entries, compute/job mutation, webhook invocation, SSM and state reads.
        Sid       = "DenyUnlistedActions"
        Effect    = "Deny"
        NotAction = distinct(flatten([for statement in local.agent_authority_boundary_allow : statement.Action]))
        Resource  = "*"
      },
      {
        Sid         = "DenyAuthorityData"
        Effect      = "Deny"
        Action      = ["dynamodb:*"]
        NotResource = "arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/${local.name_prefix}-correlation-pointers"
      },
      {
        # Do not let a shared-key, App-private-key, platform-admin credential or
        # tenant-vault read restore privileges through another transport.
        Sid      = "DenyAllSecrets"
        Effect   = "Deny"
        Action   = ["secretsmanager:*"]
        Resource = "*"
      },
      {
        Sid      = "DenyDirectArtifacts"
        Effect   = "Deny"
        Action   = ["s3:*"]
        Resource = "*"
      },
      {
        Sid         = "DenyOtherQueues"
        Effect      = "Deny"
        Action      = ["sqs:*"]
        NotResource = aws_sqs_queue.agent_submit.arn
      },
      {
        Sid         = "DenyOtherEncryptionKeys"
        Effect      = "Deny"
        Action      = ["kms:*"]
        NotResource = aws_kms_key.dynamodb.arn
      },
      {
        Sid      = "DenyDirectKMS"
        Effect   = "Deny"
        Action   = ["kms:*"]
        Resource = "*"
        Condition = {
          StringNotEquals = { "kms:ViaService" = "dynamodb.${var.aws_region}.amazonaws.com" }
        }
      },
      {
        Sid         = "DenyOtherGatewayRoutes"
        Effect      = "Deny"
        Action      = ["execute-api:Invoke"]
        NotResource = local.agent_authority_api_resources
      }
    ])
  }
}

resource "aws_iam_policy" "agent_authority_boundary" {
  count       = local.agent_authority_provisioned ? 1 : 0
  name        = "${local.name_prefix}-agent-authority-boundary"
  description = "Maximum permissions for workers using verified delegated authority"
  policy      = jsonencode(local.agent_authority_boundary)
}

# The legacy role can have an EKS ClusterAdmin access entry. IAM boundaries do
# not constrain Kubernetes authorization. Use a new role AND a distinct subject:
# reusing the old SA would let its projected token assume the legacy role with
# the unsigned AssumeRoleWithWebIdentity API, bypassing the new boundary.
resource "aws_iam_role" "agent_authority_worker" {
  count                = local.agent_authority_provisioned ? 1 : 0
  name                 = "${local.name_prefix}-agent-authority-worker-role"
  permissions_boundary = aws_iam_policy.agent_authority_boundary[0].arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Federated = local.oidc_provider_arn }
        Action    = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${replace(local.oidc_issuer, "https://", "")}:sub" = "system:serviceaccount:adp-agents:agent-authority-worker-sa"
            "${replace(local.oidc_issuer, "https://", "")}:aud" = "sts.amazonaws.com"
          }
        }
      },
      { Effect = "Allow", Principal = { AWS = aws_iam_role.keda_operator.arn }, Action = "sts:AssumeRole" }
    ]
  })
  tags = { Component = "hosted-agent-worker-authority" }
}

resource "aws_iam_role_policy" "agent_authority_worker" {
  count  = local.agent_authority_provisioned ? 1 : 0
  name   = "agent-worker-scoped-permissions"
  role   = aws_iam_role.agent_authority_worker[0].id
  policy = jsonencode(local.agent_authority_boundary)
}

resource "kubernetes_service_account" "agent_authority_worker" {
  count = local.agent_authority_provisioned ? 1 : 0
  metadata {
    name        = local.agent_authority_pod.serviceAccountName
    namespace   = kubernetes_namespace.adp_agents.metadata[0].name
    annotations = { "eks.amazonaws.com/role-arn" = aws_iam_role.agent_authority_worker[0].arn }
  }
}

data "aws_ssm_parameter" "agent_authority_registry" {
  count = local.agent_authority_provisioned ? 1 : 0
  name  = "/adp/${var.environment}/gateway/agent-registry-table"
}

resource "aws_dynamodb_table_item" "agent_authority_worker" {
  count      = local.agent_authority_provisioned ? 1 : 0
  table_name = data.aws_ssm_parameter.agent_authority_registry[0].value
  hash_key   = "agent_id"
  item = jsonencode({
    agent_id              = { S = "authority-worker" }
    role_arn              = { S = aws_iam_role.agent_authority_worker[0].arn }
    agent_name            = { S = "authority-worker" }
    org_id                = { S = "__platform__" }
    team_id               = { S = "__agents__" }
    owner                 = { S = "platform" }
    scope                 = { S = "internal" }
    requires_run_identity = { BOOL = true }
    status                = { S = "active" }
    allowed_models        = { SS = ["*"] }
    # Protected adp-cred producers: client.py list/proxy/materialize/raw-read,
    # assume.py customer role delivery, task_credentials.py SDK source session.
    # These are transport capabilities; canonical run/user/tenant and accepted
    # execution-policy checks remain mandatory at the gateway. Legacy seeds are
    # deliberately unchanged. See docs/security/s12-credential-capabilities.md.
    credential_scopes = { SS = [
      "credential:list", "credential:proxy", "credential:assume-role",
      "credential:task-session", "credential:raw-read", "credential:materialize"
    ] }
    budget_config_id = { S = "" }
  })
}
