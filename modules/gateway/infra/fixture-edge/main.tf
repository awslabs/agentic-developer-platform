# =============================================================================
# Wave 2 fixture trusted edge — Issue #5836
# =============================================================================
# WHY THIS EXISTS
# ---------------
# Epic #3959 Wave 2 acceptance (#3968) needs a PROTECTED agent worker to complete
# a genuine bootstrap against an ISOLATED fixture gateway. That is impossible
# through the ordinary edge, and the reason is structural rather than a matter of
# configuration:
#
#   * Every /internal/v1/agent route is gated by require_agent_transport
#     (modules/gateway/src/agentauth/routes.py:50-57), which 403s without
#     X-Caller-Identity.
#   * That header is believed ONLY when X-Adp-Edge-Provenance constant-time
#     matches the configured secret (src/auth/caller_provenance.py:89-101). A
#     failed provenance check is terminal — never a fallback. So the pair cannot
#     be fabricated, which is also why #5836 forbids trying.
#   * Only API Gateway injects that pair, and every AWS_IAM route forwards to a
#     SINGLE ALB ARN that terminates at Service bedrockgateway -> pods labelled
#     `app: bedrockgateway`. Genuine-provenance traffic therefore cannot reach a
#     fixture.
#
# #3968's FIXTURE-ROUTING-CONSTRAINT.md (at e8ceb349a) traced every candidate
# diversion — host headers, stage variables, canary, a second Ingress on the same
# ALB — found none available on this EKS Auto Mode cluster, and named the owning
# change instead of faking a result. THIS COMPONENT IS THAT NAMED CHANGE.
#
# WHY A SEPARATE COMPONENT AND NOT A ROUTE IN THE ORDINARY MODULE
# ---------------------------------------------------------------
# #5836 requires ordinary flags, routes, selectors and the unknown-owner probe to
# be preserved, and forbids any ordinary platform apply. Adding a fixture route to
# modules/api-gateway would put disposable per-run state inside the production
# REST API: the fixture's lifecycle would be entangled with production's
# sha1(body) redeployment trigger, and every fixture create/destroy would
# redeploy the live stage. A standalone REST API with its OWN isolated state
# backend cannot do that. Ordinary traffic is preserved STRUCTURALLY — this
# directory touches no production resource — rather than by reviewer vigilance.
#
# WHAT IS DELIBERATELY NOT HERE
# -----------------------------
#   * No VPC, subnets, NAT or VPC Link creation. The existing VPC Link is reused
#     (#5836: no duplicated broad platform infrastructure, no new NAT).
#   * No IAM role, policy or permissions-boundary change. None is needed; see
#     the least-privilege note on the resource policy below.
#   * No fixture ALB/Ingress/Deployment. Those are Kubernetes objects owned by
#     the #3968 fixture tooling, which this component must not edit concurrently.
#     This component consumes the fixture ALB as an input and owns only the edge.
# =============================================================================

