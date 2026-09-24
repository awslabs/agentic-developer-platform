# Additional existing private capacity subnets (#5830).
#
# The incident this guards: the cluster's original private subnets exhausted
# their IP addresses, so the CNI failed every new pod with "failed to assign an
# IP address to container". The fix widens the CLUSTER's own subnet set, because
# Auto Mode's AWS-managed `default` NodeClass takes its subnets from
# resourcesVpcConfig.subnetIds and must not be edited.
#
# What must hold, and why each run below exists:
#   - default unchanged: every other account, and this one until an operator opts
#     in, must plan exactly the subnet set it has today
#   - additive: the original subnets are never dropped in exchange for new ones
#   - refusal: a wrong-VPC / wrong-AZ / public / internet-gateway-routed subnet
#     must fail the PLAN, because applying any of them turns a capacity fix into
#     an outage or silent loss of zone coverage
mock_provider "aws" {}
mock_provider "kubernetes" {}
mock_provider "tls" {}

variables {
  environment             = "dev"
  name_prefix             = "adp-dev"
  vpc_id                  = "vpc-00000000000000000"
  private_subnet_ids      = ["subnet-00000000000000001", "subnet-00000000000000002"]
  eks_security_group_id   = "sg-00000000000000000"
  eks_cluster_role_arn    = "arn:aws:iam::123456789012:role/adp-dev-role-eks-cluster"
  node_group_role_arn     = "arn:aws:iam::123456789012:role/adp-dev-role-eks-node-group"
  eks_public_access_cidrs = ["10.0.0.0/8"]

  private_subnet_availability_zones = ["us-east-1a", "us-east-1b"]
}

# The mock EKS cluster returns an empty identity list, which the OIDC locals and
# IRSA trust policies index into. Same shim the endpoint_access test uses.
override_resource {
  target = aws_eks_cluster.main
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
  target = data.tls_certificate.cluster
  values = {
    certificates = [{
      sha1_fingerprint = "0123456789abcdef0123456789abcdef01234567"
    }]
  }
}

# A valid pair: two already-existing private subnets, one per existing AZ, in
# this VPC, no public IPs, NAT-routed. Overrides are per-key so a single run can
# make exactly one property wrong below.
override_data {
  target = data.aws_subnet.additional_private["us-east-1a"]
  values = {
    vpc_id                  = "vpc-00000000000000000"
    availability_zone       = "us-east-1a"
    map_public_ip_on_launch = false
  }
}

override_data {
  target = data.aws_subnet.additional_private["us-east-1b"]
  values = {
    vpc_id                  = "vpc-00000000000000000"
    availability_zone       = "us-east-1b"
    map_public_ip_on_launch = false
  }
}

override_data {
  target = data.aws_route_table.additional_private["us-east-1a"]
  values = {
    routes = [{ cidr_block = "0.0.0.0/0", nat_gateway_id = "nat-00000000000000001", gateway_id = "" }]
  }
}

override_data {
  target = data.aws_route_table.additional_private["us-east-1b"]
  values = {
    routes = [{ cidr_block = "0.0.0.0/0", nat_gateway_id = "nat-00000000000000001", gateway_id = "" }]
  }
}

# ---------------------------------------------------------------------------
# Unchanged default
# ---------------------------------------------------------------------------

run "default_leaves_the_cluster_subnet_set_unchanged" {
  command = plan

  # No additional_private_subnet_ids_by_az: this is what every other account and
  # every un-opted-in environment plans. If this ever differs from
  # private_subnet_ids, the opt-in has stopped being opt-in.
  # Compared as sets: subnet_ids is a set, so it carries membership but no order.
  assert {
    condition     = aws_eks_cluster.main.vpc_config[0].subnet_ids == toset(var.private_subnet_ids)
    error_message = "With no additional subnets configured, the cluster's subnet set must be exactly the existing private subnets."
  }

  assert {
    condition     = length(aws_eks_cluster.main.vpc_config[0].subnet_ids) == length(var.private_subnet_ids)
    error_message = "An un-opted-in plan must not add or drop a subnet."
  }
}

run "default_reads_no_subnet_or_route_table" {
  command = plan

  # The validation data sources must not be instantiated when the feature is
  # unused: an un-opted-in plan must not acquire new read dependencies, and must
  # not be able to fail on a subnet lookup it has no reason to perform.
  assert {
    condition     = length(data.aws_subnet.additional_private) == 0 && length(data.aws_route_table.additional_private) == 0
    error_message = "An un-opted-in plan must not read any additional subnet or route table."
  }
}

# ---------------------------------------------------------------------------
# Additive opt-in
# ---------------------------------------------------------------------------

run "valid_additions_are_appended_and_existing_subnets_preserved" {
  command = plan

  variables {
    additional_private_subnet_ids_by_az = {
      "us-east-1a" = "subnet-0aaaaaaaaaaaaaaa1"
      "us-east-1b" = "subnet-0bbbbbbbbbbbbbbb2"
    }
  }

  # The whole point of the change: more capacity, same existing capacity.
  assert {
    condition = alltrue([
      for id in var.private_subnet_ids : contains(aws_eks_cluster.main.vpc_config[0].subnet_ids, id)
    ])
    error_message = "Both original private subnets must remain in the cluster's subnet set; this change adds capacity, it never replaces it."
  }

  assert {
    condition = alltrue([
      for id in ["subnet-0aaaaaaaaaaaaaaa1", "subnet-0bbbbbbbbbbbbbbb2"] :
      contains(aws_eks_cluster.main.vpc_config[0].subnet_ids, id)
    ])
    error_message = "Each supplied additional subnet must be present in the cluster's subnet set."
  }

  assert {
    condition     = length(aws_eks_cluster.main.vpc_config[0].subnet_ids) == 4
    error_message = "Two additions to two existing subnets must yield exactly four subnets — no drops and no duplicates."
  }
}

