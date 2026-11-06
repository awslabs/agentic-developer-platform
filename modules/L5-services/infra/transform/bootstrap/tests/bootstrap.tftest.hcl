mock_provider "aws" {
  mock_data "aws_eks_cluster" {
    defaults = { endpoint = "https://example.eks.amazonaws.com", certificate_authority = [{ data = "ZXhhbXBsZQ==" }] }
  }
}
mock_provider "kubernetes" {}

variables {
  aws_region        = "us-east-1"
  aws_account_id    = "000000000000"
  environment       = "example"
  worker_role_arn   = "arn:aws:iam::000000000000:role/adp-example-l5-transform-worker"
  worker_role_name  = "adp-example-l5-transform-worker"
  gateway_role_name = "adp-example-gateway"
  executor = {
    cluster_name       = "example-isolated"
    node_name          = "example-node"
    node_uid           = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    provider_namespace = "example-transform-provider"
    check_namespace    = "example-transform-checks"
    provider_image     = "example.invalid/transform@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    check_image        = "example.invalid/checks@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    provider_cidrs     = ["192.0.2.1/32"]
  }
}

run "operator_bootstrap_is_scoped" {
  command = plan
  override_resource {
    target          = aws_s3_bucket.executor["selected"]
    values          = { arn = "arn:aws:s3:::example-transform-artifacts" }
    override_during = plan
  }
  assert {
    condition = aws_ecr_repository.executor.name == "adp-example-l5-transform-executor" && (
      aws_eks_access_entry.worker["selected"].principal_arn == var.worker_role_arn
    ) && kubernetes_cluster_role_v1.node_observer["selected"].rule[0].resource_names == tolist([var.executor.node_name])
    error_message = "Only the selected worker and qualified node may receive control access."
  }
  assert {
    condition     = length(kubernetes_role_v1.worker["checks"].rule) == 3 && kubernetes_resource_quota_v1.executor["provider"].spec[0].hard.pods == "5"
    error_message = "Check workers must have no Secret rule, and provider concurrency stays at five."
  }
  assert {
    condition     = length(kubernetes_network_policy_v1.deny["checks"].spec[0].egress) == 0 && length(kubernetes_network_policy_v1.deny["checks"].spec[0].ingress) == 0
    error_message = "Repository checks require denied ingress and egress."
  }
  assert {
    condition     = alltrue([for account in kubernetes_service_account_v1.sandbox : account.automount_service_account_token == false])
    error_message = "Sandbox Pods must use service accounts without automatic Kubernetes credentials."
  }
  assert {
    condition = aws_iam_role_policy.gateway_artifact_reads["selected"].role == var.gateway_role_name && jsondecode(aws_iam_role_policy.gateway_artifact_reads["selected"].policy) == {
      Version = "2012-10-17", Statement = [{
        Effect = "Allow", Action = ["s3:GetObjectVersion"], Resource = [
          "${aws_s3_bucket.executor["selected"].arn}/l5/transform/qualification/catalog.json",
          "${aws_s3_bucket.executor["selected"].arn}/l5/transform/artifacts/*"
        ]
      }]
    }
    error_message = "Gateway may only read exact retained object versions; no writes, listing, cluster or provider grants."
  }
}

