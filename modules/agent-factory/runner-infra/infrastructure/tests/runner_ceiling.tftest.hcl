# Legacy runner installation — permission ceiling and escalation paths (S14, #5613).
#
# #4725 reported that this CI runner role could reach account administration by
# creating roles, attaching policies, or assuming another role. PR #5767 (A18,
# #5674) replaced both installations' hand-written grants with the shared
# runner-runtime-policy module: an enumerated allowlist plus a NotAction deny that
# refuses everything outside it.
#
# HOW THE DENIAL WORKS, because it decides how these assertions are written:
# iam:CreateRole, iam:AttachRolePolicy and the sts:AssumeRole* variants are refused
# because they are ABSENT FROM THE ALLOWLIST, not because any statement names them.
# Grepping this tree for "CreateRole" returns nothing. A test that looked for a deny
# statement naming those actions would therefore fail misleadingly, so the
# assertions below check the mechanism that actually blocks them: the ceiling's
# action list and the NotAction deny that caps it.
#
# These tests read rendered policy documents with mocked providers. They are
# evidence about configuration only — no IAM has been applied to any account, and
# nothing here substitutes for post-apply simulation.

mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_region" { defaults = { name = "eu-west-1" } }
}
mock_provider "kubernetes" {}
mock_provider "tls" {}

variables {
  github_org = "example-org"
  # Set explicitly rather than relying on the default: the policies interpolate
  # var.aws_region (not the aws_region data source), so pinning it here keeps the
  # resource-scope assertions deterministic and independent of that default.
  aws_region = "eu-west-1"
}

# See runner_trust.tftest.hcl: this standalone stack's policies key on cluster/OIDC
# attributes unknown until apply, so they are substituted at plan time.
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

# A managed policy's ARN is generated on create, so the role's permissions_boundary
# reference is unknown at plan time. Fixing the ARN here is what makes the
# "boundary is actually attached" assertion below possible without applying.
override_resource {
  target          = aws_iam_policy.runner_boundary
  override_during = plan
  values          = { arn = "arn:aws:iam::123456789012:policy/github-arc-runner-runner-boundary" }
}

run "the_ceiling_is_attached_to_the_runner_role" {
  command = plan

  # An excellent boundary that is not attached to the role constrains nothing, and
  # that omission is invisible in review. Every other assertion in this file is
  # conditional on this one holding.
  assert {
    condition     = aws_iam_role.runner.permissions_boundary == aws_iam_policy.runner_boundary.arn
    error_message = "The legacy runner role does not inherit its permissions boundary, so none of the denies below constrain it."
  }

  # Both managed policies must land on the same bounded role; a grant attached to
  # some other role would escape this ceiling entirely.
  assert {
    condition = alltrue([
      aws_iam_role_policy_attachment.runner_base.role == aws_iam_role.runner.name,
      aws_iam_role_policy_attachment.runner_services.role == aws_iam_role.runner.name,
    ])
    error_message = "A legacy runner managed policy is attached to a role other than the bounded runner role."
  }
}