run "a_single_addition_does_not_disturb_the_existing_subnets" {
  command = plan

  variables {
    additional_private_subnet_ids_by_az = {
      "us-east-1a" = "subnet-0aaaaaaaaaaaaaaa1"
    }
  }

  # Relieving one exhausted zone must not cost coverage in the other: the result
  # is exactly the existing subnets plus the one addition, nothing swapped out.
  assert {
    condition = aws_eks_cluster.main.vpc_config[0].subnet_ids == toset(
      concat(var.private_subnet_ids, ["subnet-0aaaaaaaaaaaaaaa1"])
    )
    error_message = "A single addition must append to the existing private subnets, leaving their membership intact."
  }

  assert {
    condition     = length(aws_eks_cluster.main.vpc_config[0].subnet_ids) == 3
    error_message = "One addition to two existing subnets must yield exactly three subnets."
  }
}

# ---------------------------------------------------------------------------
# Refusals — each of these applied would be an incident, so each must fail plan
# ---------------------------------------------------------------------------

run "subnet_in_another_vpc_is_refused" {
  command = plan

  variables {
    additional_private_subnet_ids_by_az = {
      "us-east-1a" = "subnet-0aaaaaaaaaaaaaaa1"
    }
  }

  override_data {
    target = data.aws_subnet.additional_private["us-east-1a"]
    values = {
      vpc_id                  = "vpc-ffffffffffffffff"
      availability_zone       = "us-east-1a"
      map_public_ip_on_launch = false
    }
  }

  expect_failures = [data.aws_subnet.additional_private]
}

run "subnet_in_a_different_az_than_its_key_is_refused" {
  command = plan

  variables {
    additional_private_subnet_ids_by_az = {
      "us-east-1a" = "subnet-0aaaaaaaaaaaaaaa1"
    }
  }

  # Accepting this would quietly collapse the added capacity into one zone while
  # the configuration claims two.
  override_data {
    target = data.aws_subnet.additional_private["us-east-1a"]
    values = {
      vpc_id                  = "vpc-00000000000000000"
      availability_zone       = "us-east-1b"
      map_public_ip_on_launch = false
    }
  }

  expect_failures = [data.aws_subnet.additional_private]
}

run "public_ip_assigning_subnet_is_refused" {
  command = plan

  variables {
    additional_private_subnet_ids_by_az = {
      "us-east-1a" = "subnet-0aaaaaaaaaaaaaaa1"
    }
  }

  override_data {
    target = data.aws_subnet.additional_private["us-east-1a"]
    values = {
      vpc_id                  = "vpc-00000000000000000"
      availability_zone       = "us-east-1a"
      map_public_ip_on_launch = true
    }
  }

  expect_failures = [data.aws_subnet.additional_private]
}

run "subnet_in_an_az_without_existing_capacity_is_refused" {
  command = plan

  variables {
    additional_private_subnet_ids_by_az = {
      "us-east-1c" = "subnet-0ccccccccccccccc3"
    }
  }

  override_data {
    target = data.aws_subnet.additional_private["us-east-1c"]
    values = {
      vpc_id                  = "vpc-00000000000000000"
      availability_zone       = "us-east-1c"
      map_public_ip_on_launch = false
    }
  }

  override_data {
    target = data.aws_route_table.additional_private["us-east-1c"]
    values = {
      routes = [{ cidr_block = "0.0.0.0/0", nat_gateway_id = "nat-00000000000000001", gateway_id = "" }]
    }
  }

  expect_failures = [data.aws_subnet.additional_private]
}

run "internet_gateway_routed_subnet_is_refused" {
  command = plan

  variables {
    additional_private_subnet_ids_by_az = {
      "us-east-1a" = "subnet-0aaaaaaaaaaaaaaa1"
    }
  }

  # Tags can say "private"; the default route is what decides.
  override_data {
    target = data.aws_route_table.additional_private["us-east-1a"]
    values = {
      routes = [{ cidr_block = "0.0.0.0/0", nat_gateway_id = "", gateway_id = "igw-00000000000000001" }]
    }
  }

  expect_failures = [data.aws_route_table.additional_private]
}

run "subnet_without_a_default_route_is_refused" {
  command = plan

  variables {
    additional_private_subnet_ids_by_az = {
      "us-east-1a" = "subnet-0aaaaaaaaaaaaaaa1"
    }
  }

  # Nodes launched here could not reach the control plane or pull images, so they
  # would fail to join rather than relieve the exhaustion.
  override_data {
    target = data.aws_route_table.additional_private["us-east-1a"]
    values = {
      routes = [{ cidr_block = "10.0.0.0/16", nat_gateway_id = "", gateway_id = "" }]
    }
  }

  expect_failures = [data.aws_route_table.additional_private]
}

run "a_malformed_subnet_id_is_refused_before_any_lookup" {
  command = plan

  variables {
    additional_private_subnet_ids_by_az = {
      "us-east-1a" = "vpc-0aaaaaaaaaaaaaaa1"
    }
  }

  expect_failures = [var.additional_private_subnet_ids_by_az]
}

run "the_same_subnet_under_two_azs_is_refused" {
  command = plan

  variables {
    additional_private_subnet_ids_by_az = {
      "us-east-1a" = "subnet-0aaaaaaaaaaaaaaa1"
      "us-east-1b" = "subnet-0aaaaaaaaaaaaaaa1"
    }
  }

  expect_failures = [var.additional_private_subnet_ids_by_az]
}