terraform {
  required_version = ">= 1.14.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# =============================================================================
# Authoritative discovery — facts read from AWS, not strings from a tfvars file
# =============================================================================
# An earlier revision validated the fixture backend by pattern-matching operator-
# supplied strings. Root's review was correct that this establishes nothing: a
# regex cannot tell whether an ARN and a DNS name name the same load balancer,
# whether that load balancer is internal, or whether it is in the VPC the reused
# VPC Link can actually reach. Those are properties of live resources, so they are
# READ here and asserted in the blocking preconditions below.
#
# Reading (rather than trusting) is what turns "the operator typed a plausible
# ARN" into "this specific internal load balancer, in this account, region and
# VPC, tagged as owned by this run".
data "aws_lb" "fixture" {
  count = local.enabled ? 1 : 0
  arn   = var.fixture_alb_arn
}

# The reused VPC Link's own VPC. A VPC Link can only reach load balancers inside
# its VPC, so this is the fact that decides whether the integration can work at
# all — verified rather than assumed to match.
data "aws_apigatewayv2_vpc_link" "reused" {
  count       = local.enabled ? 1 : 0
  vpc_link_id = var.vpc_link_id
}

# The VPC Link resource does NOT expose a vpc_id (verified against the provider
# schema for hashicorp/aws v6.x: it exports only arn, id, name, region,
# security_group_ids, subnet_ids, tags, vpc_link_id). Its VPC is therefore derived
# from one of its subnets, which is authoritative — a subnet belongs to exactly one
# VPC.
data "aws_subnet" "vpc_link" {
  count = local.enabled ? 1 : 0
  id    = tolist(data.aws_apigatewayv2_vpc_link.reused[0].subnet_ids)[0]
}

# ---------------------------------------------------------------------------
# REACHABILITY, READ FROM THE SECURITY GROUP RULES THEMSELVES
# ---------------------------------------------------------------------------
# The previous revision "proved" reachability with
#
#   setintersection(data.aws_lb.fixture[0].security_groups,
#                   var.vpc_link_egress_target_security_group_ids)
#
# Root's review was right that this is not proof, and the reason is worth stating
# precisely, because one half of that expression looked authoritative:
#
#   * the fixture ALB's groups WERE discovered — but the set they were compared
#     against was a LIST THE OPERATOR TYPED. It asserted "the operator believes
#     the link may egress to this group", never "the link may egress to it".
#   * the list was collected by hand from a `describe-security-groups` run at
#     some earlier moment. A rule revoked or re-scoped after that — which needs no
#     change to this repo — leaves the check passing while every request times out.
#   * it only ever examined ONE DIRECTION. An egress permission on the link's
#     group means nothing if the ALB's group does not admit the link, and the ALB
#     side was not read at all. A fixture ALB reusing a permitted group but
#     missing the inbound rule passed every check and was unreachable.
#   * nothing constrained the PORT or the PROTOCOL. Set membership is true for a
#     group reachable only on 443, or only over UDP.
#
# A timeout is the worst possible failure for this component: the plan applies, the
# edge reports healthy, the bootstrap handshake never completes, and the run reads
# as "the protected worker failed" rather than "the fixture was never wired".
#
# So the rules are read from AWS and the two directions are asserted separately
# below. `aws_security_group` cannot be used for this: verified against the
# hashicorp/aws v6.66.0 schema, it exports only arn, description, id, name, region,
# tags and vpc_id — NO rule attributes. `aws_vpc_security_group_rule` is the
# authoritative per-rule read (from_port, to_port, ip_protocol, is_egress,
# referenced_security_group_id, security_group_id, cidr_ipv4).
#
# THIS IS A READ AND ONLY A READ. #5836 forbids ordinary-infrastructure changes,
# and the groups involved are shared: sg-0623ec399f4a20b87 is carried by BOTH
# ordinary gateway ALBs and sg-013f2ce2bcaf1642c is the platform VPC Link's. This
# component declares no aws_security_group and no aws_vpc_security_group_*_rule
# resource, so it cannot widen, narrow or delete any of them — the fixture ALB must
# reuse a group that already works, and if none does, the answer is a refusal here
# rather than a rule change.
#
# `group-id` covers EVERY group attached to either side, in one read, so a group
# added to the ALB later cannot escape examination the way an enumerated list
# would.
data "aws_vpc_security_group_rules" "reachability" {
  count = local.enabled ? 1 : 0

  filter {
    name = "group-id"
    values = concat(
      tolist(data.aws_apigatewayv2_vpc_link.reused[0].security_group_ids),
      tolist(data.aws_lb.fixture[0].security_groups),
    )
  }
}

data "aws_vpc_security_group_rule" "reachability" {
  for_each = local.enabled ? toset(data.aws_vpc_security_group_rules.reachability[0].ids) : toset([])

  security_group_rule_id = each.value
}

# ---------------------------------------------------------------------------
# WHAT THE FIXTURE POD SEES AS THE SOURCE OF ALB TRAFFIC
# ---------------------------------------------------------------------------
# This exists for #3968's fixture NetworkPolicy, and it is an OBSERVATION, not a
# policy change. Nothing here creates, edits or reads a NetworkPolicy.
#
# THE COMPOSITION PROBLEM
# -----------------------
# #3968's render_fixture.render_policies builds a fixture gateway policy whose only
# ingress rule is:
#
#   from: [ {namespaceSelector: adp-gateway}, {namespaceSelector: adp-agents} ]
#   ports: [ {TCP, 8080} ]
#
# A namespaceSelector matches POD sources by their namespace. Traffic arriving from
# an Application Load Balancer is not a namespace-selected pod source: with
# target-type `ip` the ALB connects from its OWN elastic network interfaces, which
# belong to the load balancer and to no pod. So the policy admits exactly the
# sources a pure in-cluster harness uses and denies the edge this component builds.
#
# The Terraform plan can apply while the policy denies both health checks and
# requests: with IP targets both reach the same pod port from the ALB interfaces.
# The target becomes unhealthy and the worker cannot bootstrap. Establish this
# network path before attributing a failed handshake to worker behavior.
#
# WHY THE ANSWER IS AN OBSERVATION AND NOT A SELECTOR
# ---------------------------------------------------
# The tempting fixes are all wrong for this issue:
#
#   * Relaxing the ordinary gateway's policy is forbidden (#5836 preserves ordinary
#     flags, routes and selectors) and would widen production's blast radius for a
#     fixture.
#   * `ipBlock: 0.0.0.0/0` on the fixture policy would admit the whole VPC and
#     every other ALB in it, which is a wider allowance than the ordinary plane has.
#   * Adding a namespaceSelector cannot work at all: the source is not a pod.
#
# What the fixture policy needs is the NARROWEST TRUE STATEMENT of the source: the
# /32 addresses of THIS fixture ALB's network interfaces. That is a fact about a
# live AWS resource, which is why it is read here and published as an output rather
# than guessed in #3968's renderer, and why this component owns it: this component
# is what creates the path.
#
# HOW THE ADDRESSES ARE DERIVED — AND WHY THIS IS AUTHORITATIVE
# -------------------------------------------------------------
# `aws_lb` publishes no interface list, and the ALB's subnets are not the answer
# (a subnet CIDR would admit every address in it, including the ordinary gateway's
# ALB — verified: both ordinary gateway ALBs and this fixture's would sit in
# subnet-03ae2ea2ebdf611bb). The interfaces are found by their description, which
# the ELB service sets to the literal string "ELB <arn_suffix>". Verified read-only
# on 2026-09-24 against the live ordinary ALB: filtering on
# `ELB app/k8s-adpgatew-bedrockg-d2e32d8c72/30c651bf3c5e4135` returned exactly its
# two interfaces (10.0.11.37, 10.0.12.11) and nothing else. `arn_suffix` is an
# attribute of the discovered load balancer, so the filter cannot be pointed at
# another ALB by a typo in a variable.
#
# These addresses describe the ALB's current interfaces. A missing new address
# denies traffic; a retired address can be reassigned and leave an unintended
# allowance. Refresh the live observation and matching owned policy before each
# execution, and remove the owned allowance during cleanup. A saved Terraform
# output or matching run nonce alone does not prove address freshness.
data "aws_network_interfaces" "fixture_alb" {
  count = local.enabled ? 1 : 0

  filter {
    name   = "description"
    values = ["ELB ${data.aws_lb.fixture[0].arn_suffix}"]
  }
}

data "aws_network_interface" "fixture_alb" {
  for_each = local.enabled ? toset(data.aws_network_interfaces.fixture_alb[0].ids) : toset([])

  id = each.value
}

locals {
  enabled = var.fixture_edge_enabled

  # Every name carries the run nonce, so a second run cannot collide with this
  # one and teardown can tell them apart by name as well as by tag.
  name_prefix = "bedrockgw-${var.environment}-w2fx-${var.run_nonce}"

  # Ownership tags are merged LAST so var.common_tags cannot override the facts
  # teardown relies on. #3968's lib/ownership.py verifies a server-returned
  # identity at teardown and deletes only on a match; these tags are the
  # API-Gateway-side equivalent of its SQS run-nonce tag.
  # The tag that carries run ownership. Named once here because it is asserted on
  # the discovered fixture ALB (run_binding_gate), applied to everything this
  # component creates, and re-read at teardown to prove ownership before deleting.
  ownership_tag_key = "AdpFixtureRun"

  # --- observed reachability, derived from the rules read above -------------
  # The two sides of the path, as the live resources report them. Both are
  # DISCOVERED: the link's groups come from the link, the ALB's from the ALB.
  vpc_link_security_group_ids    = local.enabled ? toset(data.aws_apigatewayv2_vpc_link.reused[0].security_group_ids) : toset([])
  fixture_alb_security_group_ids = local.enabled ? toset(data.aws_lb.fixture[0].security_groups) : toset([])

  # Does a rule admit the fixture listener port over TCP?
  #
  # ip_protocol "-1" is AWS's all-traffic rule; the API reports its port fields as
  # -1, so a from_port/to_port range test alone would REJECT the broadest rule
  # there is. Both forms are therefore handled explicitly. Anything else (udp,
  # icmp, or a tcp range that excludes the port) does not count, because set
  # membership in the old check was true for a group reachable only on 443.
  rule_admits_fixture_port = {
    for id, r in data.aws_vpc_security_group_rule.reachability : id => (
      r.ip_protocol == "-1" || (
        r.ip_protocol == "tcp" &&
        r.from_port <= var.fixture_alb_listener_port &&
        r.to_port >= var.fixture_alb_listener_port
      )
    )
  }

  # DIRECTION 1 — the link's group permits egress TO a group the ALB carries.
  #
  # Matched on referenced_security_group_id rather than on a CIDR. A CIDR rule that
  # happens to span the ALB's subnets is deliberately NOT accepted as proof: a CIDR
  # is a location, not an identity, so it establishes nothing about this particular
  # load balancer and would keep passing after the ALB moved or was replaced.
  vpc_link_egress_rules_to_fixture_alb = [
    for id, r in data.aws_vpc_security_group_rule.reachability : id
    if r.is_egress &&
    r.referenced_security_group_id != null &&
    contains(local.vpc_link_security_group_ids, r.security_group_id) &&
    contains(local.fixture_alb_security_group_ids, r.referenced_security_group_id) &&
    local.rule_admits_fixture_port[id]
  ]

  # DIRECTION 2 — a group the ALB carries admits ingress FROM the link's group.
  #
  # This half was never checked before, and it is the half that a correctly-built
  # fixture ALB can still fail: reusing a permitted group gives the LINK its egress
  # rule, while the inbound rule lives on the ALB side and can be absent or scoped
  # to a different port. Egress without ingress is a timeout, not a refusal.
  fixture_alb_ingress_rules_from_vpc_link = [
    for id, r in data.aws_vpc_security_group_rule.reachability : id
    if !r.is_egress &&
    r.referenced_security_group_id != null &&
    contains(local.fixture_alb_security_group_ids, r.security_group_id) &&
    contains(local.vpc_link_security_group_ids, r.referenced_security_group_id) &&
    local.rule_admits_fixture_port[id]
  ]

  # Security groups combine permissions across every attached group. A valid
  # link/ALB pair does not cancel a second rule exposing the listener to others.
  # Backend egress and ingress on other ports are outside this listener check.
  fixture_alb_untrusted_listener_rules = [
    for id, r in data.aws_vpc_security_group_rule.reachability : id
    if !r.is_egress &&
    contains(local.fixture_alb_security_group_ids, r.security_group_id) &&
    local.rule_admits_fixture_port[id] &&
    !try(contains(local.vpc_link_security_group_ids, r.referenced_security_group_id), false)
  ]

  # Reachability is the CONJUNCTION. Either half alone is a silent timeout.
  vpc_link_can_reach_fixture_alb = (
    length(local.vpc_link_egress_rules_to_fixture_alb) > 0 &&
    length(local.fixture_alb_ingress_rules_from_vpc_link) > 0
  )

  # --- the ALB traffic source, for #3968's fixture NetworkPolicy -------------
  # The narrowest true statement of where the fixture pod sees ALB traffic come
  # from: the /32 addresses of this ALB's own interfaces. See the data sources above
  # for why a subnet CIDR or a namespaceSelector cannot serve.
  #
  # Sorted so the output is stable across plans — an unstable list would produce a
  # spurious diff in #3968's rendered policy and invite someone to stop regenerating
  # it.
  fixture_alb_source_ips = sort([
    for eni in data.aws_network_interface.fixture_alb : eni.private_ip
  ])
  fixture_alb_source_cidrs = [for ip in local.fixture_alb_source_ips : "${ip}/32"]

  ownership_tags = {
    AdpFixtureRun     = var.run_nonce
    AdpFixtureIssue   = "5836"
    AdpFixtureEpic    = "3959"
    AdpFixtureAccount = var.expected_account_id
    AdpFixtureRegion  = var.aws_region
    Environment       = var.environment
    ManagedBy         = "terraform"
    Purpose           = "wave2-fixture-trusted-edge"
    Disposable        = "true"
  }
  tags = merge(var.common_tags, local.ownership_tags)
}

# =============================================================================
# Run binding — a BLOCKING gate, not a warning
# =============================================================================
# WHY THIS IS A RESOURCE PRECONDITION AND NOT A `check` BLOCK
# -----------------------------------------------------------
# The previous revision expressed these requirements in a `check` block whose
# comments promised refusal. They did not refuse. A failing `check` assertion
# emits a WARNING and `terraform plan` still exits 0 — reproduced on Terraform
# 1.15.3 with a minimal case, matching root's reproduction on 1.14.9:
#
#   condition = false  ->  "Check block assertion failed" + PLAN EXIT CODE 0
#
# So any wrapper that gates on the exit code (the runbook does, and so would CI)
# would have treated a fixture misdirected at the WRONG ACCOUNT as approved. The
# guard read as the strongest part of the component while being the weakest.
#
# Resource preconditions DO block: the same condition attached here fails the plan
# with EXIT CODE 1 (verified both ways — exit 1 when violated, exit 0 when
# satisfied). Everything else in this component depends on this resource, so
# nothing can be created while any binding requirement is unmet.
#
# terraform_data is used because the gate must be evaluated at PLAN time and must
# create no cloud resource. It is intentionally the only place these requirements
# live, so they cannot be satisfied by a second, laxer path.
resource "terraform_data" "run_binding_gate" {
  count = local.enabled ? 1 : 0

  # Recorded in state so a reviewer of an existing fixture can see what the gate
  # was satisfied against. No secret is included.
  input = {
    run_nonce  = var.run_nonce
    account_id = var.expected_account_id
    region     = var.aws_region
    vpc_id     = var.expected_vpc_id
    fixture_alb = {
      arn      = data.aws_lb.fixture[0].arn
      dns_name = data.aws_lb.fixture[0].dns_name
      internal = data.aws_lb.fixture[0].internal
      vpc_id   = data.aws_lb.fixture[0].vpc_id
    }

    # The rule ids that actually carried the two directions, recorded so a reviewer
    # of an existing fixture can re-read the SAME rules the gate passed on rather
    # than re-deriving what "should" have been true. A tfvars list could not serve
    # this purpose: it records what was typed, not what was observed.
    reachability = {
      port                        = var.fixture_alb_listener_port
      vpc_link_security_groups    = sort(tolist(local.vpc_link_security_group_ids))
      fixture_alb_security_groups = sort(tolist(local.fixture_alb_security_group_ids))
      egress_rule_ids             = sort(local.vpc_link_egress_rules_to_fixture_alb)
      ingress_rule_ids            = sort(local.fixture_alb_ingress_rules_from_vpc_link)
    }
  }

  lifecycle {
    # --- identity of the account/region actually being deployed into ---------
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.expected_account_id
      error_message = "Refusing: the caller's real account does not match expected_account_id (#5836 authorizes 879318057152 only)."
    }

    precondition {
      condition     = data.aws_region.current.region == var.aws_region
      error_message = "Refusing: the provider's resolved region does not match aws_region."
    }

    # --- the fixture ALB is a real, internal, correctly-placed load balancer --
    # Read from the load balancer itself. `internal` is the authoritative scheme
    # flag: it CANNOT be inferred from the DNS name, because internal ALBs do not
    # always carry an `internal-` prefix (verified in dev: the agent-context
    # LiteLLM ALB is internal and has no such prefix). A public ALB here would
    # expose a trusted-header-injecting edge's backend to the internet.
    precondition {
      condition     = data.aws_lb.fixture[0].internal
      error_message = <<-EOT
        Refusing: the ALB identified by fixture_alb_arn is INTERNET-FACING (internal = false).

        This edge injects genuine trusted identity headers. Its backend must not be
        reachable from outside the VPC, or the fixture gateway could be addressed
        directly, bypassing the edge that is supposed to be the only way in.
      EOT
    }

    precondition {
      condition     = data.aws_lb.fixture[0].vpc_id == var.expected_vpc_id
      error_message = "Refusing: the fixture ALB is in a different VPC than expected_vpc_id. Traffic from the reused VPC Link could never reach it."
    }

    # ARN embeds account and region; compared against the live resource's own ARN
    # so a copied-from-another-account value cannot pass.
    precondition {
      condition     = can(regex(":${var.expected_account_id}:", data.aws_lb.fixture[0].arn))
      error_message = "Refusing: the fixture ALB belongs to a different account than expected_account_id."
    }

    precondition {
      condition     = can(regex(":elasticloadbalancing:${var.aws_region}:", data.aws_lb.fixture[0].arn))
      error_message = "Refusing: the fixture ALB is in a different region than aws_region."
    }

    # --- isolation from the ordinary internal plane --------------------------
    # ordinary_internal_plane_alb_arn is now REQUIRED (no empty default), so this
    # can no longer pass by being skipped.
    precondition {
      condition     = var.fixture_alb_arn != var.ordinary_internal_plane_alb_arn
      error_message = <<-EOT
        Refusing: fixture_alb_arn equals the ORDINARY internal-plane ALB.

        This edge would inject genuine trusted headers and forward them to the
        ordinary gateway pods, so the "fixture" evaluation would actually be
        exercising live traffic while reporting isolation. That is precisely the
        dishonest outcome #3968's FIXTURE-ROUTING-CONSTRAINT.md refused to ship.
      EOT
    }

    # The ordinary ALB's DNS is also compared, because two different ARNs could
    # still front the same pods if the operator supplied a stale ordinary ARN.
    precondition {
      condition     = data.aws_lb.fixture[0].dns_name != var.ordinary_internal_plane_alb_dns
      error_message = "Refusing: the fixture ALB's DNS name is the ORDINARY internal-plane ALB's. The 'fixture' would be the live gateway."
    }

    # --- the reused VPC Link can actually reach this ALB --------------------
    # Reuse is required (#5836 forbids duplicating platform plumbing), but reuse
    # is only sound if the link is in the same VPC as the target.
    precondition {
      condition     = data.aws_subnet.vpc_link[0].vpc_id == var.expected_vpc_id
      error_message = <<-EOT
        Refusing: the reused VPC Link is not in expected_vpc_id.

        A VPC Link only reaches load balancers inside its own VPC. Applying this
        would produce an edge that times out on every call — which reads as a
        broken fixture rather than as the misconfiguration it is.
      EOT
    }

    # --- the VPC Link can actually reach the fixture ALB, BOTH WAYS ----------
    # Asserted from the security group RULES (see the data sources above), not from
    # a caller-supplied list and not from set membership. Both directions are
    # required and are stated as two separate preconditions, because the fix for
    # each is different and a combined message could not say which half is missing.
    #
    # Direction 1: the VPC Link's group egresses to a group the ALB carries.
    precondition {
      condition     = length(local.vpc_link_egress_rules_to_fixture_alb) > 0
      error_message = <<-EOT
        Refusing: no live security group rule lets the VPC Link egress to the fixture ALB.

        Read from AWS, not from a variable: none of the rules on the VPC Link's own
        security groups (${join(", ", sort(tolist(local.vpc_link_security_group_ids)))})
        permit egress on tcp/${var.fixture_alb_listener_port} to any security group the fixture ALB
        actually carries (${join(", ", sort(tolist(local.fixture_alb_security_group_ids)))}).

        Every other check would pass and every request would then TIME OUT, which
        reads as "the protected worker failed" rather than as this misconfiguration.

        Fix by giving the fixture ALB a group the link already egresses to —
        fixture-alb.yaml's alb.ingress.kubernetes.io/security-groups annotation
        exists for exactly this. Do NOT widen the shared VPC Link security group:
        it is platform infrastructure, and #5836 forbids ordinary-infrastructure
        changes. This component declares no security group rule resource, so it
        cannot make that change even by accident.

        Inspect the rules this gate read, read-only:
          aws ec2 describe-security-group-rules \
            --filters Name=group-id,Values=<link-sg-id> \
            --query 'SecurityGroupRules[?IsEgress==`true`]'
      EOT
    }

    # Direction 2: a group the ALB carries admits ingress from the link's group.
    # This is the half the previous check never looked at, and the half a fixture
    # ALB built exactly as documented can still fail — reusing a permitted group
    # supplies the LINK's egress rule, while the inbound rule lives on the ALB side.
    precondition {
      condition     = length(local.fixture_alb_ingress_rules_from_vpc_link) > 0
      error_message = <<-EOT
        Refusing: the fixture ALB's security groups do not admit the VPC Link.

        Egress from the link was found, but no rule on the fixture ALB's groups
        (${join(", ", sort(tolist(local.fixture_alb_security_group_ids)))}) permits
        INGRESS on tcp/${var.fixture_alb_listener_port} from a group the link uses
        (${join(", ", sort(tolist(local.vpc_link_security_group_ids)))}).

        One-way permission is indistinguishable from reachability until traffic
        flows, and then it is a timeout. The previous revision checked only the
        egress side, so this exact state passed the gate.

        Select a group composition that admits the link and also passes the
        listener isolation check across every attached group. Do NOT add a rule
        to a shared group. If no reusable composition qualifies, use a separately
        reviewed disposable link/group composition.

        Inspect the rules this gate read, read-only:
          aws ec2 describe-security-group-rules \
            --filters Name=group-id,Values=<alb-sg-id> \
            --query 'SecurityGroupRules[?IsEgress==`false`]'
      EOT
    }

    precondition {
      condition     = length(local.fixture_alb_untrusted_listener_rules) == 0
      error_message = "Refusing: attached ALB security groups expose the fixture listener outside the VPC Link security groups. Disallowed rule IDs: ${join(", ", local.fixture_alb_untrusted_listener_rules)}. Use an isolated suitable group composition; do not modify shared security groups."
    }

    # --- the ALB traffic source must be OBSERVED, not absent -----------------
    # #3968's fixture NetworkPolicy needs this to admit the edge (see the
    # aws_network_interfaces read above). The reason it is a REFUSAL and not a
    # best-effort output is the asymmetry of the failure: an EMPTY list renders a
    # policy with an ipBlock rule that matches nothing, which denies the edge just
    # as thoroughly as having no rule — and does it while looking configured. A
    # reviewer comparing the rendered policy against this output would see two
    # empty things agreeing.
    precondition {
      condition     = length(local.fixture_alb_source_ips) > 0
      error_message = <<-EOT
        Refusing: no network interface was found for the fixture ALB, so the traffic
        source #3968's fixture NetworkPolicy must admit cannot be established.

        The interfaces are found by the description the ELB service assigns them,
        "ELB ${data.aws_lb.fixture[0].arn_suffix}". An empty result usually means the
        ALB is still provisioning — create-fixture-alb.sh waits for it, so run that
        to completion first.

        This is refused rather than published empty because an empty source list
        renders a NetworkPolicy ipBlock that matches nothing. The fixture pod would
        deny the edge exactly as if the rule were missing, while the policy read as
        configured, and the run would report the protected worker's bootstrap as
        failed.

        Inspect read-only:
          aws ec2 describe-network-interfaces \
            --filters Name=description,Values='ELB ${data.aws_lb.fixture[0].arn_suffix}' \
            --query 'NetworkInterfaces[].PrivateIpAddress'
      EOT
    }

    # Every observed interface must belong to the fixture ALB's own VPC. This is a
    # cheap guard against the description filter matching something unexpected:
    # descriptions are service-assigned but not a documented identity contract, and
    # publishing a foreign address as "the source to admit" would have the fixture
    # pod admit traffic from an address this edge does not own.
    precondition {
      condition = alltrue([
        for eni in data.aws_network_interface.fixture_alb :
        eni.vpc_id == var.expected_vpc_id
      ])
      error_message = "Refusing: an interface matched for the fixture ALB is not in expected_vpc_id. Publishing it as the NetworkPolicy source would admit traffic from an address this edge does not own."
    }

    # --- run ownership -----------------------------------------------------
    # The fixture ALB must be tagged as belonging to THIS run. Without this, the
    # component would attach a trusted edge to any internal ALB in the VPC —
    # including one created by another run or by ordinary infrastructure — and
    # teardown could not prove which resources this run was responsible for.
    precondition {
      condition     = try(data.aws_lb.fixture[0].tags[local.ownership_tag_key], "") == var.run_nonce
      error_message = <<-EOT
        Refusing: the fixture ALB does not carry this run's ownership tag.

        Expected tag ${local.ownership_tag_key} = <run_nonce>. A load balancer that
        is not tagged for this run may belong to ordinary infrastructure or to a
        different fixture run, and a name or ARN alone is not ownership. Create the
        fixture ALB with fixture-alb.yaml (this directory), which applies the tag.
      EOT
    }
  }
}