run "the_legacy_runner_cannot_mint_or_assume_a_broader_identity" {
  command = plan

  # #4725's core claim. Assert against the ceiling's own action list: these APIs are
  # unreachable precisely because they are not in it.
  assert {
    condition = alltrue([for action in [
      "iam:CreateRole", "iam:CreateUser", "iam:AttachRolePolicy", "iam:AttachUserPolicy",
      "iam:PutRolePolicy", "iam:UpdateAssumeRolePolicy", "iam:CreatePolicy",
      "iam:CreatePolicyVersion", "iam:DeleteRolePermissionsBoundary",
      "iam:PutRolePermissionsBoundary", "iam:CreateAccessKey", "iam:CreateLoginProfile",
      "sts:AssumeRole", "sts:AssumeRoleWithSAML", "sts:AssumeRoleWithWebIdentity",
      ] :
      !contains(one([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if s.Sid == "RuntimeApiCeiling"]).Action, action)
    ])
    error_message = "The legacy runner ceiling now permits an IAM mutation or role-assumption API, restoring the #4725 escalation path."
  }

  # The NotAction deny is what turns "absent from the allowlist" into a refusal that
  # also survives a resource policy granting directly to the session.
  assert {
    condition = alltrue([for action in [
      "iam:CreateRole", "iam:AttachRolePolicy", "iam:PutRolePolicy",
      "iam:UpdateAssumeRolePolicy", "iam:PutRolePermissionsBoundary",
      "sts:AssumeRole", "sts:AssumeRoleWithSAML", "sts:AssumeRoleWithWebIdentity",
      "lambda:UpdateFunctionCode", "lambda:InvokeFunction", "codebuild:UpdateProject",
      "codebuild:CreateProject", "eks:CreateAccessEntry", "eks:AssociateAccessPolicy",
      "kms:PutKeyPolicy", "s3:PutBucketPolicy", "ecr:PutImage",
      "secretsmanager:GetSecretValue", "ssm:GetParametersByPath",
      ] :
      !contains(one([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if s.Sid == "DenyOutsideRuntimeActions"]).NotAction, action)
    ])
    error_message = "An escalation API escaped the legacy runner's NotAction ceiling: the runner can mint an identity, act through a privileged service, or promote an image."
  }

  # Belt and braces: the boundary also names the assume-role variants in an explicit
  # Deny, which cannot be out-voted by any attached policy.
  assert {
    condition = toset(flatten([
      for statement in jsondecode(aws_iam_policy.runner_boundary.policy).Statement :
      flatten([statement.Action])
      if statement.Effect == "Deny" && statement.Sid == "DenyPrivilegeEscalation"
      ])) == toset([
      "sts:AssumeRole", "sts:AssumeRoleWithSAML", "sts:AssumeRoleWithWebIdentity",
    ])
    error_message = "The legacy runner boundary no longer explicitly denies the full role-assumption set."
  }

  # And no attached grant may allow identity mutation or assumption directly.
  assert {
    condition = alltrue(flatten([
      for policy in [aws_iam_policy.runner_base.policy, aws_iam_policy.runner_services.policy] : [
        for statement in jsondecode(policy).Statement : [
          for action in flatten([statement.Action]) :
          !contains([
            "iam:CreateRole", "iam:AttachRolePolicy", "iam:PutRolePolicy",
            "iam:UpdateAssumeRolePolicy", "sts:AssumeRole",
            "sts:AssumeRoleWithSAML", "sts:AssumeRoleWithWebIdentity",
          ], action)
          if statement.Effect == "Allow"
        ]
      ]
    ]))
    error_message = "A legacy runner identity policy grants role creation, policy attachment or role assumption."
  }

  # No service-level wildcard: "iam:*" would satisfy every assertion above while
  # granting everything they exist to forbid.
  assert {
    condition = alltrue(flatten([
      for policy in [aws_iam_policy.runner_base.policy, aws_iam_policy.runner_services.policy] : [
        for statement in jsondecode(policy).Statement : [
          for action in flatten([statement.Action]) : !can(regex("[*?]", action))
        ]
      ]
    ]))
    error_message = "A legacy runner grant uses an action wildcard, so its enumerated ceiling means nothing."
  }
}

run "passrole_is_confined_to_one_service_role_and_one_service" {
  command = plan

  # iam:PassRole is the ONE IAM API the ceiling allows, and the #4725 path that
  # needs no resource creation: handing an existing privileged role to a service the
  # runner can invoke reaches administrator. Both halves — which role, and to which
  # service — must be pinned, so both get an allow-side and a deny-side assertion.
  assert {
    condition = one([for s in jsondecode(aws_iam_policy.runner_services.policy).Statement : s if s.Sid == "PassSmokeRole"]).Resource == [
      "arn:aws:iam::123456789012:role/adp-prod-codebuild-gateway-pr"
    ]
    error_message = "The legacy runner may pass a role other than the service-only gateway PR identity."
  }

  assert {
    condition = one([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if s.Sid == "DenyOtherPassSmokeRoleResources"]).NotResource == [
      "arn:aws:iam::123456789012:role/adp-prod-codebuild-gateway-pr"
    ]
    error_message = "Passing any other role is not explicitly denied, so another attached policy could re-enable it."
  }

  # StringNotEquals also denies the ABSENT condition key, so omitting
  # iam:PassedToService cannot smuggle the role to a different service.
  assert {
    condition = one([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if s.Sid == "DenyOtherPassService"]) == {
      Sid       = "DenyOtherPassService", Effect = "Deny", Action = ["iam:PassRole"], Resource = "*",
      Condition = { StringNotEquals = { "iam:PassedToService" = "codebuild.amazonaws.com" } }
    }
    error_message = "The passed role is not pinned to CodeBuild, so it could be handed to Lambda or EC2 instead."
  }

  # The build the runner may start must use the non-publishing service role, and the
  # retry/batch APIs must stay outside the ceiling or they would bypass that pin.
  assert {
    condition = one([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if s.Sid == "DenyOtherBuildRole"]).Condition == {
      StringNotEquals = { "codebuild:serviceRole" = "arn:aws:iam::123456789012:role/adp-prod-codebuild-gateway-pr" }
    }
    error_message = "An omitted or changed serviceRole override could inherit the CodeBuild project's publishing identity."
  }

  assert {
    condition = alltrue([for action in ["codebuild:RetryBuild", "codebuild:StartBuildBatch"] :
      !contains(one([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if s.Sid == "RuntimeApiCeiling"]).Action, action)
    ])
    error_message = "Retry/batch build APIs are reachable and bypass the StartBuild service-role condition."
  }
}