run "repository_only_before_images_exist" {
  command = plan
  variables {
    executor          = null
    gateway_role_name = null
  }
  assert {
    condition     = aws_ecr_repository.executor.name == "adp-example-l5-transform-executor" && aws_ecr_repository.executor.image_tag_mutability == "IMMUTABLE"
    error_message = "Repository-only bootstrap must create the immutable image repository."
  }
  assert {
    condition = alltrue([
      length(data.aws_eks_cluster.executor) == 0,
      length(data.aws_eks_cluster_auth.executor) == 0,
      length(aws_eks_access_entry.worker) == 0,
      length(aws_iam_role_policy.executor_artifacts) == 0,
      length(aws_iam_role_policy.gateway_artifact_reads) == 0,
      length(aws_s3_bucket.executor) == 0,
      length(aws_s3_bucket_versioning.executor) == 0,
      length(aws_s3_bucket_public_access_block.executor) == 0,
      length(aws_s3_bucket_lifecycle_configuration.executor) == 0,
      length(kubernetes_namespace_v1.executor) == 0,
      length(kubernetes_service_account_v1.sandbox) == 0,
      length(kubernetes_role_v1.worker) == 0,
      length(kubernetes_role_binding_v1.worker) == 0,
      length(kubernetes_cluster_role_v1.node_observer) == 0,
      length(kubernetes_cluster_role_binding_v1.node_observer) == 0,
      length(kubernetes_network_policy_v1.deny) == 0,
      length(kubernetes_network_policy_v1.provider) == 0,
      length(kubernetes_resource_quota_v1.executor) == 0,
      length(kubernetes_manifest.isolated_node_class) == 0,
      length(kubernetes_manifest.isolated_node_pool) == 0,
      length(aws_vpc_endpoint.provider) == 0,
      length(aws_security_group.isolated_node_endpoint) == 0,
      length(aws_security_group.provider_endpoint) == 0,
      length(output.worker_environment) == 0,
      output.executor_artifact_bucket == null,
    ])
    error_message = "Null executor must perform no cluster lookup and create no IAM, bucket, node, or runtime resources."
  }
}

run "executor_requires_gateway_reader" {
  command = plan
  variables { gateway_role_name = null }
  expect_failures = [aws_iam_role_policy.gateway_artifact_reads]
}

run "candidate_capacity_does_not_enable_executor" {
  command = plan
  variables {
    executor          = null
    gateway_role_name = null
    isolated_node_pool = {
      cluster_name       = "example-isolated"
      node_role_name     = "existing-eks-node-role"
      subnet_ids         = ["subnet-example-private"]
      security_group_ids = ["sg-example-node"]
    }
  }
  assert {
    condition = (
      kubernetes_manifest.isolated_node_class["selected"].manifest.spec.networkPolicy == "DefaultDeny" &&
      kubernetes_manifest.isolated_node_class["selected"].manifest.spec.advancedCompute.kubelet.podPidsLimit == 128 &&
      kubernetes_manifest.isolated_node_pool["selected"].manifest.spec.limits.cpu == "4" &&
      kubernetes_manifest.isolated_node_pool["selected"].manifest.spec.disruption.consolidateAfter == "60s" &&
      kubernetes_manifest.isolated_node_pool["selected"].manifest.spec.template.spec.expireAfter == "24h" &&
      kubernetes_manifest.isolated_node_pool["selected"].manifest.spec.template.metadata.labels == { "adp.dev/transform-candidate" = "v1" } &&
      kubernetes_manifest.isolated_node_pool["selected"].manifest.spec.template.spec.taints == [{ key = "adp.dev/transform", value = "only", effect = "NoSchedule" }]
    )
    error_message = "Candidate capacity must be dedicated, bounded and deny network by default without inheriting qualification."
  }
  assert {
    condition = alltrue([
      length(data.aws_eks_cluster.executor) == 1,
      length(aws_eks_access_entry.worker) == 0,
      length(aws_iam_role_policy.executor_artifacts) == 0,
      length(aws_iam_role_policy.gateway_artifact_reads) == 0,
      length(aws_s3_bucket.executor) == 0,
      length(kubernetes_namespace_v1.executor) == 0,
      length(output.worker_environment) == 0,
    ])
    error_message = "Preparing a candidate pool must not create execution authority, images, runtime settings or qualification."
  }
}

