# Actual Terraform plans for optional remote-node/pod ranges; no provider I/O.
mock_provider "aws" {
  mock_data "aws_iam_session_context" {
    defaults = { issuer_arn = "arn:aws:iam::111122223333:role/workspace-provisioner" }
  }

  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b", "us-east-1c"] }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

mock_provider "tls" {
  mock_data "tls_certificate" {
    defaults = {
      certificates = [
        {
          sha1_fingerprint = "9e99a48a9960b14926bb7f3b02e22da2b0ab7280"
        }
      ]
    }
  }
}

override_data {
  target = data.aws_caller_identity.current
  values = {
    account_id = "111122223333"
    arn        = "arn:aws:sts::111122223333:assumed-role/workspace-provisioner/test"
  }
}

override_data {
  target = data.aws_partition.current
  values = {
    partition = "aws"
  }
}

variables {
  org_id          = "test-org"
  environment     = "dev"
  workspace_name  = "tenant-alpha"
  workspace_id    = "tenant-alpha"
  account_id      = "111122223333"
  aws_region      = "us-east-1"
  cluster_version = "1.31"

  networking_mode    = "owned"
  vpc_cidr           = "10.64.0.0/16"
  availability_zones = ["us-east-1a", "us-east-1b"]
}


run "native_workspace_does_not_enable_remote_networks" {
  command = plan
  assert {
    condition     = length(aws_eks_cluster.workspace.remote_network_config) == 0
    error_message = "Hybrid networking must be explicitly configured."
  }
}

run "hybrid_workspace_records_node_pod_and_service_ranges" {
  command = plan
  variables {
    hybrid_networks = {
      node_cidr    = "10.100.0.0/24"
      pod_cidr     = "10.101.0.0/16"
      service_cidr = "172.20.0.0/16"
    }
  }
  assert {
    condition = (
      aws_eks_cluster.workspace.remote_network_config[0].remote_node_networks[0].cidrs == toset(["10.100.0.0/24"]) &&
      aws_eks_cluster.workspace.remote_network_config[0].remote_pod_networks[0].cidrs == toset(["10.101.0.0/16"]) &&
      aws_eks_cluster.workspace.kubernetes_network_config[0].service_ipv4_cidr == "172.20.0.0/16" &&
      aws_eks_cluster.workspace.vpc_config[0].endpoint_private_access &&
      !aws_eks_cluster.workspace.vpc_config[0].endpoint_public_access
    )
    error_message = "The cluster must carry exactly the reviewed private hybrid ranges."
  }
}

run "remote_nodes_cannot_overlap_the_workspace_vpc" {
  command = plan
  variables {
    hybrid_networks = { node_cidr = "10.64.1.0/24", pod_cidr = "10.101.0.0/16", service_cidr = "172.20.0.0/16" }
  }
  expect_failures = [aws_eks_cluster.workspace]
}

run "remote_pods_cannot_overlap_nodes" {
  command = plan
  variables {
    hybrid_networks = { node_cidr = "10.100.1.0/24", pod_cidr = "10.100.0.0/16", service_cidr = "172.20.0.0/16" }
  }
  expect_failures = [aws_eks_cluster.workspace]
}

run "service_range_cannot_overlap_remote_pods" {
  command = plan
  variables {
    hybrid_networks = { node_cidr = "10.100.0.0/24", pod_cidr = "172.20.0.0/16", service_cidr = "172.20.0.0/16" }
  }
  expect_failures = [aws_eks_cluster.workspace]
}

run "public_remote_range_is_refused" {
  command = plan
  variables {
    hybrid_networks = { node_cidr = "203.0.113.0/24", pod_cidr = "10.101.0.0/16", service_cidr = "172.20.0.0/16" }
  }
  expect_failures = [var.hybrid_networks]
}

run "broad_provider_local_route_is_refused" {
  command = plan
  variables {
    hybrid_networks = { node_cidr = "10.0.0.0/8", pod_cidr = "172.21.0.0/16", service_cidr = "172.20.0.0/16" }
  }
  expect_failures = [var.hybrid_networks]
}