# =============================================================================
# Fixture provenance secret — FRESH, and never the ordinary one
# =============================================================================
# The pod believes X-Caller-Identity only when X-Adp-Edge-Provenance matches the
# secret THAT POD is configured with. The fixture gateway is configured with this
# value, so:
#
#   * a request that did not pass through this fixture edge cannot produce it, and
#   * this edge's header is worthless against the ORDINARY gateway, whose secret
#     is a different random_password in the production module.
#
# The isolation therefore runs in both directions, which is what makes the
# fixture safe to point a real protected worker at. Generated per run (it is
# bound to the nonce via keepers), never read from the ordinary SSM path.
resource "random_password" "fixture_edge_provenance" {
  count = local.enabled ? 1 : 0

  length  = 64
  special = false

  # Rotating with the nonce guarantees a new run never inherits a previous run's
  # proof — a leaked fixture secret cannot outlive its run.
  keepers = {
    run_nonce = var.run_nonce
  }
}

locals {
  # Mapping-template syntax: 'single quotes' denote a static string literal, and
  # context.identity.userArn is the principal API GATEWAY resolved from a
  # signature it verified itself. The worker never supplies either value.
  fixture_verified_caller_identity = local.enabled ? {
    "integration.request.header.X-Caller-Identity"     = "context.identity.userArn"
    "integration.request.header.X-Adp-Edge-Provenance" = "'${random_password.fixture_edge_provenance[0].result}'"
  } : {}

  # Any non-AWS_IAM route must BLANK both headers rather than leave them
  # unmapped. Unmapped means "forward whatever the client sent", and the pod
  # treats X-Caller-Identity as proof of identity — so an unmapped header on an
  # auth-NONE path hands an unauthenticated caller a privileged TokenContext.
  # Blank is what API Gateway can actually guarantee, and the pod's
  # `headers.get(...).strip()` makes blank equivalent to absent.
  fixture_blank_caller_identity = {
    "integration.request.header.X-Caller-Identity"     = "''"
    "integration.request.header.X-Adp-Edge-Provenance" = "''"
  }

  # -------------------------------------------------------------------------
  # Forwarded path — traced from the worker, not assumed
  # -------------------------------------------------------------------------
  # lib/run_identity.py builds `base + "/bootstrap"` from
  # ADP_AGENT_CONTROL_ENDPOINT and REJECTS any endpoint that is not https with a
  # bare host (no userinfo/query/fragment) — so an in-cluster http Service URL
  # cannot be substituted, and the fixture MUST be fronted by a real HTTPS edge.
  # Routes are registered on the pod WITH the /internal prefix
  # (APIRouter(prefix="/internal/v1/agent")), so the prefix must be preserved on
  # forward or every call 404s. The ordinary module makes the same choice for the
  # same reason.
  # The host comes from the DISCOVERED load balancer, never from an input string,
  # so the forwarded destination cannot disagree with the ALB whose scheme, VPC,
  # account, region and run-ownership the gate verified.
  fixture_alb_dns_discovered = local.enabled ? data.aws_lb.fixture[0].dns_name : ""

  fixture_internal_forward_uri = "http://${local.fixture_alb_dns_discovered}/internal/{proxy}"

  fixture_api_body = jsonencode({
    openapi = "3.0.1"
    info = {
      title   = "${local.name_prefix}-fixture-edge"
      version = "1.0"
    }
    paths = {
      # -------------------------------------------------------------------
      # The trusted internal plane. AWS_IAM is REQUIRED here: API Gateway
      # verifies the SigV4 signature and only then writes the identity it
      # derived. An unsigned call never reaches the integration, so it can
      # never acquire the provenance header — this is why "unsigned calls are
      # refused" holds by construction rather than by application logic.
      # -------------------------------------------------------------------
      "/internal/{proxy+}" = {
        x-amazon-apigateway-any-method = {
          security                   = [{ sigv4 = [] }]
          "x-amazon-apigateway-auth" = { type = "AWS_IAM" }
          parameters = [
            {
              name     = "proxy"
              in       = "path"
              required = true
              type     = "string"
            }
          ]
          x-amazon-apigateway-integration = {
            type                 = "http_proxy"
            httpMethod           = "ANY"
            uri                  = local.fixture_internal_forward_uri
            timeoutInMillis      = var.integration_timeout_ms
            responseTransferMode = "STREAM"
            passthroughBehavior  = "when_no_match"
            connectionType       = "VPC_LINK"
            connectionId         = var.vpc_link_id
            integrationTarget    = var.fixture_alb_arn
            requestParameters = merge(
              {
                "integration.request.path.proxy" = "method.request.path.proxy"
              },
              local.fixture_verified_caller_identity,
            )
          }
        }
      }
      # -------------------------------------------------------------------
      # The human-session plane, auth NONE: the fixture gateway authenticates
      # these on their JWT. #5836 requires these routes to BLANK caller and
      # provenance, and that is a real requirement rather than defence in
      # depth: src/auth/dependencies.py skips the provenance branch only when
      # NO X-Caller-Identity is asserted, so a forwarded client header would
      # change which branch runs.
      # -------------------------------------------------------------------
      "/{proxy+}" = {
        x-amazon-apigateway-any-method = {
          "x-amazon-apigateway-auth" = { type = "NONE" }
          parameters = [
            {
              name     = "proxy"
              in       = "path"
              required = true
              type     = "string"
            }
          ]
          x-amazon-apigateway-integration = {
            type                 = "http_proxy"
            httpMethod           = "ANY"
            uri                  = "http://${local.fixture_alb_dns_discovered}/{proxy}"
            timeoutInMillis      = var.integration_timeout_ms
            responseTransferMode = "STREAM"
            passthroughBehavior  = "when_no_match"
            connectionType       = "VPC_LINK"
            connectionId         = var.vpc_link_id
            integrationTarget    = var.fixture_alb_arn
            requestParameters = merge(
              {
                "integration.request.path.proxy" = "method.request.path.proxy"
              },
              local.fixture_blank_caller_identity,
            )
          }
        }
      }
    }
    components = {
      securitySchemes = {
        sigv4 = {
          type                           = "apiKey"
          name                           = "Authorization"
          in                             = "header"
          "x-amazon-apigateway-authtype" = "awsSigv4"
        }
      }
    }
  })
}