run "warm_capacity_retains_idle_nodes_without_qualifying_replacements" {
  command = plan
  variables {
    executor          = null
    gateway_role_name = null
    isolated_node_pool = {
      cluster_name       = "example-isolated"
      node_role_name     = "existing-eks-node-role"
      subnet_ids         = ["subnet-example-private"]
      security_group_ids = ["sg-example-node"]
      capacity_mode      = "warm"
    }
  }
  assert {
    condition = (
      kubernetes_manifest.isolated_node_pool["selected"].manifest.spec.disruption == {
        consolidationPolicy = "WhenEmpty", consolidateAfter = "Never"
      } &&
      kubernetes_manifest.isolated_node_pool["selected"].manifest.spec.template.spec.expireAfter == "24h" &&
      kubernetes_manifest.isolated_node_pool["selected"].manifest.spec.limits.cpu == "4" &&
      kubernetes_manifest.isolated_node_pool["selected"].manifest.spec.template.metadata == { labels = { "adp.dev/transform-candidate" = "v1" } } &&
      kubernetes_manifest.isolated_node_pool["selected"].manifest.spec.template.spec.taints == [{ key = "adp.dev/transform", value = "only", effect = "NoSchedule" }] &&
      kubernetes_manifest.isolated_node_class["selected"].manifest.spec.networkPolicy == "DefaultDeny" &&
      kubernetes_manifest.isolated_node_class["selected"].manifest.spec.advancedCompute.kubelet.podPidsLimit == 128
    )
    error_message = "Warm mode may disable idle consolidation only; lifetime, capacity and isolation boundaries must remain unchanged."
  }
  assert {
    condition = alltrue([
      length(aws_eks_access_entry.worker) == 0,
      length(aws_iam_role_policy.executor_artifacts) == 0,
      length(aws_iam_role_policy.gateway_artifact_reads) == 0,
      length(kubernetes_namespace_v1.executor) == 0,
      length(output.worker_environment) == 0,
    ])
    error_message = "Warm capacity must not create execution authority or install a qualified node binding."
  }
}

run "isolated_pool_rejects_unknown_capacity_mode" {
  command = plan
  variables {
    isolated_node_pool = {
      cluster_name       = "example-isolated"
      node_role_name     = "existing-eks-node-role"
      subnet_ids         = ["subnet-example-private"]
      security_group_ids = ["sg-example-node"]
      capacity_mode      = "unbounded"
    }
  }
  expect_failures = [var.isolated_node_pool]
}

run "candidate_pool_rejects_different_cluster" {
  command = plan
  variables {
    isolated_node_pool = {
      cluster_name       = "different-cluster"
      node_role_name     = "existing-eks-node-role"
      subnet_ids         = ["subnet-example-private"]
      security_group_ids = ["sg-example-node"]
    }
  }
  expect_failures = [var.isolated_node_pool]
}

run "private_provider_endpoint_starts_denied" {
  command = plan
  variables {
    executor = null
    isolated_node_pool = {
      cluster_name       = "example-isolated"
      node_role_name     = "existing-eks-node-role"
      subnet_ids         = ["subnet-example-private"]
      security_group_ids = ["sg-example-node"]
    }
    provider_endpoint = {
      vpc_id     = "vpc-example"
      subnet_ids = ["subnet-example-private"]
    }
  }
  assert {
    condition = (
      aws_vpc_endpoint.provider["selected"].service_name == "com.amazonaws.us-east-1.transform-custom" &&
      !aws_vpc_endpoint.provider["selected"].private_dns_enabled &&
      aws_vpc_endpoint.provider["selected"].ip_address_type == "ipv4" &&
      jsondecode(aws_vpc_endpoint.provider["selected"].policy).Statement == [{
        Effect = "Deny", Principal = "*", Action = ["*"], Resource = ["*"]
      }] &&
      aws_vpc_security_group_ingress_rule.provider_endpoint["selected"].from_port == 443 &&
      aws_vpc_security_group_ingress_rule.provider_endpoint["selected"].to_port == 443 &&
      aws_vpc_security_group_ingress_rule.provider_endpoint["selected"].cidr_ipv4 == null &&
      aws_vpc_security_group_ingress_rule.provider_endpoint["selected"].cidr_ipv6 == null
    )
    error_message = "Private networking qualification must not authorize provider APIs or allow broad endpoint ingress."
  }
}

