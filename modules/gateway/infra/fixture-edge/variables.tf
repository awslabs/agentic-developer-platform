# =============================================================================
# Inputs — Wave 2 fixture trusted edge (Issue #5836)
# =============================================================================
# Every input is either (a) bound to one fixture run, or (b) a discovered fact
# about the target environment that the operator must pass explicitly. There are
# deliberately NO defaults for account/region/nonce: a fixture edge that can be
# created without naming its target is one that can be created against the wrong
# target, and the whole point of this component is that it is disposable and
# provably scoped.
# =============================================================================

variable "fixture_edge_enabled" {
  description = <<-EOT
    Master switch. FALSE (the default) means this component creates NOTHING —
    every resource is gated on it.

    This default is what makes the component safe to merge ahead of any decision
    to run it: a plan/apply with no variables set is empty, so merging cannot
    move traffic, cost money (the fixture ALB is the real cost) or create an
    edge that injects trusted headers. Turning it on is an explicit, reviewable
    act by root at execution time.
  EOT
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Run binding — nonce / account / region
# ---------------------------------------------------------------------------
# Issue #5836 requires all inputs, outputs and resources to be bound to the run
# nonce, account and region. These three are validated rather than trusted,
# because the failure they prevent is a fixture created against the wrong
# account, whose teardown would then "clean up" resources it does not own.

variable "run_nonce" {
  description = <<-EOT
    Per-run identifier from the #3968 ownership ledger (lib/ownership.py new_nonce()).
    Bounds every resource name and tag so two runs cannot collide and so teardown
    can prove which run created a resource.

    Lower-case hex, 8-32 chars, clock-independent by construction upstream.
  EOT
  type        = string

  validation {
    # Rejects an empty/placeholder nonce. A name prefix with an unvalidated
    # nonce is how an "owned" resource ends up sharing a name with somebody
    # else's — the exact failure lib/ownership.py was written to stop.
    condition     = can(regex("^[0-9a-f]{8,32}$", var.run_nonce))
    error_message = "run_nonce must be 8-32 lower-case hex characters (see #3968 lib/ownership.py new_nonce())."
  }
}

variable "expected_account_id" {
  description = <<-EOT
    The 12-digit AWS account this fixture is authorized for (#5836: 879318057152).
    Checked against the CALLER'S REAL IDENTITY at plan time, so a correct-looking
    tfvars file cannot create a fixture edge in an unintended account.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_account_id))
    error_message = "expected_account_id must be exactly 12 digits."
  }
}

variable "aws_region" {
  description = "Region the fixture edge is created in. Verified against the provider's resolved region."
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]$", var.aws_region))
    error_message = "aws_region must be an AWS region such as us-east-1."
  }
}

variable "environment" {
  description = "Target environment name (e.g. dev). Used in names, tags and the fixture's own SSM path."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9-]{1,16}$", var.environment))
    error_message = "environment must be 1-16 lower-case alphanumeric/dash characters."
  }
}

# ---------------------------------------------------------------------------
# Fixture backend — where the trusted edge forwards to
# ---------------------------------------------------------------------------

variable "fixture_alb_arn" {
  description = <<-EOT
    LOAD BALANCER ARN (never a listener ARN) of the FIXTURE's own internal ALB,
    fronting the fixture gateway pods.

    It must be a separate ALB from the ordinary internal-plane ALB: on this EKS
    Auto Mode cluster a second Ingress cannot join an existing ALB (group.name is
    a documented no-op there), so sharing is not merely discouraged, it is
    impossible — see the #3968 FIXTURE-ROUTING-CONSTRAINT.md evidence.

    The "listener ARN is rejected" behaviour is verified upstream against the live
    API; see docs/design-notes/4010-internal-plane-alb-separation.md.
  EOT
  type        = string

  validation {
    condition = can(regex(
      "^arn:aws:elasticloadbalancing:[a-z0-9-]+:[0-9]{12}:loadbalancer/(app|net)/[^/]+/[0-9a-f]+$",
      var.fixture_alb_arn
    ))
    error_message = "fixture_alb_arn must be a load balancer ARN (arn:aws:elasticloadbalancing:...:loadbalancer/app/NAME/ID), not a listener ARN."
  }
}

