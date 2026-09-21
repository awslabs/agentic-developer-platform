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
  values          = { arn = "arn:aws:sqs:us-east-1:123456789012:adp-dev-agent-submit.fifo", url = "https://sqs.us-east-1.amazonaws.com/123456789012/adp-dev-agent-submit.fifo" }
}

override_resource {
  target          = aws_s3_bucket.agent_run_logs
  override_during = plan
  values          = { arn = "arn:aws:s3:::adp-dev-agent-run-logs-123456789012", bucket = "adp-dev-agent-run-logs-123456789012" }
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
    arn = "arn:aws:eks:us-east-1:123456789012:cluster/adp-dev-eks-cluster"
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
    id  = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/dev/marker-signing-key"
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


run "prepare_without_activating_workers" {
  command = plan
  variables {
    agent_authority_enabled              = false
    agent_authority_worker_image_digests = []
  }
  assert {
    condition     = local.agent_worker_pause_annotation == "" && !contains(keys(kubernetes_config_map.worker_gateway[0].data), "AGENT_DISPATCH_QUEUE_URL")
    error_message = "Preparation must neither pause KEDA nor enable a previously unwired dispatch queue."
  }
  assert {
    condition     = length(aws_iam_role.agent_authority_worker) == 1 && length(kubernetes_secret.agent_authority) == 1
    error_message = "Preparation must provision the bounded role and signing resources."
  }
  assert {
    condition     = local.agent_worker_sa_name == "agent-scaledjob-sa" && kubernetes_config_map.worker_gateway[0].data.AGENT_AUTHORITY_ENABLED == "false"
    error_message = "Preparation must preserve legacy launches and gateway authentication."
  }
  assert {
    condition     = aws_lambda_function.github_webhook.environment[0].variables.ADP_WORK_CLAIMS_ENABLED == "false"
    error_message = "Preparation must not enable producer admission."
  }
  assert {
    condition     = length(aws_iam_role_policy_attachments_exclusive.legacy_worker) == 0 && aws_iam_role.agent_scaledjob.permissions_boundary == null
    error_message = "Preparation must not retire permissions of existing workers."
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_role.agent_scaledjob.assume_role_policy).Statement : s if try(s.Sid, "") == "GatewayCustomerTaskSource"]) == 0
    error_message = "Preparation must not enable customer task-source assumption."
  }
}

run "preparation_is_environment_scoped" {
  command = plan
  variables {
    environment = "staging"
    aws_region  = "eu-west-1"
  }
  override_data {
    target          = data.aws_ssm_parameter.gateway_apigw_invoke_url
    override_during = plan
    values          = { value = "https://staging123.execute-api.eu-west-1.amazonaws.com/staging" }
  }
  assert {
    condition     = aws_iam_role.agent_authority_worker[0].name == "adp-staging-agent-authority-worker-role" && local.eks_cluster_name == "adp-staging-eks-cluster"
    error_message = "Neither the role nor the cluster may silently target dev."
  }
  assert {
    condition     = nonsensitive(aws_iam_role_policy.lambda_work_claim_admission[0].policy) == jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["execute-api:Invoke"], Resource = ["arn:aws:execute-api:eu-west-1:123456789012:staging123/staging/POST/internal/v1/agent/work/admit"] }] })
    error_message = "Producer IAM must target only the selected environment API, stage and region."
  }
}

run "activation_refuses_missing_release_acceptance" {
  command = plan
  variables { agent_authority_enabled = true }
  expect_failures = [terraform_data.worker_security_rollout]
}

run "activation_refuses_mutable_worker_image" {
  command = plan
  variables {
    agent_authority_enabled                = true
    agent_authority_runtime_ready          = true
    agent_authority_legacy_workers_drained = true
    agent_image                            = "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime:latest"
  }
  expect_failures = [terraform_data.worker_security_rollout]
}

run "preparation_refuses_source_activation" {
  command = plan
  variables { agent_task_source_isolation_confirmed = true }
  expect_failures = [terraform_data.worker_security_rollout]
}

run "retirement_refuses_undrained_legacy_workers" {
  command = plan
  variables { agent_legacy_worker_admin_retired = true }
  expect_failures = [aws_iam_role_policy_attachments_exclusive.legacy_worker]
}

run "activated_worker_uses_protected_identity" {
  command = plan
  variables {
    agent_authority_enabled                = true
    agent_authority_runtime_ready          = true
    agent_authority_legacy_workers_drained = true
    agent_worker_admission_paused          = true
  }
  assert {
    condition     = strcontains(local.agent_worker_pause_annotation, "autoscaling.keda.sh/paused")
    error_message = "The staged activation must keep admissions paused."
  }
  assert {
    condition     = local.agent_worker_sa_name == "agent-authority-worker-sa" && kubernetes_config_map.worker_gateway[0].data.AGENT_AUTHORITY_ENABLED == "true"
    error_message = "The verified activation must configure both worker and gateway authority."
  }
  assert {
    condition     = aws_lambda_function.github_webhook.environment[0].variables.ADP_WORK_CLAIMS_ENABLED == "true"
    error_message = "Protected producers must use work admission."
  }
}

