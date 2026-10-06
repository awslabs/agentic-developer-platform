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
    arn        = "arn:aws:sts::123456789012:assumed-role/Installer/test"
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

# Pin the role ARN so the configured registry JSON is known during plan.
override_resource {
  target          = aws_iam_role.agent_authority_worker
  override_during = plan
  values          = { arn = "arn:aws:iam::123456789012:role/adp-dev-agent-authority-worker-role" }
}

override_resource {
  target          = aws_iam_role.keda_operator
  override_during = plan
  values = {
    arn = "arn:aws:iam::123456789012:role/adp-dev-keda-operator-role"
  }
}



override_data {
  target          = data.aws_ssm_parameter.domain_operation_registry
  override_during = plan
  values          = { value = "test-domain-registry" }
}
override_data {
  target          = data.aws_iam_role.domain_operation_producer
  override_during = plan
  values          = { arn = "arn:aws:iam::123456789012:role/adp-dev-superplane-api-producer", unique_id = "AROA11111111111111111" }
}
override_data {
  target          = data.aws_iam_role.domain_operation_worker
  override_during = plan
  values          = { arn = "arn:aws:iam::123456789012:role/adp-dev-superplane-domain-worker", unique_id = "AROA22222222222222222" }
}
run "default_is_absent" {
  command = plan
  assert {
    condition     = length(terraform_data.domain_operation_registration) == 0 && length(aws_iam_role_policy.gateway_domain_operations) == 0 && length(kubernetes_role.gateway_domain_operation_read) == 0 && !contains(keys(local.worker_gateway_config), "ADP_DOMAIN_OPERATION_BINDINGS")
    error_message = "Default deployments must add no registration, IAM, RBAC or binding config."
  }
}
run "explicit_binding_uses_exact_resources_and_separate_schemas" {
  command = plan
  variables {
    agent_authority_prepared = true
    domain_operation_bindings = {
      superplane = {
        operator_role_arn = "arn:aws:iam::123456789012:role/Installer"
        producer_role_arn = "arn:aws:iam::123456789012:role/adp-dev-superplane-api-producer"
        producer_role_id  = "AROA11111111111111111"
        worker_role_arn   = "arn:aws:iam::123456789012:role/adp-dev-superplane-domain-worker"
        worker_role_id    = "AROA22222222222222222"
        binding = {
          domain                           = "superplane"
          org_id                           = "00000000-0000-4000-8000-000000000001"
          adp_org_id                       = "test-org"
          producer_registry_id             = "00000000-0000-4000-8000-000000000002"
          worker_registry_id               = "00000000-0000-4000-8000-000000000003"
          database_secret_id               = "arn:aws:secretsmanager:us-east-1:123456789012:secret:operations-AbCdEf"
          database_schema                  = "superplane_operations"
          domain_database_secret_id        = "arn:aws:secretsmanager:us-east-1:123456789012:secret:domain-AbCdEf"
          domain_database_schema           = "superplane"
          observation_credential_secret_id = "arn:aws:secretsmanager:us-east-1:123456789012:secret:observation-AbCdEf"
          queue_url                        = "https://sqs.us-east-1.amazonaws.com/123456789012/adp-dev-superplane-domain-operations"
          worker_namespace                 = "superplane"
          worker_service_account           = "superplane-paid-worker"
          worker_scaled_job                = "superplane-paid-worker"
          worker_container                 = "paid-worker"
          worker_image_digests             = ["sha256:1111111111111111111111111111111111111111111111111111111111111111"]
          repo                             = "example/project"
          observation_url                  = "https://gateway.example/internal"
        }
      }
    }
  }
  assert {
    condition     = length(terraform_data.domain_operation_registration) == 1 && length(local.domain_operation_secrets) == 3 && local.domain_operation_gateway_bindings[0].database_schema != local.domain_operation_gateway_bindings[0].domain_database_schema && jsondecode(local.worker_gateway_config.ADP_DOMAIN_OPERATION_BINDINGS)[0].worker_service_account == "superplane-paid-worker"
    error_message = "The actual Gateway payload must retain exact separate credentials/schemas and worker identity."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.gateway_domain_operations[0].policy).Statement[0].Resource == ["arn:aws:sqs:us-east-1:123456789012:adp-dev-superplane-domain-operations"] && !strcontains(aws_iam_role_policy.gateway_domain_operations[0].policy, "*") && alltrue([for rule in kubernetes_role.gateway_domain_operation_read["superplane"].rule : toset(rule.verbs) == toset(["get"]) && !contains(rule.resources, "secrets")])
    error_message = "Shared owner permissions must be exact, read-only Kubernetes metadata and no worker grants."
  }
}