run "the_legacy_runner_cannot_read_another_tenants_vault" {
  command = plan

  # Vault paths carry NO environment segment, so a grant on "adp/*" reached every
  # customer's stored credentials. PATH SHAPE IS LOAD-BEARING: an env-segmented deny
  # (adp/prod/users/*) would match no real vault secret while reading in review as
  # though it closed the hole — the worst failure mode for a security control.
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
      "arn:aws:secretsmanager:*:123456789012:secret:adp/*/tenants/*",
    ])
    error_message = "The legacy vault deny is not on the environment-less vault namespaces, so it may match nothing at all."
  }

  assert {
    condition = toset(flatten([
      for statement in jsondecode(aws_iam_policy.runner_boundary.policy).Statement :
      flatten([statement.Action])
      if statement.Sid == "DenyTenantVaultSecrets"
    ])) == toset(["secretsmanager:*"])
    error_message = "The legacy vault deny does not block the full Secrets Manager API on tenant paths."
  }

  assert {
    condition = alltrue([for statement in jsondecode(aws_iam_policy.runner_services.policy).Statement :
      alltrue([for action in flatten([statement.Action]) : !startswith(action, "secretsmanager:")])
    ])
    error_message = "The default legacy runner role carries ambient secret access."
  }
}

# Without this, a future change could satisfy every deny-side assertion above by
# breaking the runner completely — a permission ceiling that blocks the runner's own
# job is not a passing result.
run "normal_runner_work_is_still_permitted" {
  command = plan

  assert {
    condition = alltrue([for sid in [
      "ImagePull", "ImageAuthentication", "OwnLogs", "ModelInference",
      "Identity", "GatewayEndpoint", "StartSmokeBuild", "SafeSmokeBuild", "OwnSmokeSource",
      ] :
      contains([for s in jsondecode(aws_iam_policy.runner_base.policy).Statement : s.Sid], sid)
    ])
    error_message = "A capability the runner needs for ordinary CI work was dropped from the legacy grants."
  }

  # Own log stream only, and the configuration parameter for this environment.
  assert {
    condition = one([for s in jsondecode(aws_iam_policy.runner_base.policy).Statement : s if s.Sid == "OwnLogs"]).Resource == [
      "arn:aws:logs:eu-west-1:123456789012:log-group:/adp/runner/adp-prod:*"
    ]
    error_message = "The legacy runner cannot write its own log stream, or can write outside its own log group."
  }

  assert {
    condition = one([for s in jsondecode(aws_iam_policy.runner_base.policy).Statement : s if s.Sid == "GatewayEndpoint"]).Resource == [
      "arn:aws:ssm:eu-west-1:123456789012:parameter/adp/prod/gateway/apigw-invoke-url"
    ]
    error_message = "The legacy runner's gateway endpoint parameter scope changed."
  }

  # Rendered size is a real deploy risk here: these policies were already split in
  # two (#1204) after exceeding an IAM size limit once.
  assert {
    condition = alltrue([for policy in [
      aws_iam_policy.runner_base.policy,
      aws_iam_policy.runner_services.policy,
      aws_iam_policy.runner_boundary.policy,
    ] : length(policy) <= 6144])
    error_message = "A rendered legacy runner policy exceeds IAM's 6144-character managed-policy quota and would fail to apply."
  }
}
