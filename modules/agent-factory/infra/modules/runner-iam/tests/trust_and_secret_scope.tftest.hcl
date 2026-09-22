# The runner role must trust named callers and reach only its own secrets — A18 (#5674).
#
# Two findings, both on an identity that executes instructions authored outside
# the organisation (workflow files, issue text, PR contents):
#
#   1. Trust was StringLike on "system:serviceaccount:${var.runner_namespace}*:
#      github-runner-sa". The "*" sat OUTSIDE the interpolation, so the default
#      matched "arc-runners" AND every "arc-runners-<anything>". Creating a
#      conventionally named namespace WAS being trusted by this role.
#
#   2. The role could read and write secret:adp/* — the same prefix under which
#      per-customer vault secrets are minted with no environment segment.
#
# These assertions read the rendered policy documents. No live IAM is touched or
# claimed; see the issue's scope note.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
}

variables {
  environment       = "test"
  name_prefix       = "adp-test"
  aws_region        = "eu-west-1"
  oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/TEST"
  oidc_issuer       = "https://oidc.eks.eu-west-1.amazonaws.com/id/TEST"
}

run "trust_names_exact_service_accounts_not_a_pattern" {
  command = plan

  # The regression itself: no pattern operator anywhere in the trust policy. A
  # StringLike condition on `sub` is what let namespace creation confer trust.
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role.runner.assume_role_policy).Statement :
      !can(statement.Condition.StringLike)
    ])
    error_message = "The runner trust policy still uses a StringLike condition, so a namespace name can confer trust."
  }

  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role.runner.assume_role_policy).Statement :
      !can(regex("[*?]", join(",", flatten([
        statement.Condition.StringEquals["oidc.eks.eu-west-1.amazonaws.com/id/TEST:sub"]
      ]))))
    ])
    error_message = "A trusted subject contains a wildcard character; StringEquals does not glob, so this matches nothing or invites reverting to StringLike."
  }

  # Only the runner's own namespace by default.
  assert {
    condition = flatten([
      jsondecode(aws_iam_role.runner.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.eu-west-1.amazonaws.com/id/TEST:sub"]
      ]) == [
      "system:serviceaccount:arc-runners:github-runner-sa"
    ]
    error_message = "The default trusted-subject set is not exactly the runner's own service account."
  }

  # A federated trust policy conditioned only on `sub` accepts a token minted for
  # a different audience.
  assert {
    condition = (
      jsondecode(aws_iam_role.runner.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.eu-west-1.amazonaws.com/id/TEST:aud"]
      == "sts.amazonaws.com"
    )
    error_message = "The runner trust policy does not pin the token audience."
  }
}

run "a_new_conventionally_named_namespace_is_not_trusted" {
  command = plan

  # THE POINT OF THE CHANGE. Under the old StringLike pattern
  # ("arc-runners*"), a namespace called "arc-runners-attacker" matched and its
  # github-runner-sa could assume this role. Here the trusted set is enumerated,
  # so the same name is absent — onboarding a namespace is no longer the same act
  # as being trusted by this role.
  assert {
    condition = !contains(
      flatten([
        jsondecode(aws_iam_role.runner.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.eu-west-1.amazonaws.com/id/TEST:sub"]
      ]),
      "system:serviceaccount:arc-runners-attacker:github-runner-sa"
    )
    error_message = "A namespace nobody authorised is trusted by the runner role."
  }

  # And the old pattern WOULD have matched it — otherwise the assertion above
  # proves only that a name is absent from a list, not that behaviour changed.
  assert {
    # The old condition was StringLike on "<namespace>*", so the pattern derived
    # from the real trusted subject still matches the unauthorised namespace.
    # Referencing the rendered policy keeps this tied to the configuration: if the
    # trusted subject stops being "arc-runners...", this stops reproducing and
    # must be revisited deliberately rather than passing on two literals.
    condition = can(regex(
      "^${trimsuffix(flatten([
        jsondecode(aws_iam_role.runner.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.eu-west-1.amazonaws.com/id/TEST:sub"]
      ])[0], ":github-runner-sa")}",
      "system:serviceaccount:arc-runners-attacker"
    ))
    error_message = "This test no longer reproduces the old pattern's reach; update it deliberately."
  }
}

