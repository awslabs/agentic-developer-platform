# Platform isolation — the requirement confirmed 2026-09-16.
# Issue #5042 (U3), EPIC #4910.
#
# THE SENTENCE THIS FILE EXISTS TO ANSWER
#
#   "A separate state key alone does not prove resource isolation."
#
# tests/backend.tftest.hcl covers the state key. It is necessary and it is not sufficient:
# a module could hold its own state object and still create, adopt or destroy a platform
# resource. So this file asserts the properties that make the isolation claim true of the
# resources themselves.
#
# WHAT IS ASSERTED HERE
#
#   1. Every runtime identity is scoped to a NAMED service account in a NAMED namespace.
#      A trust policy scoped only to the cluster's OIDC provider is assumable by ANY pod
#      in the cluster, including core ADP pods — the domain app would then be a privilege
#      escalation path into a platform role, which is the failure that matters most here.
#   2. The namespace is domain-owned and the scoping tracks the variable rather than being
#      hardcoded, so a namespace change cannot leave a stale wildcard behind.
#   3. Nothing this module grants can reach a gateway-owned or platform-owned resource:
#      secrets by ARN, parameters by path, ECR by repository.
#   4. GPU workloads do not default onto the ADP management cluster.
#   5. Every resource name is domain-prefixed, so an operator reading a plan or a bill can
#      tell domain-owned resources from platform-owned ones.
#
# WHAT A PLAN CANNOT ASSERT, AND WHERE IT IS COVERED INSTEAD
#
# The strongest form of the claim is a NEGATIVE — "this module declares no VPC, no EKS
# cluster, no RDS instance, no gateway resource". `terraform test` cannot express that:
# referencing a resource the configuration does not declare is a configuration error, not
# a failed assertion, so there is no way to assert an absence. tests/test_platform_isolation.py
# enumerates the declared resource types as text and fails on any platform-owned type.
#
# Neither half is redundant. A module could declare only domain resource types and still
# hand out a cluster-wide role (caught here); or scope every policy perfectly and still
# take an RDS instance into its state (caught there).

mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

variables {
  environment          = "dev"
  aws_region           = "us-east-1"
  account_id           = "111122223333"
  cors_allowed_origins = ["https://superplane.dev.adp.internal"]
  database_secret_name = "adp/dev/superplane/database"
  jwt_secret_name      = "adp/dev/superplane/jwt-signing-key"
}

override_data {
  target = data.aws_caller_identity.current
  values = {
    account_id = "111122223333"
  }
}

override_data {
  target = data.terraform_remote_state.platform
  values = {
    outputs = {
      eks_oidc_provider_arn = "arn:aws:iam::111122223333:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E"
      eks_oidc_issuer       = "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E"
    }
  }
}