run "retired_source_keeps_only_customer_sts_authority" {
  command = plan
  variables {
    agent_authority_enabled                = true
    agent_authority_runtime_ready          = true
    agent_authority_legacy_workers_drained = true
    agent_task_source_isolation_confirmed  = true
    agent_legacy_worker_admin_retired      = true
  }
  assert {
    condition     = length(aws_iam_role_policy_attachments_exclusive.legacy_worker[0].policy_arns) == 0
    error_message = "Terraform must remove out-of-band administrator attachments."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.agent_scaledjob_permissions.policy).Statement[1].NotAction == ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity", "sts:GetCallerIdentity"]
    error_message = "The retired source role may not regain platform operations."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.agent_scaledjob_permissions.policy).Statement[2].Resource == "arn:aws:iam::123456789012:role/*"
    error_message = "The retired source must deny chaining into the platform account."
  }
}


# A fixture key only. Tests never read AWS; activation must use the existing
# webhook key, not Terraform's public bootstrap placeholder.
override_data {
  target          = data.aws_secretsmanager_secret_version.worker_marker
  override_during = plan
  values = {
    secret_string = "fixture-only-32-byte-key-never-deploy"
    version_id    = "fixture-current-version"
  }
}

run "preparation_does_not_read_or_project_marker_key_or_receive_tasks" {
  command = plan
  assert {
    condition = (
      length(data.aws_secretsmanager_secret_version.worker_marker) == 0 &&
      length(kubernetes_secret.worker_run_services) == 0 &&
      kubernetes_config_map.worker_gateway[0].data.ADP_RUN_TASKS_ENABLED == "false" &&
      !contains(keys(kubernetes_config_map.worker_gateway[0].data), "ADP_RUN_TASK_QUEUE_URL")
    )
    error_message = "Preparation must not read/project service keys or activate task consumption."
  }
}

run "active_run_services_are_gateway_owned" {
  command = plan
  variables {
    agent_authority_enabled                = true
    agent_authority_runtime_ready          = true
    agent_authority_legacy_workers_drained = true
    agent_worker_admission_paused          = true
  }
  assert {
    condition = (
      kubernetes_config_map.worker_gateway[0].data.ADP_RUN_TASKS_ENABLED == "true" &&
      kubernetes_config_map.worker_gateway[0].data.ADP_RUN_TASK_QUEUE_URL == aws_sqs_queue.agent_submit.url &&
      kubernetes_config_map.worker_gateway[0].data.AGENT_RUN_LOGS_BUCKET == aws_s3_bucket.agent_run_logs.bucket &&
      kubernetes_config_map.worker_gateway[0].data.AGENT_FALLBACK_BUCKET == aws_s3_bucket.agent_run_logs.bucket &&
      kubernetes_config_map.worker_gateway[0].data.ADP_DOOR_SERVICE_URL == var.agent_door_service_url
    )
    error_message = "Gateway task and archive settings must select only this environment's resources."
  }
  assert {
    condition = (
      kubernetes_secret.worker_run_services[0].metadata[0].namespace == var.gateway_namespace &&
      kubernetes_secret.worker_run_services[0].data["marker-signing-key"] == data.aws_secretsmanager_secret_version.worker_marker[0].secret_string
    )
    error_message = "Project the existing marker key only to the gateway namespace."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.gateway_authorized_dispatch[0].policy).Statement[2] == {
      Sid      = "DeliverOwnRunTask", Effect = "Allow",
      Action   = ["sqs:ReceiveMessage", "sqs:ChangeMessageVisibility", "sqs:DeleteMessage"],
      Resource = [aws_sqs_queue.agent_submit.arn]
    }
    error_message = "Gateway task delivery needs receive/heartbeat/ack only on its queue."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.gateway_authorized_dispatch[0].policy).Statement[3] == {
      Sid      = "WriteOwnRunArtifacts", Effect = "Allow", Action = ["s3:PutObject"],
      Resource = ["${aws_s3_bucket.agent_run_logs.arn}/runs/*"]
    }
    error_message = "Gateway archive writes must be limited to the run namespace."
  }
  assert {
    condition = (
      yamldecode(local.keda_trigger_auth_yaml).spec.podIdentity.identityOwner == "keda"
    )
    error_message = "KEDA must poll under its own identity now workers have no SQS access."
  }
}