run "an_additional_namespace_must_be_listed_explicitly" {
  command = plan

  # Trust is extensible, but only by naming the namespace — which is the review
  # step the wildcard skipped. This also proves the change does not lock out a
  # legitimate second runner namespace.
  variables {
    runner_trusted_namespaces = ["arc-runners-org"]
  }

  assert {
    condition = toset(flatten([
      jsondecode(aws_iam_role.runner.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.eu-west-1.amazonaws.com/id/TEST:sub"]
      ])) == toset([
      "system:serviceaccount:arc-runners:github-runner-sa",
      "system:serviceaccount:arc-runners-org:github-runner-sa",
    ])
    error_message = "An explicitly listed namespace is not trusted, or the runner's own namespace was dropped."
  }
}

run "the_runner_cannot_read_another_tenants_vault" {
  command = plan

  # Vault paths carry NO environment segment — adp/users/<sub>/...,
  # adp/teams/<id>/..., adp/orgs/<id>/..., adp/domain-apps/<app>/<org>/... — so
  # a grant on "adp/*" reached every customer's stored credentials. The boundary
  # denies those four namespaces; a Deny in a boundary cannot be out-voted by any
  # attached policy, which is why this lives here rather than in the grant.
  assert {
    condition = length([
      for statement in jsondecode(aws_iam_policy.runner_boundary.policy).Statement :
      statement
      if statement.Effect == "Deny" && statement.Sid == "DenyTenantVaultSecrets"
    ]) == 1
    error_message = "The runner boundary does not deny cross-tenant vault reads."
  }

  # PATH SHAPE IS LOAD-BEARING. An env-segmented Deny (adp/test/users/*) would
  # match no real vault secret while reading, in review, as though it closed the
  # hole — the worst failure mode for a security control. So assert the exact
  # env-less shape rather than merely "a Deny mentioning users".
  assert {
    condition = toset(flatten([
      for statement in jsondecode(aws_iam_policy.runner_boundary.policy).Statement :
      flatten([statement.Resource])
      if statement.Sid == "DenyTenantVaultSecrets"
      ])) == toset([
      "arn:aws:secretsmanager:*:123456789012:secret:adp/users/*",
      "arn:aws:secretsmanager:*:123456789012:secret:adp/teams/*",
      "arn:aws:secretsmanager:*:123456789012:secret:adp/orgs/*",
      "arn:aws:secretsmanager:*:123456789012:secret:adp/domain-apps/*",
    ])
    error_message = "The vault Deny is not on the four environment-less vault namespaces, so it may match nothing at all."
  }

  # A wildcard Deny on these exact resources blocks every current and future
  # read, mutation, deletion, restore, rotation, replication and resource-policy
  # action. This is intentionally stronger than enumerating today's write set.
  assert {
    condition = toset(flatten([
      for statement in jsondecode(aws_iam_policy.runner_boundary.policy).Statement :
      flatten([statement.Action])
      if statement.Sid == "DenyTenantVaultSecrets"
    ])) == toset(["secretsmanager:*"])
    error_message = "The vault Deny does not block the full Secrets Manager API on tenant paths."
  }

  assert {
    condition = toset(flatten([
      for statement in jsondecode(aws_iam_policy.runner_services.policy).Statement :
      flatten([statement.Resource])
      if statement.Sid == "SecretsManagerOps"
      ])) == toset([
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:bedrockgw-*",
      "arn:aws:secretsmanager:eu-west-1:123456789012:secret:adp/test/*",
    ])
    error_message = "The runner secret grant is not limited to its account, region and environment-owned prefixes."
  }

  assert {
    condition = !contains(flatten([
      for statement in jsondecode(aws_iam_policy.runner_services.policy).Statement :
      flatten([statement.Action])
      if statement.Sid == "SecretsManagerOps"
    ]), "secretsmanager:ListSecrets")
    error_message = "The runner can still enumerate every tenant secret because ListSecrets cannot be resource-scoped."
  }
}

run "the_active_runner_cannot_mint_or_assume_a_broader_identity" {
  command = plan

  assert {
    condition = alltrue([
      for action in local.privilege_escalation_actions :
      contains(flatten([
        for statement in jsondecode(aws_iam_policy.runner_boundary.policy).Statement :
        flatten([statement.Action])
        if statement.Effect == "Deny" && statement.Sid == "DenyPrivilegeEscalation"
      ]), action)
    ])
    error_message = "The active runner boundary does not deny the complete privilege-escalation action set."
  }

  assert {
    condition = alltrue(flatten([
      for policy in [aws_iam_policy.runner_base.policy, aws_iam_policy.runner_services.policy] : [
        for statement in jsondecode(policy).Statement : [
          for action in flatten([statement.Action]) :
          !contains(local.privilege_escalation_actions, action)
          if statement.Effect == "Allow"
        ]
      ]
    ]))
    error_message = "An active runner identity policy still grants identity mutation or role assumption."
  }

  assert {
    condition = length([
      for statement in jsondecode(aws_iam_policy.runner_base.policy).Statement : statement
      if statement.Sid == "IAMRolePolicyMgmt"
    ]) == 0
    error_message = "The active runner still carries the IAMRolePolicyMgmt write statement."
  }
}