run "shared_schema_is_refused" {
  command = plan
  variables {
    agent_authority_prepared = true
    domain_operation_bindings = {
      superplane = {
        operator_role_arn = "arn:aws:iam::123456789012:role/Installer"
        producer_role_arn = "arn:aws:iam::123456789012:role/adp-dev-superplane-api-producer"
        producer_role_id  = "AROA11111111111111111"
        worker_role_arn   = "arn:aws:iam::123456789012:role/adp-dev-superplane-domain-worker"
        worker_role_id    = "AROA22222222222222222"
        binding = {
          domain                           = "superplane"
          org_id                           = "00000000-0000-4000-8000-000000000001"
          adp_org_id                       = "test-org"
          producer_registry_id             = "00000000-0000-4000-8000-000000000002"
          worker_registry_id               = "00000000-0000-4000-8000-000000000003"
          database_secret_id               = "arn:aws:secretsmanager:us-east-1:123456789012:secret:operations-AbCdEf"
          database_schema                  = "superplane_operations"
          domain_database_secret_id        = "arn:aws:secretsmanager:us-east-1:123456789012:secret:domain-AbCdEf"
          domain_database_schema           = "superplane_operations"
          observation_credential_secret_id = "arn:aws:secretsmanager:us-east-1:123456789012:secret:observation-AbCdEf"
          queue_url                        = "https://sqs.us-east-1.amazonaws.com/123456789012/adp-dev-superplane-domain-operations"
          worker_namespace                 = "superplane"
          worker_service_account           = "superplane-paid-worker"
          worker_scaled_job                = "superplane-paid-worker"
          worker_container                 = "paid-worker"
          worker_image_digests             = ["sha256:1111111111111111111111111111111111111111111111111111111111111111"]
          repo                             = "example/project"
          observation_url                  = "https://gateway.example/internal"
        }
      }
    }
  }
  expect_failures = [var.domain_operation_bindings]
}

run "recreated_role_identity_is_refused" {
  command = plan
  variables {
    agent_authority_prepared = true
    domain_operation_bindings = {
      superplane = {
        operator_role_arn = "arn:aws:iam::123456789012:role/Installer"
        producer_role_arn = "arn:aws:iam::123456789012:role/adp-dev-superplane-api-producer"
        producer_role_id  = "AROA99999999999999999"
        worker_role_arn   = "arn:aws:iam::123456789012:role/adp-dev-superplane-domain-worker"
        worker_role_id    = "AROA22222222222222222"
        binding = {
          domain                           = "superplane"
          org_id                           = "00000000-0000-4000-8000-000000000001"
          adp_org_id                       = "test-org"
          producer_registry_id             = "00000000-0000-4000-8000-000000000002"
          worker_registry_id               = "00000000-0000-4000-8000-000000000003"
          database_secret_id               = "arn:aws:secretsmanager:us-east-1:123456789012:secret:operations-AbCdEf"
          database_schema                  = "superplane_operations"
          domain_database_secret_id        = "arn:aws:secretsmanager:us-east-1:123456789012:secret:domain-AbCdEf"
          domain_database_schema           = "superplane"
          observation_credential_secret_id = "arn:aws:secretsmanager:us-east-1:123456789012:secret:observation-AbCdEf"
          queue_url                        = "https://sqs.us-east-1.amazonaws.com/123456789012/adp-dev-superplane-domain-operations"
          worker_namespace                 = "superplane"
          worker_service_account           = "superplane-paid-worker"
          worker_scaled_job                = "superplane-paid-worker"
          worker_container                 = "paid-worker"
          worker_image_digests             = ["sha256:1111111111111111111111111111111111111111111111111111111111111111"]
          repo                             = "example/project"
          observation_url                  = "https://gateway.example/internal"
        }
      }
    }
  }
  expect_failures = [terraform_data.domain_operation_registration]
}
