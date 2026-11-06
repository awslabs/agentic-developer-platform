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
      vpc_id                     = "vpc-00000000000000000"
      eks_cluster_endpoint       = "https://example.invalid"
      eks_cluster_ca_certificate = "Y2VydA=="
      eks_cluster_name           = "fixture-cluster"
      eks_oidc_issuer            = "oidc.eks.us-east-1.amazonaws.com/id/fixture"
      eks_oidc_provider_arn      = "arn:aws:iam::000000000000:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/fixture"
    }
  }
}

override_data {
  target = data.aws_vpc_endpoint.sts[0]
  values = {
    vpc_endpoint_type     = "Interface"
    private_dns_enabled   = true
    state                 = "available"
    network_interface_ids = ["eni-00000000000000001"]
  }
}
override_data {
  target = data.aws_network_interface.sts["eni-00000000000000001"]
  values = { private_ip = "10.0.1.10" }
}
run "default_is_inert" {
  command = plan
  assert {
    condition = (length(aws_iam_role.api) == 0 && length(kubernetes_namespace_v1.api) == 0 &&
      length(kubernetes_deployment_v1.api) == 0 && length(kubernetes_service_v1.api) == 0 &&
      length(data.terraform_remote_state.platform) == 0 && output.api_service == null)
    error_message = "The disabled connector root must not read cluster state or own any workload."
  }
}

run "connector_only_uses_pinned_image_and_workload_identity" {
  command = plan
  variables {
    enabled               = true
    api_image             = "registry.example.invalid/devlake-api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    deployment_role_boundaries = { "arn:aws:iam::000000000000:role/adp-dev-devlake-connectors-api" = "arn:aws:iam::000000000000:policy/approved-api-boundary" }
    provider_egress_cidrs = ["198.51.100.0/24"]
  }
  assert {
    condition = (kubernetes_deployment_v1.api[0].spec[0].template[0].spec[0].container[0].image == var.api_image &&
      kubernetes_deployment_v1.api[0].spec[0].template[0].spec[0].container[0].env[0].name == "CONNECTOR_API_LISTEN" &&
      length([for entry in kubernetes_deployment_v1.api[0].spec[0].template[0].spec[0].container[0].env : entry if entry.name == "FEATURE_DEVLAKE_CONNECTORS_ENABLED" && entry.value == "true"]) == 1 &&
      aws_iam_role.api[0].permissions_boundary == var.deployment_role_boundaries["arn:aws:iam::000000000000:role/adp-dev-devlake-connectors-api"] &&
      output.api_service == "connector-api.adp-devlake-connectors.svc.cluster.local:8080")
    error_message = "Connector-only mode must use a pinned image, bounded role and isolated API service."
  }
  assert {
    condition     = length(kubernetes_network_policy_v1.deny) == 1 && length(kubernetes_network_policy_v1.allow) == 1 && length(data.aws_network_interface.sts) == 1
    error_message = "The connector namespace must default-deny network traffic."
  }
}

run "reject_unpinned_image" {
  command = plan
  variables {
    enabled           = true
    api_image         = "registry.example.invalid/devlake-api:latest"
    deployment_role_boundaries = { "arn:aws:iam::000000000000:role/adp-dev-devlake-connectors-api" = "arn:aws:iam::000000000000:policy/approved-api-boundary" }
  }
  expect_failures = [var.api_image]
}

run "reject_missing_role_admission" {
  command = plan
  variables {
    enabled   = true
    api_image = "registry.example.invalid/devlake-api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
  expect_failures = [aws_iam_role.api[0]]
}

run "reject_public_sts" {
  command = plan
  variables {
    enabled   = true
    api_image = "registry.example.invalid/devlake-api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    deployment_role_boundaries = {
      "arn:aws:iam::000000000000:role/adp-dev-devlake-connectors-api" = "arn:aws:iam::000000000000:policy/approved-api-boundary"
    }
  }
  override_data {
    target = data.aws_vpc_endpoint.sts[0]
    values = {
      vpc_endpoint_type     = "Gateway"
      private_dns_enabled   = false
      state                 = "available"
      network_interface_ids = ["eni-00000000000000001"]
    }
  }
  expect_failures = [data.aws_vpc_endpoint.sts[0]]
}

run "reject_cross_account_boundary" {
  command = plan
  variables {
    enabled   = true
    api_image = "registry.example.invalid/devlake-api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    deployment_role_boundaries = {
      "arn:aws:iam::000000000000:role/adp-dev-devlake-connectors-api" = "arn:aws:iam::111111111111:policy/foreign-boundary"
    }
  }
  expect_failures = [aws_iam_role.api[0]]
}