# ---------------------------------------------------------------------------
# 1. Runtime identities are scoped to named service accounts, not to the cluster.
#
# The distinction is invisible in a rendered policy unless you look for it: both the scoped
# and the unscoped version name the same Federated principal. What separates them is the
# presence of a `sub` condition, so that is asserted explicitly rather than inferred.
# ---------------------------------------------------------------------------
run "control_plane_trust_is_scoped_to_named_service_accounts" {
  command = plan

  # The principal is the platform's OIDC provider, read from platform state — this module
  # does not create an identity provider of its own.
  assert {
    condition = alltrue([
      for p in data.aws_iam_policy_document.control_plane_assume.statement[0].principals :
      p.type == "Federated"
    ])
    error_message = "the control-plane role must be assumable only through EKS web identity federation."
  }

  # A `sub` condition must exist. Without it the role is assumable by every pod in the
  # cluster — the escalation path this requirement is about.
  assert {
    condition = length([
      for c in data.aws_iam_policy_document.control_plane_assume.statement[0].condition :
      c if endswith(c.variable, ":sub")
    ]) == 1
    error_message = "the trust policy must carry an OIDC 'sub' condition; without one it is assumable by any pod in the cluster, including core ADP pods."
  }

  # And the subjects it admits are exactly the two named service accounts in the domain
  # namespace — no more, and none of them a wildcard. Compared as a set rather than
  # filtered with alltrue(), which would pass vacuously if the condition were removed (see
  # the note on the namespace run below).
  #
  # Joined into a string before comparing: `condition.values` is a set, and comparing it to
  # a tuple literal makes Terraform warn "LHS and RHS values are of different types" and
  # evaluate false regardless of the contents — a comparison that always fails is no more
  # use than one that always passes.
  assert {
    condition = join(",", sort(flatten([
      for c in data.aws_iam_policy_document.control_plane_assume.statement[0].condition :
      c.values if endswith(c.variable, ":sub")
      ]))) == join(",", [
      "system:serviceaccount:superplane:superplane-api",
      "system:serviceaccount:superplane:superplane-controller",
    ])
    error_message = "the trust policy must admit exactly the two named domain service accounts — no extra subject, no other namespace, no wildcard."
  }

  # `aud` pins the token audience to STS. Without it a token minted for a different
  # audience — a projected token for some other in-cluster service — could be replayed
  # against AWS.
  assert {
    condition = length([
      for c in data.aws_iam_policy_document.control_plane_assume.statement[0].condition :
      c if endswith(c.variable, ":aud") && contains(c.values, "sts.amazonaws.com")
    ]) == 1
    error_message = "the trust policy must pin the token audience to sts.amazonaws.com."
  }

  # StringEquals, not StringLike: StringLike would make every value above a pattern, and
  # the no-wildcard assertion would then be checking the wrong thing.
  assert {
    condition = alltrue([
      for c in data.aws_iam_policy_document.control_plane_assume.statement[0].condition :
      c.test == "StringEquals"
    ])
    error_message = "OIDC conditions must use StringEquals — StringLike would reintroduce pattern matching on the subject."
  }
}

# The SkyPilot role is the one with the larger blast radius (it launches compute), so its
# trust boundary is asserted separately rather than assumed to match.
run "skypilot_trust_is_scoped_to_its_own_service_account" {
  command = plan

  # Counted, not `alltrue`-filtered — see the note on the namespace run below for why a
  # filtered alltrue() passes when the condition it describes has been deleted. Exactly one
  # admitted subject: the SkyPilot service account and nothing else.
  assert {
    condition = join(",", sort(flatten([
      for c in data.aws_iam_policy_document.skypilot_assume.statement[0].condition :
      c.values if endswith(c.variable, ":sub")
    ]))) == "system:serviceaccount:skypilot:skypilot-api"
    error_message = "the SkyPilot role must be assumable only by the skypilot-api service account in the skypilot namespace."
  }

  # Two roles, not one. The API pod must not hold the compute-launching identity.
  assert {
    condition     = aws_iam_role.skypilot.name != aws_iam_role.control_plane.name
    error_message = "the SkyPilot compute identity must be separate from the API/controller identity."
  }

  # The compute grant is an unresolved seam and must stay visibly empty by default —
  # not approximated with a placeholder policy (see irsa.tf).
  assert {
    condition     = length(var.skypilot_compute_policy_arns) == 0
    error_message = "no compute policy may be granted by default; the grant is attached by whoever resolves the target account and spend authorization (U19)."
  }
}

# ---------------------------------------------------------------------------
# 2. The scoping tracks the namespace variable.
#
# Hardcoding "superplane" into the trust policy would pass the run above and then silently
# admit nothing (or the wrong thing) once the namespace changed. This run changes the
# namespace and checks the subject follows.
#
# NOTE ON THE `length(...) > 0` GUARDS: each assertion below counts matching subjects
# instead of wrapping `alltrue()` around a filtered list. Mutation-tested during
# development — with the plain `alltrue(flatten([... if endswith(c.variable, ":sub")]))`
# form, DELETING the `sub` condition entirely made this run PASS, because the filter
# produced an empty list and `alltrue([])` is true. The run above catches that deletion, so
# the suite as a whole was still sound, but an assertion that passes when the thing it
# describes is absent is not one to leave in place.
# ---------------------------------------------------------------------------
run "trust_scoping_follows_the_configured_namespace" {
  command = plan

  variables {
    namespace          = "superplane-staging"
    skypilot_namespace = "skypilot-staging"
  }

  assert {
    condition = length([
      for v in flatten([
        for c in data.aws_iam_policy_document.control_plane_assume.statement[0].condition :
        c.values if endswith(c.variable, ":sub")
      ]) : v if startswith(v, "system:serviceaccount:superplane-staging:")
    ]) == 2
    error_message = "both OIDC subjects (api, controller) must be derived from var.namespace, not hardcoded."
  }

  assert {
    condition = length([
      for v in flatten([
        for c in data.aws_iam_policy_document.skypilot_assume.statement[0].condition :
        c.values if endswith(c.variable, ":sub")
      ]) : v if startswith(v, "system:serviceaccount:skypilot-staging:")
    ]) == 1
    error_message = "the SkyPilot OIDC subject must be derived from var.skypilot_namespace."
  }
}

