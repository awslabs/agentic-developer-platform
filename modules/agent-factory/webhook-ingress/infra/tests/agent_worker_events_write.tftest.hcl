# Issue #5028 AC4: the worker's table-wide webhook-events write grant is removed
# when delegated authority is on, and kept when it is off.
#
# Why this needs a test rather than a code comment. The grant is the vulnerability
# AC4 exists to close: every agent worker assumes ONE shared role, the row key
# (event_id, arrived_at) is caller-supplied, and the grant carries no key or
# attribute condition — so any run can rewrite another run's control_address and
# control_token and take over its control channel. But it cannot simply be
# deleted, because with the flag off the direct DynamoDB write is the only writer
# and the gateway path has no fallback; deleting it unconditionally freezes every
# run at webhook_received (the #1455 gate failure).
#
# Both halves are therefore load-bearing, and each fails in a way the other's test
# would not catch:
#   - grant still present with the flag ON  -> the vulnerability is not closed,
#     and everything still works, so nothing surfaces it.
#   - grant missing with the flag OFF       -> every hosted run's status freezes.
# A `concat` on a jsonencode'd Statement list is easy to "simplify" back into an
# unconditional entry during a later refactor, and neither failure mode produces a
# plan error.
#
# Plan-only with mocked providers: this proves what the configuration DECLARES for
# each value of the flag. It cannot prove the deployed role matches — that is the
# post-apply check in the issue's Validation section.

mock_provider "aws" {}
mock_provider "kubernetes" {}
mock_provider "helm" {}
mock_provider "tls" {}

variables {
  # Enabling authority requires an approved immutable worker digest; the variable
  # validation rejects tags, so a plausible digest is supplied rather than "".
  agent_authority_worker_image_digests = ["sha256:0000000000000000000000000000000000000000000000000000000000000000"]
  agent_image                          = "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:0000000000000000000000000000000000000000000000000000000000000000"
}

override_data {
  target          = data.aws_ssm_parameter.gateway_apigw_invoke_url
  override_during = plan
  values          = { value = "https://example123.execute-api.us-east-1.amazonaws.com/dev" }
}

override_resource {
  target          = aws_cloudwatch_log_group.agent_bootstrap
  override_during = plan
  values          = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:/adp/dev/agent-factory/bootstrap" }
}

override_resource {
  target          = aws_sqs_queue.agent_submit
  override_during = plan
  values          = { arn = "arn:aws:sqs:us-east-1:123456789012:adp-dev-agent-submit.fifo" }
}

override_resource {
  target          = aws_s3_bucket.agent_run_logs
  override_during = plan
  values          = { arn = "arn:aws:s3:::adp-dev-agent-run-logs-123456789012" }
}

# -----------------------------------------------------------------------------
# Overrides: why they are here and why every one is `override_during = plan`
# -----------------------------------------------------------------------------
# The assertions read the RENDERED policy JSON, which interpolates ARNs from
# resources and data sources in this stack. Under a mock provider those are
# unknown until apply, so a plan-only run cannot evaluate the condition at all.
# Pinning them makes the policy string known during plan.
#
# `command = apply` would be the other way to resolve them, but it is worse here:
# the mock provider invents non-ARN strings for every computed attribute, and
# unrelated resources (aws_lambda_function.gitlab_webhook,
# aws_iam_role_policy_attachment.*) validate their inputs as ARNs and fail — so an
# apply run fails for reasons that have nothing to do with the permission under
# test. Plan + pinned ARNs keeps the failure surface to the policy itself.
#
# File-level so both runs share them: the two runs differ ONLY in the flag, which
# is the variable whose effect is being measured.
override_data {
  target          = data.aws_caller_identity.current
  override_during = plan
  values = {
    account_id = "123456789012"
  }
}

override_data {
  target          = data.aws_region.current
  override_during = plan
  values = {
    name = "us-east-1"
  }
}

# KMS validates its own policy document as JSON, and the OIDC locals index into
# the cluster's identity list — neither relates to IAM; these two just let the
# plan get far enough to render the policies.
override_data {
  target          = data.aws_iam_policy_document.dynamodb_kms
  override_during = plan
  values = {
    json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
  }
}