resource "aws_api_gateway_rest_api" "fixture" {
  count = local.enabled ? 1 : 0

  name        = "${local.name_prefix}-fixture-edge"
  description = "DISPOSABLE Wave 2 fixture trusted edge (issue #5836, run ${var.run_nonce}). Safe to delete with the run."
  body        = local.fixture_api_body

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  tags = local.tags

  # Nothing may be created until every run-binding requirement has passed. The
  # gate is a plan-time blocking precondition (exit 1 on violation), and this
  # dependency is what puts it ahead of the first cloud resource.
  depends_on = [terraform_data.run_binding_gate]

  # The same invariant the ordinary module enforces (#5653), carried here rather
  # than inherited: a route added to THIS body later must also map both headers.
  # Asserted at plan time because the failure mode is a future edit, and it fails
  # silently — the apply succeeds and the header becomes forgeable on that path.
  # Reading self.body checks the ACTUALLY RENDERED document, so it cannot drift
  # from what is deployed.
  lifecycle {
    postcondition {
      condition = alltrue([
        for path_key, path_item in try(jsondecode(self.body).paths, {}) :
        alltrue([
          for required_header in [
            "integration.request.header.X-Caller-Identity",
            "integration.request.header.X-Adp-Edge-Provenance",
            ] : contains(
            keys(try(path_item["x-amazon-apigateway-any-method"]["x-amazon-apigateway-integration"].requestParameters, {})),
            required_header
          )
        ])
        if can(path_item["x-amazon-apigateway-any-method"]["x-amazon-apigateway-integration"])
      ])
      error_message = <<-EOT
        Issue #5836: every fixture-edge route must map BOTH caller identity and edge provenance.

        An unmapped header is forwarded from the client, and the gateway pod treats
        X-Caller-Identity as proof of identity — granting an unauthenticated caller
        a privileged internal TokenContext against the fixture.

        Add ONE of these to the new route's x-amazon-apigateway-integration:

          AWS_IAM route:  local.fixture_verified_caller_identity
          any other route: local.fixture_blank_caller_identity

        Do not remove this check to make a deploy pass.
      EOT
    }
  }
}