run "managed_gateway_grants_do_not_consume_inline_quota" {
  command = plan
  variables {
    gateway_authority_managed_policies = true
  }
  override_resource {
    target          = aws_iam_role.agent_scaledjob
    override_during = plan
    values          = { arn = "arn:aws:iam::123456789012:role/adp-dev-agent-scaledjob-role" }
  }
  override_resource {
    target          = aws_iam_policy.gateway_authorized_dispatch
    override_during = plan
    values          = { arn = "arn:aws:iam::123456789012:policy/adp-dev-policy-gateway-authorized-dispatch" }
  }
  override_resource {
    target          = aws_iam_policy.gateway_task_source
    override_during = plan
    values          = { arn = "arn:aws:iam::123456789012:policy/adp-dev-policy-gateway-task-source" }
  }
  assert {
    condition = (
      length(aws_iam_role_policy.gateway_authorized_dispatch) == 0 &&
      length(aws_iam_role_policy.gateway_task_source) == 0 &&
      aws_iam_role_policy_attachment.gateway_authorized_dispatch[0].role == "adp-dev-role-gateway-service" &&
      aws_iam_role_policy_attachment.gateway_task_source[0].role == "adp-dev-role-gateway-service" &&
      aws_iam_role_policy_attachment.gateway_authorized_dispatch[0].policy_arn == aws_iam_policy.gateway_authorized_dispatch[0].arn &&
      aws_iam_role_policy_attachment.gateway_task_source[0].policy_arn == aws_iam_policy.gateway_task_source[0].arn
    )
    error_message = "Managed mode must attach both scoped policies to the gateway without consuming inline quota."
  }
  assert {
    condition = jsondecode(aws_iam_policy.gateway_authorized_dispatch[0].policy).Statement[2] == {
      Sid      = "DeliverOwnRunTask", Effect = "Allow",
      Action   = ["sqs:ReceiveMessage", "sqs:ChangeMessageVisibility", "sqs:DeleteMessage"],
      Resource = ["arn:aws:sqs:us-east-1:123456789012:adp-dev-agent-submit.fifo"]
    }
    error_message = "Managed policy must retain the exact own-queue permissions."
  }
  assert {
    condition = jsondecode(aws_iam_policy.gateway_authorized_dispatch[0].policy).Statement[3] == {
      Sid      = "WriteOwnRunArtifacts", Effect = "Allow", Action = ["s3:PutObject"],
      Resource = ["arn:aws:s3:::adp-dev-agent-run-logs-123456789012/runs/*"]
    }
    error_message = "Managed policy must retain the exact run archive prefix."
  }
  assert {
    condition = (
      length(jsondecode(aws_iam_policy.gateway_task_source[0].policy).Statement) == 3 &&
      jsondecode(aws_iam_policy.gateway_task_source[0].policy).Statement[0].Action == ["sts:AssumeRole"] &&
      jsondecode(aws_iam_policy.gateway_task_source[0].policy).Statement[0].Resource == aws_iam_role.agent_scaledjob.arn &&
      kubernetes_config_map.worker_gateway[0].data.AGENT_TASK_SOURCE_ISOLATION_CONFIRMED == "false" &&
      kubernetes_config_map.worker_gateway[0].data.AGENT_AUTHORITY_ENABLED == "false"
    )
    error_message = "Managed grants must not broaden source assumption or silently activate authority/source trust."
  }
}

run "activation_refuses_placeholder_marker_key" {
  command = plan
  variables {
    agent_authority_enabled                = true
    agent_authority_runtime_ready          = true
    agent_authority_legacy_workers_drained = true
    agent_worker_admission_paused          = true
  }
  override_data {
    target          = data.aws_secretsmanager_secret_version.worker_marker
    override_during = plan
    values = {
      secret_string = "PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND"
      version_id    = "unseeded-version"
    }
  }
  expect_failures = [kubernetes_secret.worker_run_services]
}

run "shared_reporting_preserves_existing_worker_role" {
  command = plan
  variables {
    agent_authority_enabled            = false
    shared_run_reporting_enabled       = true
    shared_worker_continuation_enabled = true
  }
  assert {
    condition     = local.agent_worker_sa_name == "agent-scaledjob-sa" && local.worker_gateway_config.ADP_SHARED_RUN_REPORTING_ENABLED == "true" && local.worker_gateway_config.ADP_SHARED_WORKER_CONTINUATION_ENABLED == "true"
    error_message = "Shared reporting and accepted continuation must use the existing worker service account."
  }
  assert {
    condition     = aws_ssm_parameter.agent_run_reporting_key.type == "SecureString" && aws_ssm_parameter.agent_run_reporting_key.key_id == aws_kms_key.dynamodb.arn
    error_message = "The shared gateway/tick signing key must use the existing customer-managed encryption key."
  }
}

run "continuation_cannot_outrun_reporting" {
  command = plan
  variables {
    agent_authority_enabled            = false
    shared_run_reporting_enabled       = false
    shared_worker_continuation_enabled = true
  }
  expect_failures = [kubernetes_config_map.worker_gateway]
}
