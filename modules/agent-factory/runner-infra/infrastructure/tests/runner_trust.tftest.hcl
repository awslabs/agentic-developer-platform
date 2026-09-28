# Legacy runner installation — trust boundary of the shared runner role (S14, #5613).
#
# PR #5767 (A18, #5674) narrowed this role's trust from StringLike on
# "system:serviceaccount:arc-runners-*:github-runner-sa" to an enumerated list.
# That change landed in this tree but was never covered here: this root module
# had no tests at all, while its sibling (infra/modules/runner-iam) has fifteen.
# automation-trust-ci.yml triggers on changes under runner-infra/** yet its
# Terraform loop did not include this directory, so widening the condition below
# ran CI that asserted nothing about the file changed. These tests close that gap.
#
# Offline by construction: providers are mocked and the cluster/OIDC attributes the
# trust policy is keyed on are substituted, so no credentials, no remote backend
# and no AWS calls are involved. Nothing here is evidence about live IAM.

mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_region" { defaults = { name = "eu-west-1" } }
}
mock_provider "kubernetes" {}
mock_provider "tls" {}

variables {
  github_org = "example-org"
}

# This is a standalone stack: it creates its own EKS cluster, and the trust policy's
# condition KEY is derived from that cluster's OIDC issuer. Both are unknown until
# apply, so without these stand-ins the assume-role document cannot be read during a
# plan and the policy would be untestable without live AWS. override_during = plan
# supplies fixed values at plan time. The whole resource object is replaced, so
# certificate_authority is included for the kubernetes provider and outputs.tf.
override_resource {
  target          = aws_eks_cluster.main
  override_during = plan
  values = {
    name                  = "github-arc-runner-eks"
    endpoint              = "https://example.eks.amazonaws.com"
    identity              = [{ oidc = [{ issuer = "https://oidc.eks.eu-west-1.amazonaws.com/id/TEST" }] }]
    certificate_authority = [{ data = "TUlJQ0ZBS0U=" }]
  }
}

override_resource {
  target          = aws_iam_openid_connect_provider.eks
  override_during = plan
  values = {
    arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/TEST"
    url = "https://oidc.eks.eu-west-1.amazonaws.com/id/TEST"
  }
}

run "trust_names_exact_service_accounts_not_a_pattern" {
  command = plan

  # The regression itself. A StringLike condition on `sub` is what let namespace
  # creation confer trust, so assert the operator is absent rather than only
  # checking today's value — a future edit could reintroduce the pattern with the
  # same trusted namespace still listed.
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role.runner.assume_role_policy).Statement :
      !can(statement.Condition.StringLike)
    ])
    error_message = "The legacy runner trust policy uses a StringLike condition again, so a namespace name can confer trust."
  }

  assert {
    condition = !can(regex("[*?]", join(",", flatten([
      jsondecode(aws_iam_role.runner.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.eu-west-1.amazonaws.com/id/TEST:sub"]
    ]))))
    error_message = "A trusted subject contains a wildcard; StringEquals does not glob, so this matches nothing or invites reverting to StringLike."
  }

  # Exactly the namespace this stack's own eks.tf binds the shared role to — not the
  # per-repository arc-runners-<repo> namespaces, which get their own narrower roles.
  assert {
    condition = flatten([
      jsondecode(aws_iam_role.runner.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.eu-west-1.amazonaws.com/id/TEST:sub"]
      ]) == [
      "system:serviceaccount:arc-runners:github-runner-sa"
    ]
    error_message = "The default trusted-subject set is not exactly the shared runner's own service account."
  }

  # A federated trust policy conditioned only on `sub` accepts a token minted for a
  # different audience, so the audience pin is a separate control worth its own check.
  assert {
    condition = (
      jsondecode(aws_iam_role.runner.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.eu-west-1.amazonaws.com/id/TEST:aud"]
      == "sts.amazonaws.com"
    )
    error_message = "The legacy runner trust policy does not pin the token audience."
  }

  # Federation must be the only way in: a bare account/principal statement would
  # bypass every condition above.
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role.runner.assume_role_policy).Statement :
      can(statement.Principal.Federated) && statement.Action == "sts:AssumeRoleWithWebIdentity"
    ])
    error_message = "The legacy runner role trusts a principal other than the federated OIDC provider."
  }
}

run "a_new_conventionally_named_namespace_is_not_trusted" {
  command = plan

  # THE POINT OF THE CHANGE. Namespaces are created per repository by
  # scripts/onboard-repo.sh with a fixed service-account name, so under the old
  # "arc-runners-*" pattern onboarding a repository silently made its runner trusted
  # by this shared role. Here the trusted set is enumerated, so the name is absent.
  assert {
    condition = !contains(
      flatten([
        jsondecode(aws_iam_role.runner.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.eu-west-1.amazonaws.com/id/TEST:sub"]
      ]),
      "system:serviceaccount:arc-runners-attacker:github-runner-sa"
    )
    error_message = "A namespace nobody authorised is trusted by the legacy runner role."
  }

  # And the old pattern WOULD have matched that name — otherwise the assertion above
  # proves only that a string is missing from a list, not that behaviour changed.
  # Deriving the pattern from the rendered policy keeps this tied to the configuration
  # instead of comparing two literals.
  assert {
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

  # Trust stays extensible, but only by naming the namespace — the review step the
  # wildcard skipped. Also proves the narrowing does not lock out a legitimate
  # second runner namespace.
  variables {
    runner_trusted_namespaces = ["arc-runners", "arc-runners-org"]
  }

  assert {
    condition = toset(flatten([
      jsondecode(aws_iam_role.runner.assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.eu-west-1.amazonaws.com/id/TEST:sub"]
      ])) == toset([
      "system:serviceaccount:arc-runners:github-runner-sa",
      "system:serviceaccount:arc-runners-org:github-runner-sa",
    ])
    error_message = "An explicitly listed namespace is not trusted, or the shared runner's own namespace was dropped."
  }
}

# The variable guards are the reason a reviewer can trust the StringEquals list:
# without them a "*" entry silently matches nothing, and the tempting repair is to
# switch the operator back to StringLike.
run "a_wildcard_namespace_entry_is_rejected" {
  command = plan
  variables { runner_trusted_namespaces = ["arc-runners-*"] }
  expect_failures = [var.runner_trusted_namespaces]
}

run "an_empty_trusted_namespace_list_is_rejected" {
  command = plan
  variables { runner_trusted_namespaces = [] }
  expect_failures = [var.runner_trusted_namespaces]
}