# =============================================================================
# Resource policy — the wrong-role refusal, without touching ordinary IAM
# =============================================================================
# LEAST PRIVILEGE, AND WHY NO IAM CHANGE IS NEEDED
# ------------------------------------------------
# The protected worker role's existing grants are wildcard-on-API-ID:
#   scaledjob-iam.tf:198-201        arn:aws:execute-api:us-east-1:*:*/*/*/internal/*
#   agent-authority-boundary.tf:11  arn:aws:execute-api:...:*/*/*/internal/v1/agent/*
# Because this fixture API serves that same /internal/v1/agent path, the worker
# can already invoke it. So supported access is achieved with ZERO widening of
# any ordinary permission — nothing is added to the identity policy or the
# permissions boundary, which #5836 requires.
#
# The consequence of that wildcard is that restriction must come from the API
# side, which is what this policy does: an explicit Deny for any principal other
# than the listed role ARNs. A valid signature from an unlisted role is refused
# at the edge, so "wrong-role calls are refused" is provable without weakening
# production.
locals {
  # execute-api resource ARNs for the TRUSTED INTERNAL PLANE ONLY.
  #
  # Shape is <execution_arn>/<stage>/<METHOD>/<path>. Wildcards cover stage and
  # method; the path segment is pinned to /internal so the role restriction cannot
  # leak onto the human plane. This mirrors how the ordinary API scopes its own
  # path-specific denies (modules/api-gateway/main.tf
  # "DenyInternalRoutesOutsideAllowedSources" uses .../*/*/internal/*).
  #
  # Both forms are listed deliberately: `/internal/*` does not match the bare
  # `/internal` resource itself, and omitting the bare form would leave one
  # unrestricted internal resource behind.
  internal_plane_policy_resources = local.enabled ? [
    "${aws_api_gateway_rest_api.fixture[0].execution_arn}/*/*/internal",
    "${aws_api_gateway_rest_api.fixture[0].execution_arn}/*/*/internal/*",
  ] : []
}

