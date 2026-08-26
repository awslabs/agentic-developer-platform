# Tests for internal-plane ALB separation (Issue #4010)
#
# The `/internal/{proxy+}` route must target a SEPARATE ALB from the one
# CloudFront fronts, so the internal control plane is unreachable from the edge
# by routing. These tests lock in the two properties that make that safe:
#
#  1. FALLBACK — when the internal-plane vars are unset the route still points at
#     the edge ALB (pre-#4010 behavior). This is what lets the Terraform merge
#     ahead of the cluster-side rollout without a window where /internal 503s.
#  2. SEPARATION — when they ARE set, the internal route points at the internal
#     ALB while the public routes stay on the edge ALB. A regression that wired
#     both to one ALB would silently remove the separation this issue exists to
#     create, which is exactly the kind of failure the issue warned "must be
#     tested, not assumed".

variables {
  environment        = "dev"
  name_prefix        = "bedrockgw-dev"
  aws_region         = "us-east-1"
  vpc_id             = "vpc-0123456789abcdef0"
  private_subnet_ids = ["subnet-0aaa", "subnet-0bbb"]

  internal_alb_arn = "arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/edge-alb/abc123"
  internal_alb_dns = "internal-edge-alb-1.us-east-1.elb.amazonaws.com"
}

# Test: with the internal-plane vars unset, the internal route falls back to the
# edge ALB — i.e. merging this change alone does not move any traffic.
run "internal_route_falls_back_to_edge_alb_when_unset" {
  command = plan

  variables {
    internal_plane_alb_arn = ""
    internal_plane_alb_dns = ""
  }

  assert {
    condition     = local.internal_plane_alb_dns == var.internal_alb_dns
    error_message = "With internal_plane_alb_dns unset, the internal plane must fall back to the edge ALB DNS (pre-#4010 behavior)."
  }

  assert {
    condition     = local.internal_plane_alb_arn == var.internal_alb_arn
    error_message = "With internal_plane_alb_arn unset, the internal plane must fall back to the edge ALB ARN (pre-#4010 behavior)."
  }
}

# Test: with the internal-plane vars set, the internal route targets the internal
# ALB and is genuinely distinct from the edge ALB.
run "internal_route_targets_internal_plane_alb_when_set" {
  command = plan

  variables {
    internal_plane_alb_arn = "arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/internal-alb/def456"
    internal_plane_alb_dns = "internal-internal-plane-alb-2.us-east-1.elb.amazonaws.com"
  }

  assert {
    condition     = local.internal_plane_alb_dns == "internal-internal-plane-alb-2.us-east-1.elb.amazonaws.com"
    error_message = "The internal plane must route to the internal-plane ALB DNS when it is set."
  }

  # The core security property: the two planes must not share a load balancer.
  assert {
    condition     = local.internal_plane_alb_dns != var.internal_alb_dns
    error_message = "Internal-plane and edge ALB DNS must differ — otherwise the #4010 separation does not exist."
  }

  assert {
    condition     = local.internal_plane_alb_arn != var.internal_alb_arn
    error_message = "Internal-plane and edge ALB ARN must differ — otherwise the #4010 separation does not exist."
  }
}

# Test: a partially-populated config (DNS set, ARN empty) must not silently
# produce a half-wired integration. integrationTarget must be a LOAD BALANCER
# ARN, and an empty one would be rejected at apply time, so the ARN falls back to
# the edge ALB rather than being emitted empty.
run "internal_route_arn_falls_back_when_only_dns_set" {
  command = plan

  variables {
    internal_plane_alb_arn = ""
    internal_plane_alb_dns = "internal-internal-plane-alb-2.us-east-1.elb.amazonaws.com"
  }

  assert {
    condition     = local.internal_plane_alb_arn == var.internal_alb_arn
    error_message = "An empty internal_plane_alb_arn must fall back to the edge ALB ARN, never to an empty integrationTarget."
  }
}

# Test: the VPC Link SG gets the reciprocal ingress rule on the internal-plane
# ALB's SG. Both directions are required — the #4010 spike showed that opening
# only one side leaves the connection silently dropped (~10s timeout, then 503)
# rather than refused.
run "internal_plane_alb_gets_vpc_link_ingress_rule" {
  command = plan

  variables {
    internal_plane_alb_arn                = "arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/internal-alb/def456"
    internal_plane_alb_dns                = "internal-internal-plane-alb-2.us-east-1.elb.amazonaws.com"
    internal_plane_alb_security_group_ids = ["sg-0internalplane"]
  }

  assert {
    condition     = length(aws_security_group_rule.internal_plane_alb_from_vpc_link) == 1
    error_message = "The internal-plane ALB SG must get an ingress rule from the VPC Link SG."
  }

  assert {
    condition     = aws_security_group_rule.internal_plane_alb_from_vpc_link[0].from_port == 80
    error_message = "The internal-plane ALB ingress rule must be on port 80 (the internal Ingress listens on 80)."
  }
}

# Test: no internal-plane SG rules are created when no SG IDs are supplied, so
# clusters without ingress-internal.yaml plan identically to today.
run "no_internal_plane_sg_rules_when_unset" {
  command = plan

  variables {
    internal_plane_alb_security_group_ids = []
  }

  assert {
    condition     = length(aws_security_group_rule.internal_plane_alb_from_vpc_link) == 0
    error_message = "No internal-plane SG rules should be created when internal_plane_alb_security_group_ids is empty."
  }
}