# NOTE: there is deliberately NO fixture_alb_dns input.
#
# An earlier revision took the DNS name as a string and pattern-matched it. Root's
# review was right that this proves nothing: a typed string cannot establish that
# the ARN and the DNS name refer to the SAME load balancer, and a mismatched pair
# would silently build an edge that forwards somewhere nobody reviewed. The DNS
# name is now READ from the load balancer identified by fixture_alb_arn
# (data.aws_lb.fixture.dns_name in main.tf), so the two cannot disagree by
# construction. Scheme, VPC, account, region and run-ownership come from that same
# authoritative read rather than from the operator's typing.

variable "expected_vpc_id" {
  description = <<-EOT
    The VPC the fixture ALB and the reused VPC Link must BOTH be in.

    Required because a VPC Link can only reach load balancers inside its own VPC:
    a fixture ALB in another VPC produces a plan that applies cleanly and then
    times out on every call, which reads as a broken fixture rather than as the
    misconfiguration it is. Verified against both live resources, not assumed.
  EOT
  type        = string

  validation {
    condition     = can(regex("^vpc-[0-9a-f]{8,17}$", var.expected_vpc_id))
    error_message = "expected_vpc_id must be a VPC id such as vpc-0d6115bead9301d25."
  }
}

variable "ordinary_internal_plane_alb_arn" {
  description = <<-EOT
    The ORDINARY internal-plane ALB ARN, supplied for one purpose only: to assert
    the fixture is not pointed at it.

    REQUIRED — it deliberately has no default. In the previous revision this
    defaulted to empty, which made the "not the ordinary ALB" comparison pass
    trivially precisely when the operator had not looked the value up: the moment
    the check mattered most was the moment it was silently skipped. Not having
    discovered the ordinary identity is not evidence of isolation, so the
    component now refuses to plan without it.

    Without this check the component would happily build a "fixture" edge that
    forwards to the ordinary gateway pods. That would look green while actually
    exercising live traffic under an evaluation ticket — the failure mode
    FIXTURE-ROUTING-CONSTRAINT.md refused to ship.

    Discover it read-only:
      aws ssm get-parameter --name /adp/<env>/gateway/internal-plane-alb-arn \
        --query Parameter.Value --output text
  EOT
  type        = string

  validation {
    condition = can(regex(
      "^arn:aws:elasticloadbalancing:[a-z0-9-]+:[0-9]{12}:loadbalancer/(app|net)/[^/]+/[0-9a-f]+$",
      var.ordinary_internal_plane_alb_arn
    ))
    error_message = "ordinary_internal_plane_alb_arn must be a load balancer ARN. Discover it from /adp/<env>/gateway/internal-plane-alb-arn; an empty value is not accepted because a skipped isolation check is not a passed one."
  }
}

# DELIBERATELY ABSENT: vpc_link_egress_target_security_group_ids
# --------------------------------------------------------------
# An earlier revision took the security groups the VPC Link "may egress to" as an
# INPUT, and proved reachability by intersecting it with the ALB's discovered
# groups. Root's review rejected that, correctly: the list recorded what an
# operator typed after running describe-security-groups at some earlier moment,
# so the check passed whenever the operator's belief was stale, examined only the
# egress direction, and constrained neither port nor protocol.
#
# main.tf now reads the rules themselves (data.aws_vpc_security_group_rule) and
# asserts BOTH directions on the fixture port. The input is not merely unused: it
# is removed, because leaving it would offer a second and laxer way to satisfy a
# requirement that must have exactly one.