# A core ADP namespace is rejected outright. "Domain code must not run in a platform
# namespace" is otherwise only a convention, and a domain app sharing adp-gateway's
# namespace would share its network policies and its service-account surface.
run "a_core_adp_namespace_is_rejected" {
  command = plan

  variables {
    namespace = "adp-gateway"
  }

  expect_failures = [var.namespace]
}

run "kube_system_is_rejected" {
  command = plan

  variables {
    namespace = "kube-system"
  }

  expect_failures = [var.namespace]
}

# ---------------------------------------------------------------------------
# 3. No grant reaches a platform- or gateway-owned resource.
#
# Asserted as "every resource ARN is narrower than the service" rather than by listing
# gateway ARNs, because the gateway's resource names are not this module's business and a
# hardcoded list here would rot. A wildcard is what would make gateway resources reachable.
# ---------------------------------------------------------------------------
run "grants_cannot_reach_platform_or_gateway_resources" {
  # `apply`, not `plan` — and the reason is the whole point of the wildcard sweep below.
  #
  # The ECR pull statement scopes itself to `[for r in aws_ecr_repository.superplane :
  # r.arn]`. Repository ARNs are computed, so at plan time that statement's `resources` is
  # unknown, and ANY expression that sweeps all four statements evaluates to unknown —
  # Terraform then reports "Unknown condition value" and skips every later run in the file.
  #
  # The tempting fix is to narrow the sweep to the statements whose resources come from
  # configuration. That is worse than it looks: it would leave the ECR statement — the one
  # statement whose resources are dynamic, and so the likeliest place for a `*` to be
  # introduced — as the only one not checked for a wildcard.
  #
  # So this run applies against the mocked provider instead. No AWS call is made; the mock
  # fabricates the computed ARNs, which makes all four statements knowable. A literal `*`
  # added to any statement is configuration, stays known, and still fails the count.
  command = apply

  # Secrets: scoped to the two named secrets. `secretsmanager:*` or `:secret:*` would
  # include the gateway's database credentials and GitHub App private key.
  assert {
    condition = alltrue([
      for arn in data.aws_iam_policy_document.control_plane.statement[0].resources :
      strcontains(arn, ":secret:adp/dev/superplane/")
    ])
    error_message = "secret access must be scoped to this domain app's own secret paths."
  }

  # Parameters: confined to this module's own SSM path. Gateway configuration lives
  # elsewhere under /adp/dev/, so a prefix of /adp/dev/* would read it.
  assert {
    condition = alltrue([
      for arn in data.aws_iam_policy_document.control_plane.statement[1].resources :
      strcontains(arn, ":parameter/adp/dev/superplane/")
    ])
    error_message = "parameter access must be confined to /adp/<env>/superplane/*, not to the whole /adp/<env>/ tree."
  }

  # The only `*` resource in the whole policy is ecr:GetAuthorizationToken, which AWS does
  # not support resource-level permissions for. Pinning the count means a future wildcard
  # added anywhere else fails here rather than passing as "there was already a star".
  assert {
    condition = length(flatten([
      for s in data.aws_iam_policy_document.control_plane.statement :
      [for r in s.resources : r if r == "*"]
    ])) == 1
    error_message = "the only permitted '*' resource is ecr:GetAuthorizationToken, which has no resource-level permissions. Any other wildcard grant breaks the isolation claim."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.control_plane.statement :
      s.sid == "EcrAuth" if contains(s.resources, "*")
    ])
    error_message = "a '*' resource is only acceptable on the ECR authorization-token statement."
  }

  # SkyPilot's own grant is narrower still: its state-backend secret and nothing else.
  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.skypilot.statement :
      !contains(s.resources, "*")
    ])
    error_message = "the SkyPilot role must hold no wildcard resource grant."
  }
}