resource "aws_api_gateway_rest_api_policy" "fixture" {
  count = local.enabled ? 1 : 0

  rest_api_id = aws_api_gateway_rest_api.fixture[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # -----------------------------------------------------------------------
      # 1. The human-session transport plane.
      # -----------------------------------------------------------------------
      # Allowed WITHOUT a signature, and deliberately so: the fixture gateway
      # authenticates these requests from their own session JWT, exactly as the
      # ordinary gateway does. Authentication happens in the POD, not at the edge.
      #
      # This statement exists because the previous revision did not have it, and
      # the omission broke the path it was supposed to serve. A single
      # "deny everyone but the worker role" over the WHOLE API also covered these
      # auth-NONE routes; an unsigned browser request carries no aws:PrincipalArn,
      # so it matched "everyone else" and was refused AT THE EDGE before the pod
      # could ever see the JWT. The human plane was unreachable while the policy
      # looked like a tightening.
      #
      # Allowing unsigned transport here is NOT a trust grant: the /{proxy+} route
      # blanks BOTH X-Caller-Identity and X-Adp-Edge-Provenance (see
      # local.fixture_blank_caller_identity), so a caller on this plane cannot
      # assert an internal identity. Transport is open; identity is not.
      {
        Sid       = "AllowHumanSessionTransport"
        Effect    = "Allow"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource  = "${aws_api_gateway_rest_api.fixture[0].execution_arn}/*/*/*"
      },
      # -----------------------------------------------------------------------
      # 2. The trusted internal plane — restricted to the listed roles.
      # -----------------------------------------------------------------------
      {
        Sid       = "AllowListedFixtureCallersOnInternal"
        Effect    = "Allow"
        Principal = { AWS = var.allowed_caller_role_arns }
        Action    = "execute-api:Invoke"
        Resource  = local.internal_plane_policy_resources
      },
      {
        # Deny scoped to the INTERNAL PATHS ONLY. An explicit Deny beats any
        # Allow, so this is what makes the wrong-role refusal provable — while
        # leaving the human plane above reachable.
        #
        # aws:PrincipalArn for a request signed by an assumed role is the
        # underlying IAM ROLE ARN, not a session-specific ARN. The previous
        # revision additionally listed invented
        # `arn:aws:sts::...:assumed-role/NAME/*` variants and asserted in a
        # comment that a session presents that form; that claim was wrong, and the
        # extra entries widened the match for no benefit. Listing the role ARN is
        # both correct and sufficient.
        #
        # WHICH LAYER REFUSES WHAT (do not conflate these):
        #   * unsigned call to /internal/*    -> refused by API Gateway AWS_IAM
        #     authorization on the route (no valid SigV4 -> 403, never integrated).
        #   * signed call from an unlisted role -> refused by THIS statement
        #     (resource policy), even though the signature is valid.
        #   * forged/replayed provenance header -> refused by the GATEWAY POD
        #     (src/auth/caller_provenance.py constant-time compare). The edge
        #     overwrites the header on the internal route, so a client-supplied
        #     value never survives; the pod check is the backstop.
        Sid       = "DenyNonListedPrincipalsOnInternal"
        Effect    = "Deny"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource  = local.internal_plane_policy_resources
        Condition = {
          StringNotLike = {
            "aws:PrincipalArn" = var.allowed_caller_role_arns
          }
        }
      },
    ]
  })
}