run "private_provider_endpoint_is_scoped_to_admitted_provider" {
  command = plan
  variables {
    executor = null
    isolated_node_pool = {
      cluster_name       = "example-isolated"
      node_role_name     = "existing-eks-node-role"
      subnet_ids         = ["subnet-example-private"]
      security_group_ids = ["sg-example-node"]
    }
    provider_endpoint = {
      vpc_id     = "vpc-example"
      subnet_ids = ["subnet-example-private"]
      authorizations = [{
        role_arn     = "arn:aws:iam::000000000000:role/selected-provider"
        package_arns = ["arn:aws:transform-custom:us-east-1:000000000000:package/selected-definition"]
      }]
    }
  }
  assert {
    condition = jsondecode(aws_vpc_endpoint.provider["selected"].policy).Statement == [
      {
        Effect    = "Allow", Principal = { AWS = "arn:aws:iam::000000000000:role/selected-provider" }
        Action    = ["transform-custom:GetTransformationPackageUrl", "transform-custom:ExecuteTransformation"]
        Resource  = ["arn:aws:transform-custom:us-east-1:000000000000:package/selected-definition"]
        Condition = { StringEquals = { "aws:RequestedRegion" = "us-east-1" } }
      },
      {
        Effect    = "Allow", Principal = { AWS = "arn:aws:iam::000000000000:role/selected-provider" }
        Action    = ["transform-custom:ConverseStream"]
        Resource  = ["*"]
        Condition = { StringEquals = { "aws:RequestedRegion" = "us-east-1" } }
      }
    ]
    error_message = "Only the selected provider role, exact package and documented resource-less stream action may use the endpoint."
  }
}

run "private_provider_endpoint_rejects_wildcard_identity" {
  command = plan
  variables {
    executor = null
    isolated_node_pool = {
      cluster_name       = "example-isolated"
      node_role_name     = "existing-eks-node-role"
      subnet_ids         = ["subnet-example-private"]
      security_group_ids = ["sg-example-node"]
    }
    provider_endpoint = {
      vpc_id     = "vpc-example"
      subnet_ids = ["subnet-example-private"]
      authorizations = [{
        role_arn     = "arn:aws:iam::000000000000:role/*"
        package_arns = ["arn:aws:transform-custom:us-east-1:000000000000:package/*"]
      }]
    }
  }
  expect_failures = [var.provider_endpoint]
}

run "deny_all_endpoint_cannot_take_over_vpc_dns" {
  command = plan
  variables {
    executor = null
    isolated_node_pool = {
      cluster_name       = "example-isolated"
      node_role_name     = "existing-eks-node-role"
      subnet_ids         = ["subnet-example-private"]
      security_group_ids = ["sg-example-node"]
    }
    provider_endpoint = {
      vpc_id              = "vpc-example"
      subnet_ids          = ["subnet-example-private"]
      private_dns_enabled = true
    }
  }
  expect_failures = [var.provider_endpoint]
}

run "endpoint_readback_becomes_protected_runtime_reference" {
  command = plan
  variables {
    isolated_node_pool = {
      cluster_name       = "example-isolated"
      node_role_name     = "existing-eks-node-role"
      subnet_ids         = ["subnet-example-private"]
      security_group_ids = ["sg-example-node"]
    }
    provider_endpoint = {
      vpc_id     = "vpc-aaaaaaaaaaaaaaaaa"
      subnet_ids = ["subnet-example-private"]
    }
  }
  override_resource {
    target = aws_vpc_endpoint.provider["selected"]
    values = {
      id = "vpce-bbbbbbbbbbbbbbbbb"
      dns_entry = [{
        dns_name       = "vpce-bbbbbbbbbbbbbbbbb-example1.transform-custom.us-east-1.vpce.amazonaws.com"
        hosted_zone_id = "ZEXAMPLE"
        }, {
        dns_name       = "vpce-bbbbbbbbbbbbbbbbb-example1-us-east-1a.transform-custom.us-east-1.vpce.amazonaws.com"
        hosted_zone_id = "ZEXAMPLE"
      }]
    }
    override_during = plan
  }
  assert {
    condition = jsondecode(output.worker_environment.L5_TRANSFORM_PROVIDER_ENDPOINT_REF) == {
      schema_version = 1
      account_id     = "000000000000"
      region         = "us-east-1"
      vpc_id         = "vpc-aaaaaaaaaaaaaaaaa"
      endpoint_id    = "vpce-bbbbbbbbbbbbbbbbb"
      service_name   = "com.amazonaws.us-east-1.transform-custom"
      dns_name       = "vpce-bbbbbbbbbbbbbbbbb-example1.transform-custom.us-east-1.vpce.amazonaws.com"
    }
    error_message = "Provider routing must come from the selected AWS endpoint readback and leave VPC DNS unchanged."
  }
}
