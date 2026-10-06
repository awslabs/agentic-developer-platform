mock_provider "aws" {}
mock_provider "kubernetes" {}
variables {
  account_id                  = "123456789012"
  caller_arn                  = "arn:aws:sts::123456789012:assumed-role/Installer/test"
  environment                 = "dev"
  aws_region                  = "us-east-1"
  gateway_namespace           = "adp-gateway"
  agent_authority_provisioned = false
}
override_data {
  target = data.aws_ssm_parameter.domain_operation_registry
  values = { value = "test-domain-registry" }
}
override_data {
  target = data.aws_iam_role.domain_operation_producer
  values = { arn = "arn:aws:iam::123456789012:role/adp-dev-superplane-api-producer", unique_id = "AROA11111111111111111" }
}
override_data {
  target = data.aws_iam_role.domain_operation_worker
  values = { arn = "arn:aws:iam::123456789012:role/adp-dev-superplane-domain-worker", unique_id = "AROA22222222222222222" }
}
run "default_is_absent" {
  command = plan
  assert {
    condition     = length(terraform_data.domain_operation_registration) == 0 && length(aws_iam_role_policy.gateway_domain_operations) == 0 && length(kubernetes_role.gateway_domain_operation_read) == 0 && length(output.gateway_bindings) == 0
    error_message = "Default deployments must add no registration, IAM, RBAC or binding config."
  }
}
run "explicit_binding_uses_exact_resources_and_separate_schemas" {
  command = plan
  variables {
    agent_authority_provisioned = true
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
          producer_registry_id             = "cc73893b-35a5-53c8-bdd9-7e5f0b3f6312"
          worker_registry_id               = "bcda60a1-7ab6-5261-9412-40b5ecced082"
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
    condition     = length(terraform_data.domain_operation_registration) == 1 && length(local.domain_operation_secrets) == 3 && local.domain_operation_gateway_bindings[0].database_schema != local.domain_operation_gateway_bindings[0].domain_database_schema && output.gateway_bindings[0].worker_service_account == "superplane-paid-worker"
    error_message = "The actual Gateway payload must retain exact separate credentials/schemas and worker identity."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.gateway_domain_operations[0].policy).Statement[0].Resource == ["arn:aws:sqs:us-east-1:123456789012:adp-dev-superplane-domain-operations"] && !strcontains(aws_iam_role_policy.gateway_domain_operations[0].policy, "*") && alltrue([for rule in kubernetes_role.gateway_domain_operation_read["superplane"].rule : toset(rule.verbs) == toset(["get"]) && !contains(rule.resources, "secrets")])
    error_message = "Shared owner permissions must be exact, read-only Kubernetes metadata and no worker grants."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.gateway_domain_operations[0].policy).Statement[2].Action == ["iam:GetRole"] && jsondecode(aws_iam_role_policy.gateway_domain_operations[0].policy).Statement[2].Resource == ["arn:aws:iam::123456789012:role/adp-dev-superplane-api-producer", "arn:aws:iam::123456789012:role/adp-dev-superplane-domain-worker"]
    error_message = "Runtime immutable identity verification may read only the exact producer and worker IAM roles."
  }
}

run "shared_schema_is_refused" {
  command = plan
  variables {
    agent_authority_provisioned = true
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
          producer_registry_id             = "cc73893b-35a5-53c8-bdd9-7e5f0b3f6312"
          worker_registry_id               = "bcda60a1-7ab6-5261-9412-40b5ecced082"
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
    agent_authority_provisioned = true
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
          producer_registry_id             = "cc73893b-35a5-53c8-bdd9-7e5f0b3f6312"
          worker_registry_id               = "bcda60a1-7ab6-5261-9412-40b5ecced082"
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
