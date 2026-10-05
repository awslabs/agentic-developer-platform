# =============================================================================
# Supplied networking is READ, never adopted — Issue #5532 (w6-09) design item 3, AC-01.
# =============================================================================
# "Support owned networking versus explicitly supplied networking without silently adopting
# its lifecycle."
#
# THE FAILURE THIS FILE EXISTS TO CATCH
#
# A customer lends ADP an existing VPC. ADP creates a workspace in it. Later the workspace is
# torn down — and the customer's VPC, subnets, route tables and NAT gateway go with it,
# because they were in the workspace module's state. Nobody chose that; it followed from the
# module having adopted resources it was only meant to read.
#
# So the assertion is about the RESOURCE GRAPH, not about intent: in supplied mode the count
# of every network resource must be zero. A resource absent from state cannot be destroyed by
# a destroy, whatever the operator types.
#
# `tests/test_networking_modes.py` is the companion check. This file proves the current
# module behaves correctly; that one proves a network resource added LATER without the gate
# is caught, which a plan test would only notice if someone remembered to extend it.
# =============================================================================

mock_provider "aws" {
  # The apply-only missing-STS-rule case must reach the actual data-source
  # postcondition. Random computed strings fail downstream ARN validation first,
  # and absent computed blocks cannot supply the cluster's OIDC/CA outputs.
  # These defaults supply provider results; they do not replace the guarded SG
  # data source or the real node-group dependency on that guard.
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111122223333:role/mock-workspace-role" }
  }

  mock_resource "aws_kms_key" {
    defaults = {
      arn    = "arn:aws:kms:us-east-1:111122223333:key/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
      key_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    }
  }

  mock_resource "aws_launch_template" {
    defaults = { id = "lt-0abcdef1234567890", latest_version = 1 }
  }

  mock_resource "aws_eks_cluster" {
    defaults = {
      arn                   = "arn:aws:eks:us-east-1:111122223333:cluster/mock-workspace"
      identity              = [{ oidc = [{ issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/TEST" }] }]
      certificate_authority = [{ data = "TU9DS0VEQ0VSVElGSUNBVEU=" }]
      vpc_config            = { cluster_security_group_id = "sg-02222222222222222" }
    }
  }

  mock_resource "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::111122223333:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/TEST" }
  }

  mock_data "aws_vpc_security_group_rules" {
    defaults = { ids = ["sgr-0123456789abcdef0"] }
  }

  mock_data "aws_vpc_endpoint" {
    defaults = {
      id                  = "vpce-0123456789abcdef0"
      private_dns_enabled = true
      security_group_ids  = ["sg-0123456789abcdef0"]
    }
  }

  mock_data "aws_route_tables" {
    defaults = { ids = ["rtb-0123456789abcdef0"] }
  }
  mock_data "aws_route_table" {
    defaults = {
      id     = "rtb-0123456789abcdef0"
      vpc_id = "vpc-0a1b2c3d4e5f67890"
      routes = [{ cidr_block = "0.0.0.0/0", nat_gateway_id = "nat-0123456789abcdef0", gateway_id = "" }]
    }
  }
  mock_data "aws_nat_gateway" {
    defaults = {
      id                = "nat-0123456789abcdef0"
      vpc_id            = "vpc-0a1b2c3d4e5f67890"
      subnet_id         = "subnet-0ccccccccccccccc3"
      connectivity_type = "public"
      state             = "available"
    }
  }
  mock_data "aws_internet_gateway" {
    defaults = {
      id          = "igw-0123456789abcdef0"
      attachments = [{ vpc_id = "vpc-0a1b2c3d4e5f67890", state = "available" }]
    }
  }

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

# The supplied VPC. Overridden rather than mocked loosely, so the id the module reads is the
# id the test supplied and `vpc_id` below is a real assertion rather than a tautology on a
# generated mock value.
override_data {
  target = data.aws_vpc.supplied[0]
  values = {
    id                   = "vpc-0a1b2c3d4e5f67890"
    cidr_block           = "172.31.0.0/16"
    enable_dns_support   = true
    enable_dns_hostnames = true
  }
}

variables {
  org_id          = "test-org"
  environment     = "dev"
  workspace_name  = "tenant-byo"
  workspace_id    = "tenant-byo"
  account_id      = "111122223333"
  aws_region      = "us-east-1"
  cluster_version = "1.31"

  networking_mode = "supplied"
  supplied_vpc_id = "vpc-0a1b2c3d4e5f67890"
  supplied_private_subnet_ids = [
    "subnet-0aaaaaaaaaaaaaaa1",
    "subnet-0bbbbbbbbbbbbbbb2",
  ]
}

run "supplied_mode_declares_no_network_resource_at_all" {
  command = plan

  assert {
    condition     = length(aws_default_security_group.workspace) == 0
    error_message = "Supplied mode must never adopt or revoke rules from the customer's default security group."
  }

  # The core assertion of this file. Each of these is a resource whose destruction would
  # damage a network ADP does not own.
  assert {
    condition     = length(aws_vpc.workspace) == 0
    error_message = "Supplied mode must not declare a VPC: a VPC in this module's state is a VPC a workspace destroy deletes, and this one belongs to its supplier."
  }

  assert {
    condition     = length(aws_subnet.private) == 0 && length(aws_subnet.public) == 0
    error_message = "Supplied mode must not declare subnets: the supplied subnets already exist and are their owner's."
  }

  assert {
    condition     = length(aws_nat_gateway.workspace) == 0
    error_message = "Supplied mode must not declare a NAT gateway: deleting the supplier's egress path would break every other workload in their VPC, not just this workspace."
  }

  assert {
    condition     = length(aws_internet_gateway.workspace) == 0
    error_message = "Supplied mode must not declare an internet gateway."
  }

  assert {
    condition     = length(aws_route_table.private) == 0 && length(aws_route_table.public) == 0
    error_message = "Supplied mode must not declare route tables: replacing a supplied VPC's routes would redirect traffic that is not this workspace's."
  }

  assert {
    condition     = length(aws_route_table_association.private) == 0 && length(aws_route_table_association.public) == 0
    error_message = "Supplied mode must not declare route table associations."
  }

  assert {
    condition     = length(aws_eip.nat) == 0
    error_message = "Supplied mode must not allocate an Elastic IP: egress is the supplier's."
  }
}

run "supplied_mode_still_produces_a_working_cluster_in_the_supplied_network" {
  command = plan

  # The other half of the requirement, and the reason "declare nothing" is not a sufficient
  # implementation: reading the network has to actually work. A module that refused to
  # declare network resources AND failed to place the cluster would pass the run above.
  assert {
    condition     = output.vpc_id == "vpc-0a1b2c3d4e5f67890"
    error_message = "The cluster must resolve the supplied VPC id by reading it. Got: ${output.vpc_id}"
  }

  assert {
    condition = (
      length(aws_eks_node_group.default.subnet_ids) == 2 &&
      contains(aws_eks_node_group.default.subnet_ids, "subnet-0aaaaaaaaaaaaaaa1") &&
      contains(aws_eks_node_group.default.subnet_ids, "subnet-0bbbbbbbbbbbbbbb2")
    )
    error_message = "Nodes must be placed in exactly the supplied private subnets, not in subnets this module invented."
  }

  assert {
    condition     = aws_security_group.cluster.vpc_id == "vpc-0a1b2c3d4e5f67890"
    error_message = "The cluster security group must be created in the supplied VPC. (A security group ADP creates IS ADP's to destroy — it is additive and its removal does not damage the supplied network.)"
  }
}

run "supplied_mode_is_recorded_as_supplied_on_the_resources_and_in_the_outputs" {
  command = plan

  # Ownership has to be answerable WITHOUT reconstructing the request, because the person
  # running a teardown months later has the state and the console, not the tfvars.
  assert {
    condition     = output.network_ownership == "supplied"
    error_message = "network_ownership must report \"supplied\" so a teardown can tell, before it acts, that the network is not ADP's to delete. Got: ${output.network_ownership}"
  }

  assert {
    condition     = output.nat_gateway_id == null
    error_message = "nat_gateway_id must be null in supplied mode — distinguishable from an id, so a consumer cannot treat \"not ours\" as a resource we manage."
  }

  assert {
    condition     = length(output.public_subnet_ids) == 0
    error_message = "public_subnet_ids must be empty in supplied mode: this module makes no claim about a supplied network's public subnets because it did not create or verify them."
  }
}

run "owned_mode_declares_the_network_and_says_so" {
  command = plan

  variables {
    environment     = "dev"
    workspace_name  = "tenant-owned"
    workspace_id    = "tenant-owned"
    account_id      = "111122223333"
    aws_region      = "us-east-1"
    cluster_version = "1.31"

    networking_mode             = "owned"
    vpc_cidr                    = "10.64.0.0/16"
    availability_zones          = ["us-east-1a", "us-east-1b", "us-east-1c"]
    supplied_vpc_id             = ""
    supplied_private_subnet_ids = []
  }

  assert {
    condition     = length(aws_default_security_group.workspace) == 1
    error_message = "Owned mode must adopt exactly one default security group."
  }

  assert {
    condition     = length(aws_default_security_group.workspace[0].ingress) == 0 && length(aws_default_security_group.workspace[0].egress) == 0
    error_message = "Owned default security groups must plan explicit empty ingress and egress sets."
  }

  # THE ANTI-VACUOUS HALF. Every assertion in the first run is a count-is-zero check, and all
  # of them would pass against a module that declares no network resources in EITHER mode —
  # i.e. against a module that is simply broken. This run proves the gates distinguish the
  # modes rather than being permanently off.
  assert {
    condition     = length(aws_vpc.workspace) == 1
    error_message = "Owned mode must create exactly one VPC. Without this, the supplied-mode zero-counts prove nothing."
  }

  assert {
    condition     = length(aws_subnet.private) == 3 && length(aws_subnet.public) == 3
    error_message = "Owned mode must create one private and one public subnet per availability zone."
  }

  assert {
    condition     = length(aws_nat_gateway.workspace) == 1
    error_message = "Owned mode must create exactly one NAT gateway (see network.tf for why one rather than one per zone, and what fails when its zone does)."
  }

  assert {
    condition     = output.network_ownership == "adp-created"
    error_message = "network_ownership must report \"adp-created\" in owned mode. Got: ${output.network_ownership}"
  }

  assert {
    condition = (
      aws_subnet.private[0].cidr_block == "10.64.128.0/20" &&
      aws_subnet.public[0].cidr_block == "10.64.0.0/20"
    )
    error_message = "Subnet CIDRs must be a deterministic carve of the VPC CIDR: public in the first half, private offset into the second. A non-deterministic layout means a plan diff can show a subnet moving, and moving a subnet drains every node in it."
  }
}

override_data {
  target = data.aws_subnet.supplied["subnet-0aaaaaaaaaaaaaaa1"]
  values = { vpc_id = "vpc-0a1b2c3d4e5f67890", availability_zone = "us-east-1a", map_public_ip_on_launch = false }
}
override_data {
  target = data.aws_subnet.supplied["subnet-0bbbbbbbbbbbbbbb2"]
  values = { vpc_id = "vpc-0a1b2c3d4e5f67890", availability_zone = "us-east-1b", map_public_ip_on_launch = false }
}

run "supplied_subnet_in_another_vpc_is_refused" {
  command = plan
  override_data {
    target = data.aws_subnet.supplied["subnet-0bbbbbbbbbbbbbbb2"]
    values = { vpc_id = "vpc-09999999999999999", availability_zone = "us-east-1b", map_public_ip_on_launch = false }
  }
  expect_failures = [terraform_data.topology_guard]
}
run "supplied_subnets_in_one_zone_are_refused" {
  command = plan
  override_data {
    target = data.aws_subnet.supplied["subnet-0bbbbbbbbbbbbbbb2"]
    values = { vpc_id = "vpc-0a1b2c3d4e5f67890", availability_zone = "us-east-1a", map_public_ip_on_launch = false }
  }
  expect_failures = [terraform_data.topology_guard]
}
run "supplied_subnet_in_another_region_is_refused" {
  command = plan
  override_data {
    target = data.aws_subnet.supplied["subnet-0bbbbbbbbbbbbbbb2"]
    values = { vpc_id = "vpc-0a1b2c3d4e5f67890", availability_zone = "us-west-2b", map_public_ip_on_launch = false }
  }
  expect_failures = [terraform_data.topology_guard]
}

override_data {
  target = data.aws_route_table.supplied_nat["nat-0123456789abcdef0"]
  values = {
    id     = "rtb-0fedcba9876543210"
    vpc_id = "vpc-0a1b2c3d4e5f67890"
    routes = [{ cidr_block = "0.0.0.0/0", gateway_id = "igw-0123456789abcdef0", nat_gateway_id = "" }]
  }
}

run "automatic_public_address_is_refused" {
  command = plan
  override_data {
    target = data.aws_subnet.supplied["subnet-0aaaaaaaaaaaaaaa1"]
    values = { vpc_id = "vpc-0a1b2c3d4e5f67890", availability_zone = "us-east-1a", map_public_ip_on_launch = true }
  }
  expect_failures = [terraform_data.topology_guard]
}

run "disabled_dns_support_is_refused" {
  command = plan
  override_data {
    target = data.aws_vpc.supplied[0]
    values = { id = "vpc-0a1b2c3d4e5f67890", enable_dns_support = false, enable_dns_hostnames = true }
  }
  expect_failures = [terraform_data.topology_guard]
}

run "disabled_dns_hostnames_is_refused" {
  command = plan
  override_data {
    target = data.aws_vpc.supplied[0]
    values = { id = "vpc-0a1b2c3d4e5f67890", enable_dns_support = true, enable_dns_hostnames = false }
  }
  expect_failures = [terraform_data.topology_guard]
}

run "public_node_route_is_refused" {
  command = plan
  override_data {
    target = data.aws_route_table.supplied_nodes["subnet-0aaaaaaaaaaaaaaa1"]
    values = { id = "rtb-0123456789abcdef0", vpc_id = "vpc-0a1b2c3d4e5f67890", routes = [{ cidr_block = "0.0.0.0/0", gateway_id = "igw-0123456789abcdef0", nat_gateway_id = "" }] }
  }
  expect_failures = [terraform_data.topology_guard]
}

run "no_egress_is_refused" {
  command = plan
  override_data {
    target = data.aws_route_table.supplied_nodes["subnet-0aaaaaaaaaaaaaaa1"]
    values = { id = "rtb-0123456789abcdef0", vpc_id = "vpc-0a1b2c3d4e5f67890", routes = [] }
  }
  expect_failures = [terraform_data.topology_guard]
}

run "nat_unavailable_is_refused" {
  command = plan
  override_data {
    target = data.aws_nat_gateway.supplied["nat-0123456789abcdef0"]
    values = { id = "nat-0123456789abcdef0", vpc_id = "vpc-0a1b2c3d4e5f67890", subnet_id = "subnet-0ccccccccccccccc3", state = "failed", connectivity_type = "public" }
  }
  expect_failures = [terraform_data.topology_guard]
}

run "private_nat_is_refused" {
  command = plan
  override_data {
    target = data.aws_nat_gateway.supplied["nat-0123456789abcdef0"]
    values = { id = "nat-0123456789abcdef0", vpc_id = "vpc-0a1b2c3d4e5f67890", subnet_id = "subnet-0ccccccccccccccc3", state = "available", connectivity_type = "private" }
  }
  expect_failures = [terraform_data.topology_guard]
}

run "nat_without_internet_route_is_refused" {
  command = plan
  override_data {
    target = data.aws_route_table.supplied_nat["nat-0123456789abcdef0"]
    values = { id = "rtb-0fedcba9876543210", vpc_id = "vpc-0a1b2c3d4e5f67890", routes = [] }
  }
  expect_failures = [terraform_data.topology_guard]
}
run "internet_gateway_attached_to_other_vpc_is_refused" {
  command = plan
  override_data {
    target = data.aws_internet_gateway.supplied["igw-0123456789abcdef0"]
    values = { id = "igw-0123456789abcdef0", attachments = [{ vpc_id = "vpc-09999999999999999", state = "available" }] }
  }
  expect_failures = [terraform_data.topology_guard]
}
run "main_route_table_is_used_without_explicit_association" {
  command = plan
  override_data {
    target = data.aws_route_tables.supplied_explicit["subnet-0aaaaaaaaaaaaaaa1"]
    values = { ids = [] }
  }
  assert {
    condition     = data.aws_route_table.supplied_nodes["subnet-0aaaaaaaaaaaaaaa1"].route_table_id == data.aws_route_table.supplied_main[0].id
    error_message = "A subnet without an explicit association must use its VPC main route table."
  }
}

run "supplied_private_sts_is_retained" {
  command = plan
  assert {
    condition     = length(aws_vpc_endpoint.private_sts) == 0 && length(aws_security_group.private_sts) == 0 && length(aws_vpc_security_group_ingress_rule.private_sts_nodes) == 0
    error_message = "Supplied private STS and its security group must never be adopted into workspace state."
  }
  assert {
    condition     = output.sts_endpoint_id == "vpce-0123456789abcdef0" && output.sts_endpoint_security_group_id == "sg-0123456789abcdef0"
    error_message = "Bootstrap must receive the exact supplied private STS identities."
  }
}

run "supplied_sts_without_private_dns_is_refused" {
  command = plan
  override_data {
    target = data.aws_vpc_endpoint.supplied_sts[0]
    values = { private_dns_enabled = false, security_group_ids = ["sg-0123456789abcdef0"] }
  }
  expect_failures = [data.aws_vpc_endpoint.supplied_sts]
}

run "supplied_sts_without_node_ingress_stops_before_nodes" {
  command = apply
  override_data {
    target = data.aws_security_group.supplied_sts[0]
    values = { arn = "arn:aws:ec2:us-east-1:111122223333:security-group/sg-0123456789abcdef0", id = "sg-0123456789abcdef0", vpc_id = "vpc-0a1b2c3d4e5f67890" }
  }
  override_data {
    target = data.aws_vpc_security_group_rules.supplied_sts[0]
    values = { ids = [] }
  }
  expect_failures = [data.aws_security_group.supplied_sts]
}


run "hybrid_ranges_cannot_overlap_a_secondary_supplied_vpc_range" {
  command = plan
  variables {
    hybrid_networks = { node_cidr = "10.100.0.0/24", pod_cidr = "10.101.0.0/16", service_cidr = "172.20.0.0/16" }
  }
  override_data {
    target = data.aws_vpc.supplied[0]
    values = {
      id                   = "vpc-0a1b2c3d4e5f67890"
      cidr_block           = "172.31.0.0/16"
      enable_dns_support   = true
      enable_dns_hostnames = true
      cidr_block_associations = [{
        association_id = "vpc-cidr-assoc-0123456789abcdef0"
        cidr_block     = "10.100.0.0/16"
        state          = "associated"
      }]
    }
  }
  expect_failures = [aws_eks_cluster.workspace]
}