override_data {
  target          = data.aws_iam_policy_document.cloudwatch_kms
  override_during = plan
  values = {
    json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
  }
}

override_data {
  target          = data.aws_eks_cluster.main
  override_during = plan
  values = {
    identity = [{
      oidc = [{
        issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E"
      }]
    }]
    certificate_authority = [{
      data = "TFNUQVJUQ0VSVElGSUNBVEU="
    }]
  }
}

override_data {
  target          = data.aws_kms_alias.secrets
  override_during = plan
  values = {
    target_key_arn = "arn:aws:kms:us-east-1:123456789012:key/22222222-2222-2222-2222-222222222222"
  }
}

# The four ARNs the two policies under test actually interpolate.
override_resource {
  target          = aws_kms_key.dynamodb
  override_during = plan
  values = {
    arn = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111"
  }
}

override_resource {
  target          = aws_dynamodb_table.webhook_events
  override_during = plan
  values = {
    arn = "arn:aws:dynamodb:us-east-1:123456789012:table/adp-dev-webhook-events"
  }
}

override_resource {
  target          = aws_cloudwatch_log_group.agent_logs
  override_during = plan
  values = {
    arn = "arn:aws:logs:us-east-1:123456789012:log-group:/adp/dev/agent-factory/agent"
  }
}

override_resource {
  target          = aws_secretsmanager_secret.marker_signing_key
  override_during = plan
  values = {
    arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/dev/marker-signing-key"
  }
}

override_resource {
  target          = aws_iam_policy.agent_authority_boundary
  override_during = plan
  values          = { arn = "arn:aws:iam::123456789012:policy/adp-dev-agent-authority-boundary" }
}

override_resource {
  target          = aws_iam_role.keda_operator
  override_during = plan
  values = {
    arn = "arn:aws:iam::123456789012:role/adp-dev-keda-operator-role"
  }
}

run "grant_is_present_while_authority_is_off" {
  command = plan

  variables {
    agent_authority_enabled = false
  }

  assert {
    condition     = aws_iam_role.agent_scaledjob.permissions_boundary == null
    error_message = "Flag-off workers must retain the existing bootstrap permissions."
  }

  # Off is the DEFAULT, so this is the shipping configuration. The worker still
  # writes its own row directly and must be able to.
  assert {
    condition = length([
      for statement in jsondecode(aws_iam_role_policy.agent_scaledjob_permissions.policy).Statement :
      statement if statement.Sid == "DynamoDBWebhookEventsUpdate"
    ]) == 1
    error_message = "With agent_authority_enabled = false the worker MUST keep DynamoDBWebhookEventsUpdate: the gateway write path is inactive and has no DynamoDB fallback, so removing it freezes every run's status at webhook_received (#1455)."
  }
}