variable "ordinary_internal_plane_alb_dns" {
  description = <<-EOT
    The ORDINARY internal-plane ALB's DNS name, compared against the fixture ALB's
    DISCOVERED DNS name.

    Checked in addition to the ARN because the two can disagree: a stale ordinary
    ARN would let the ARN comparison pass while the fixture still fronts the live
    gateway's pods. Comparing the resolved DNS name closes that gap.

    Discover it read-only:
      aws ssm get-parameter --name /adp/<env>/gateway/internal-plane-alb-dns \
        --query Parameter.Value --output text
  EOT
  type        = string

  validation {
    condition     = length(var.ordinary_internal_plane_alb_dns) > 0
    error_message = "ordinary_internal_plane_alb_dns is required — a skipped isolation check is not a passed one."
  }
}

variable "fixture_alb_listener_port" {
  description = <<-EOT
    Port the fixture ALB listens on. Defaults to 80 to match the ordinary
    internal-plane ALB.

    This default is load-bearing rather than cosmetic: the reused VPC Link's
    security group permits egress on port 80 only (verified by read-only
    discovery of sg-013f2ce2bcaf1642c in dev). A different port would need that
    shared security group opened, which is an ordinary-infrastructure change this
    issue forbids. See docs/design-notes/4010-internal-plane-alb-separation.md,
    which reached the same conclusion for the ordinary internal plane.
  EOT
  type        = number
  default     = 80

  validation {
    condition     = var.fixture_alb_listener_port > 0 && var.fixture_alb_listener_port <= 65535
    error_message = "fixture_alb_listener_port must be a valid TCP port."
  }
}

# ---------------------------------------------------------------------------
# VPC Link — reuse, do not duplicate
# ---------------------------------------------------------------------------

variable "vpc_link_id" {
  description = <<-EOT
    EXISTING apigatewayv2 VPC Link id to reuse (dev: the bedrockgw VPC link).

    Reused deliberately: #5836 forbids duplicating broad platform infrastructure
    or adding new NAT, and a VPC Link is per-VPC plumbing, not per-run state. The
    link carries no routing authority of its own — the ALB is bound per
    integration via integrationTarget — so reuse does not let fixture traffic
    reach ordinary pods, or the reverse.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]{4,12}$", var.vpc_link_id))
    error_message = "vpc_link_id must be an apigatewayv2 VPC link id."
  }
}

# ---------------------------------------------------------------------------
# Caller authorization — least privilege
# ---------------------------------------------------------------------------

variable "allowed_caller_role_arns" {
  description = <<-EOT
    Exact IAM role ARNs permitted to invoke the fixture edge — normally just the
    protected agent worker role for this run.

    Enforced by the API's own resource policy, so a wrong-role caller is refused
    AT THE EDGE even though it holds a valid signature. This is what makes the
    "wrong-role calls are refused" acceptance provable without weakening any
    ordinary permission: the restriction lives on the fixture API, so nothing
    about the worker's existing identity policy or permissions boundary changes.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.allowed_caller_role_arns) > 0
    error_message = "allowed_caller_role_arns must list at least one role ARN — an unrestricted fixture edge is not acceptable."
  }

  validation {
    condition = alltrue([
      for arn in var.allowed_caller_role_arns :
      can(regex("^arn:aws:iam::[0-9]{12}:role/.+$", arn))
    ])
    error_message = "Every entry in allowed_caller_role_arns must be an IAM role ARN."
  }

  validation {
    # A wildcard principal would defeat the wrong-role refusal the evaluation
    # has to demonstrate.
    condition = alltrue([
      for arn in var.allowed_caller_role_arns : !can(regex("[*?]", arn))
    ])
    error_message = "allowed_caller_role_arns must not contain wildcards — list exact role ARNs."
  }
}

variable "integration_timeout_ms" {
  description = "Integration timeout for the fixture route, matching the ordinary long-poll budget."
  type        = number
  default     = 29000

  validation {
    condition     = var.integration_timeout_ms >= 1000 && var.integration_timeout_ms <= 300000
    error_message = "integration_timeout_ms must be between 1000 and 300000."
  }
}

variable "common_tags" {
  description = "Base tags. Ownership/run tags are merged on top and cannot be overridden by this input."
  type        = map(string)
  default     = {}
}