resource "aws_api_gateway_deployment" "fixture" {
  count = local.enabled ? 1 : 0

  rest_api_id = aws_api_gateway_rest_api.fixture[0].id

  # The resource policy is part of the trigger deliberately: a policy change does
  # not take effect until the stage is redeployed. Without it here the apply
  # succeeds, the plan looks correct, and the wrong-role Deny is not actually in
  # force — an unrestricted trusted edge that reports as restricted.
  triggers = {
    redeployment = sha1(jsonencode([
      local.fixture_api_body,
      try(aws_api_gateway_rest_api_policy.fixture[0].policy, ""),
    ]))
  }

  depends_on = [aws_api_gateway_rest_api_policy.fixture]

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_cloudwatch_log_group" "fixture" {
  count = local.enabled ? 1 : 0

  name = "/aws/api-gateway/${local.name_prefix}-fixture-edge"

  # Short retention: this is disposable per-run evidence, and a log group that
  # outlives its run is an untracked leftover the cleanup ledger cannot reach.
  retention_in_days = 7
  tags              = local.tags
}

resource "aws_api_gateway_stage" "fixture" {
  count = local.enabled ? 1 : 0

  deployment_id = aws_api_gateway_deployment.fixture[0].id
  rest_api_id   = aws_api_gateway_rest_api.fixture[0].id
  stage_name    = var.environment

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.fixture[0].arn
    # Deliberately NO request/response headers in this format. The provenance
    # header would otherwise be written to CloudWatch, and #5836 requires the
    # secret to stay out of logs. caller identifies the verified principal,
    # which is the fact the evaluation needs, and is not a secret.
    format = jsonencode({
      requestId               = "$context.requestId"
      sourceIp                = "$context.identity.sourceIp"
      caller                  = "$context.identity.userArn"
      requestTime             = "$context.requestTime"
      httpMethod              = "$context.httpMethod"
      resourcePath            = "$context.resourcePath"
      status                  = "$context.status"
      integrationErrorMessage = "$context.integrationErrorMessage"
      integrationLatency      = "$context.integrationLatency"
    })
  }

  tags       = local.tags
  depends_on = [aws_cloudwatch_log_group.fixture]
}

# =============================================================================
# Provenance handoff to the fixture gateway
# =============================================================================
# The fixture pod must be configured with the SAME value this edge injects, or
# every internal call 403s. A SecureString parameter under a run-scoped path is
# the handoff: the fixture tooling reads it with --with-decryption when rendering
# the fixture ConfigMap, so the secret never transits an issue comment, a
# workflow log, or a published artifact.
#
# It is NOT a Terraform output (see outputs.tf): outputs land in state artifacts
# and console output, and #5836 requires secrets stay out of both.
resource "aws_ssm_parameter" "fixture_provenance_secret" {
  count = local.enabled ? 1 : 0

  name        = "/adp/${var.environment}/gateway/fixture/${var.run_nonce}/apigw-provenance-secret"
  description = "DISPOSABLE per-run fixture edge provenance proof (issue #5836, run ${var.run_nonce}). Not the ordinary gateway secret."
  type        = "SecureString"
  value       = random_password.fixture_edge_provenance[0].result

  tags = local.tags
}