run "grant_is_gone_once_authority_is_on" {
  command = plan

  variables {
    agent_authority_enabled                = true
    agent_worker_admission_paused          = true
    agent_authority_runtime_ready          = true
    agent_authority_legacy_workers_drained = true
  }

  assert {
    condition     = aws_iam_role.agent_authority_worker[0].permissions_boundary == aws_iam_policy.agent_authority_boundary[0].arn
    error_message = "Omitting an inline Allow cannot restrict AdministratorAccess: the boundary must be attached."
  }

  assert {
    condition     = length(jsonencode(jsondecode(aws_iam_policy.agent_authority_boundary[0].policy))) <= 6144
    error_message = "The boundary must fit AWS's managed policy size limit."
  }

  assert {
    condition = alltrue([
      for action in ["sts:AssumeRole", "iam:DeleteRolePermissionsBoundary", "eks:CreateAccessEntry", "lambda:InvokeFunction", "ssm:GetParameter", "sqs:SendMessage", "dynamodb:PutItem"] :
      !contains(one([for statement in jsondecode(aws_iam_policy.agent_authority_boundary[0].policy).Statement : statement.NotAction if statement.Sid == "DenyUnlistedActions"]), action)
    ])
    error_message = "Privilege escalation and alternate authority publishers must be explicitly denied despite AdministratorAccess."
  }

  assert {
    condition     = kubernetes_service_account.agent_authority_worker[0].metadata[0].name != kubernetes_service_account.agent_scaledjob_sa.metadata[0].name
    error_message = "Authority pods need a distinct service account: reusing the old subject lets them assume the legacy admin role via web identity."
  }

  assert {
    condition     = jsondecode(aws_iam_role.agent_authority_worker[0].assume_role_policy).Statement[0].Condition.StringEquals["${replace(local.oidc_issuer, "https://", "")}:sub"] == "system:serviceaccount:adp-agents:agent-authority-worker-sa"
    error_message = "Only the dedicated service account may obtain the bounded worker role."
  }

  # The whole point of AC4. Asserted on the rendered policy JSON rather than on
  # the local, so a statement re-added anywhere in the list still fails.
  assert {
    condition = length([
      for statement in jsondecode(aws_iam_role_policy.agent_scaledjob_permissions.policy).Statement :
      statement if statement.Sid == "DynamoDBWebhookEventsUpdate"
    ]) == 0
    error_message = "With agent_authority_enabled = true the worker must hold NO webhook-events write grant. It is a shared role and the row key is caller-supplied, so any grant here lets one run rewrite another run's control_address/control_token."
  }

  # Sid-independent: a re-grant under a different Sid, or folded into another
  # statement's Action/Resource list, is the same hole.
  assert {
    condition = length([
      for statement in jsondecode(aws_iam_role_policy.agent_scaledjob_permissions.policy).Statement :
      statement if statement.Effect == "Allow" &&
      length([for action in tolist(statement.Action) : action if can(regex("^dynamodb:(Update|Put|Delete|BatchWrite)", action))]) > 0 &&
      length([for resource in flatten([statement.Resource]) : resource if can(regex("webhook-events", resource))]) > 0
    ]) == 0
    error_message = "No statement on the worker role may allow a DynamoDB write action against webhook-events, under any Sid. Renaming the Sid or folding the action into a neighbouring statement reopens the same cross-run control-channel hijack."
  }

  # The correlation-pointers grant is a DIFFERENT table with different writers
  # that have NOT migrated. Removing the grant above by omission (rather than by
  # an explicit Deny, which would match both tables under an `adp-*` pattern) is
  # what keeps this working.
  assert {
    condition = length([
      for statement in jsondecode(aws_iam_role_policy.agent_scaledjob_permissions.policy).Statement :
      statement if statement.Sid == "DynamoDBTableMgmt"
    ]) == 1
    error_message = "The correlation-pointers write grant must survive: its writers (lib/correlation_store.py) did not migrate, and its advisory lineage is not an authority input."
  }

  # The permission did not vanish, it moved. If it had vanished, every status
  # write would 503 with the flag on — a total status outage that looks like a
  # gateway fault.
  assert {
    condition = length([
      for statement in jsondecode(aws_iam_role_policy.gateway_agent_self_write.policy).Statement :
      statement if statement.Sid == "WebhookEventsSelfWrite" &&
      length([for action in statement.Action : action if action == "dynamodb:UpdateItem"]) > 0
    ]) == 1
    error_message = "The gateway must hold the webhook-events UpdateItem the worker gave up; it is the only place the write can be bounded to the caller's own run."
  }

  # GetItem, not Query. The row key comes from protected state, and the read must
  # resolve the ONE row the verified credential names — a Query can match a second
  # row planted under the same event_id.
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.gateway_agent_self_write.policy).Statement :
      alltrue([for action in statement.Action : !can(regex("^dynamodb:(Query|Scan|PutItem|DeleteItem)$", action))])
    ])
    error_message = "The gateway self-write policy must grant neither Query/Scan (a match set is not an exact row) nor PutItem/DeleteItem (the row is created by the ingress Lambda, and a deletable status history is not an audit trail)."
  }
}