# What this module WRITES is also confined to its own path — isolation is not only about
# reads. A parameter written outside this prefix could overwrite platform configuration.
run "everything_written_stays_under_the_domain_prefix" {
  command = plan

  assert {
    condition = alltrue([
      for name in [
        aws_ssm_parameter.control_plane_role_arn.name,
        aws_ssm_parameter.skypilot_role_arn.name,
        aws_ssm_parameter.namespace.name,
        aws_ssm_parameter.cors_allowed_origins.name,
        aws_ssm_parameter.database_secret_name.name,
        aws_ssm_parameter.jwt_secret_name.name,
        aws_ssm_parameter.skypilot_image.name,
        aws_ssm_parameter.workspace_cluster_context.name,
      ] : startswith(name, "/adp/dev/superplane/")
    ])
    error_message = "every parameter this module writes must live under /adp/<env>/superplane/ — writing outside it could overwrite platform or gateway configuration."
  }
}

# ---------------------------------------------------------------------------
# 4. GPU workloads do not default onto the ADP management cluster.
#
# The requirement names this explicitly. The failure mode is a default that resolves to
# "the cluster I am currently talking to", which is the management cluster — so the default
# is empty, and empty is published as the explicit sentinel "none" rather than as an empty
# string a consumer might treat as unset-and-therefore-current.
# ---------------------------------------------------------------------------
run "gpu_workloads_have_no_default_cluster_target" {
  command = plan

  assert {
    condition     = var.workspace_cluster_context == ""
    error_message = "workspace_cluster_context must default to empty — a default context would schedule GPU workloads onto whichever cluster is current, i.e. the ADP management cluster."
  }

  assert {
    condition     = aws_ssm_parameter.workspace_cluster_context.value == "none"
    error_message = "an unconfigured workspace target must be published as the explicit sentinel 'none', so a consumer cannot read it as 'unset, use the current cluster'."
  }
}

run "a_configured_workspace_cluster_is_published_verbatim" {
  command = plan

  variables {
    workspace_cluster_context = "arn:aws:eks:us-east-1:111122223333:cluster/adp-dev-superplane-gpu"
  }

  assert {
    condition     = aws_ssm_parameter.workspace_cluster_context.value == "arn:aws:eks:us-east-1:111122223333:cluster/adp-dev-superplane-gpu"
    error_message = "a configured workspace cluster context must be published unchanged for the rollout to select."
  }
}

# ---------------------------------------------------------------------------
# 5. Every resource is identifiably domain-owned.
#
# This is what makes the isolation claim auditable after the fact: reading a plan, a bill or
# an IAM listing, an operator can tell which resources belong to the domain app. An
# unprefixed resource is either not ours or is one we should not be creating.
# ---------------------------------------------------------------------------
run "all_resource_names_are_domain_prefixed" {
  command = plan

  assert {
    condition = alltrue([
      for name in [
        aws_iam_role.control_plane.name,
        aws_iam_role.skypilot.name,
        aws_iam_role_policy.control_plane.name,
        aws_iam_role_policy.skypilot.name,
      ] : startswith(name, "adp-dev-superplane-")
    ])
    error_message = "every IAM resource must carry the adp-<env>-superplane- prefix so domain-owned resources are distinguishable from platform-owned ones."
  }

  # ECR repository names come from U2's lock, so they are checked for the domain prefix
  # rather than for an exact list — the lock owns which images exist, this module owns that
  # they are recognisably Superplane's.
  assert {
    condition = alltrue([
      for name, repo in aws_ecr_repository.superplane :
      startswith(name, "adp-superplane-")
    ])
    error_message = "ECR repositories must be domain-prefixed; they are created and destroyed with the domain app, not with platform ECR."
  }

  # And there really are repositories — an empty set would pass every assertion above
  # vacuously, which is the same "zero items ran" hazard the domain CI docs call out.
  assert {
    condition     = length(aws_ecr_repository.superplane) >= 3
    error_message = "the three Superplane image repositories from releases/superplane.lock.yaml must be created here; an empty set would satisfy the prefix check vacuously."
  }
}
