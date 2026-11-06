mock_provider "aws" {}
mock_provider "kubernetes" {}

variables {
  environment    = "dev"
  aws_region     = "us-east-1"
  aws_account_id = "000000000000"
}
override_data {
  target = data.terraform_remote_state.platform[0]
  values = {
    outputs = {
      eks_cluster_endpoint       = "https://example.invalid"
      eks_cluster_ca_certificate = "Y2VydA=="
      eks_cluster_name           = "fixture-cluster"
      eks_oidc_issuer            = "oidc.eks.us-east-1.amazonaws.com/id/fixture"
      eks_oidc_provider_arn      = "arn:aws:iam::000000000000:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/fixture"
    }
  }
}

run "disabled_collection_reads_no_shared_state" {
  command = plan
  assert {
    condition = (length(data.terraform_remote_state.platform) == 0 && length(aws_iam_role.worker) == 0 &&
      length(kubernetes_namespace_v1.collection) == 0 && length(kubernetes_deployment_v1.worker) == 0 &&
      length(aws_iam_role_policy.worker_database) == 0)
    error_message = "Default-off collection must not read shared state or provision resources."
  }
}

run "cell_workload_is_quarantined_before_migration" {
  command = plan
  variables {
    enabled                = true
    connectors_enabled     = true
    cell_id                = "fixture-cell"
    existing_db_endpoint   = "fixture.cluster-abc.us-east-1.rds.amazonaws.com"
    existing_db_resource_id = "cluster-FIXTURE123"
    ca_configmap_name      = "approved-rds-ca"
    schema_manifest_sha256 = "a9cd139a21eb46bbc65aa3c5e1e3519a41e7d4c3901953e83e7cd157d4001ef2"
    worker_image           = "registry.example.invalid/worker@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    worker_role_boundary_arn = "arn:aws:iam::000000000000:policy/operator-approved-worker"
    writer_db_username     = "fixture_writer"
  }
  assert {
    condition = (kubernetes_deployment_v1.worker[0].spec[0].replicas == "0" &&
      kubernetes_deployment_v1.worker[0].spec[0].template[0].spec[0].container[0].image == var.worker_image &&
      length(kubernetes_network_policy_v1.deny) == 1)
    error_message = "A pinned cell worker must stay inactive and network-isolated until migration is admitted."
  }
  assert {
    condition = (aws_iam_role.worker[0].permissions_boundary == var.worker_role_boundary_arn &&
      contains(keys(kubernetes_service_account_v1.worker[0].metadata[0].annotations), "eks.amazonaws.com/role-arn") &&
      jsondecode(aws_iam_role.worker[0].assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.us-east-1.amazonaws.com/id/fixture:sub"] == "system:serviceaccount:adp-insights-devlake-fixture-cell:devlake-fixture-cell-worker" &&
      length([for entry in kubernetes_deployment_v1.worker[0].spec[0].template[0].spec[0].container[0].env : entry if entry.name == "DEVLAKE_DB_AUTH" && entry.value == "iam"]) == 1 &&
      length([for entry in kubernetes_deployment_v1.worker[0].spec[0].template[0].spec[0].container[0].env : entry if entry.name == "DEVLAKE_DB_USERNAME" && entry.value == "fixture_writer"]) == 1 &&
      length([for entry in kubernetes_deployment_v1.worker[0].spec[0].template[0].spec[0].container[0].env : entry if entry.name == "DEVLAKE_DB_SSLMODE" && entry.value == "verify-full"]) == 1)
    error_message = "The cell workload must have a bounded identity and IAM/verified-TLS configuration, not a password DSN."
  }
  assert {
    condition = (length([for entry in kubernetes_deployment_v1.worker[0].spec[0].template[0].spec[0].container[0].env : entry if entry.name == "FEATURE_DEVLAKE_CONNECTORS_ENABLED" && entry.value == "true"]) == 1 &&
      length([for entry in kubernetes_deployment_v1.worker[0].spec[0].template[0].spec[0].container[0].env : entry if entry.name == "DEVLAKE_COLLECTION_ENABLED" && entry.value == "true"]) == 1)
    error_message = "A selected worker must receive both explicit effective optional deployment flags."
  }
  assert {
    condition = (length(jsondecode(aws_iam_role_policy.worker_database[0].policy).Statement) == 1 &&
      jsondecode(aws_iam_role_policy.worker_database[0].policy).Statement[0].Action == ["rds-db:connect"] &&
      jsondecode(aws_iam_role_policy.worker_database[0].policy).Statement[0].Resource == "arn:aws:rds-db:us-east-1:000000000000:dbuser:cluster-FIXTURE123/fixture_writer")
    error_message = "The worker must connect only as its own writer on the selected cell DB resource."
  }
}

run "reject_collection_without_connectors" {
  command = plan
  variables {
    enabled                = true
    cell_id                = "fixture-cell"
    existing_db_endpoint   = "fixture.cluster-abc.us-east-1.rds.amazonaws.com"
    existing_db_resource_id = "cluster-FIXTURE123"
    ca_configmap_name      = "approved-rds-ca"
    schema_manifest_sha256 = "a9cd139a21eb46bbc65aa3c5e1e3519a41e7d4c3901953e83e7cd157d4001ef2"
    worker_image           = "registry.example.invalid/worker@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    worker_role_boundary_arn = "arn:aws:iam::000000000000:policy/operator-approved-worker"
    writer_db_username     = "fixture_writer"
  }
  expect_failures = [aws_iam_role.worker[0]]
}

run "reject_schema_mismatch" {
  command = plan
  variables {
    enabled                = true
    connectors_enabled     = true
    cell_id                = "fixture-cell"
    existing_db_endpoint   = "fixture.cluster-abc.us-east-1.rds.amazonaws.com"
    existing_db_resource_id = "cluster-FIXTURE123"
    ca_configmap_name      = "approved-rds-ca"
    schema_manifest_sha256 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    worker_image           = "registry.example.invalid/worker@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    worker_role_boundary_arn = "arn:aws:iam::000000000000:policy/operator-approved-worker"
    writer_db_username     = "fixture_writer"
  }
  expect_failures = [aws_iam_role.worker[0]]
}
