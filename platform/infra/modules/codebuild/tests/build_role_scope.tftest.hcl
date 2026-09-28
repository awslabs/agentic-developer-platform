# Build identities must not be administrators — A18 (#5674).
#
# The finding these lock down: all ten CodeBuild projects shared ONE IAM role
# with AdministratorAccess attached, and each executed a buildspec read from a
# zip in the shared Terraform state bucket. Anything that could place a zip
# there could run arbitrary AWS API calls as an administrator, and every
# project's compromise was every other project's compromise.
#
# These assertions are about the RENDERED policy documents, not about live IAM.
# No deployment is claimed — see the issue's scope note.

mock_provider "aws" {
  # aws_iam_policy_document is a provider-side data source, so under a mock
  # provider its `json` is a generated placeholder string — and the AWS provider
  # then rejects it as "not a JSON object" when it lands in assume_role_policy.
  # Supplying a real document keeps the trust policy out of scope for these
  # assertions (they are about grants and the boundary) without the mock
  # breaking the plan.
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  # A mocked aws_iam_policy gets a random string for `arn`, which the provider
  # then rejects when it lands in permissions_boundary ("invalid ARN: invalid
  # prefix"). The boundary ARN must therefore be ARN-shaped for the plan to
  # complete; the assertions below compare roles against
  # aws_iam_policy.codebuild_boundary.arn rather than this literal, so they still
  # test the wiring rather than the fixture.
  mock_resource "aws_iam_policy" {
    defaults = {
      arn = "arn:aws:iam::123456789012:policy/mock-boundary"
    }
  }

  # Same reason for roles: service_role must be ARN-shaped. Note this gives every
  # mocked role the SAME arn, so an assertion comparing arns would pass
  # vacuously — the per-project wiring below is therefore asserted on role
  # NAMES, which the module derives from the project key and are real.
  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/mock-role"
    }
  }
}

# Distinct from the global mock role ARN: catches service-role miswiring.
override_resource {
  target = aws_iam_role.project["superplane-executor"]
  values = { arn = "arn:aws:iam::123456789012:role/adp-test-codebuild-superplane-executor" }
}

variables {
  enabled_domain_apps        = ["superplane"]
  name_prefix                = "adp-test"
  state_bucket               = "adp-terraform-state-123456789012"
  account_id                 = "123456789012"
  aws_region                 = "us-east-1"
  ecr_registry               = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
  security_scans_bucket_arn  = "arn:aws:s3:::adp-security-scans-123456789012"
  security_scans_bucket_name = "adp-security-scans-123456789012"
}

run "no_build_identity_is_an_administrator" {
  command = apply

  # The literal regression. AdministratorAccess was attached to the shared role
  # via aws_iam_role_policy_attachment.codebuild_admin; that resource is gone,
  # so this asserts on what the module renders rather than on its absence: any
  # managed-policy attachment carrying an AWS-managed admin/power policy fails.
  assert {
    condition = length([
      for name, policy in aws_iam_role_policy.project :
      name if can(regex("(AdministratorAccess|PowerUserAccess)", policy.policy))
    ]) == 0
    error_message = "A per-project build policy references an AWS-managed administrator policy."
  }

  assert {
    condition = alltrue([
      for key, policy in aws_iam_role_policy.agent_context_image :
      !can(regex("(AdministratorAccess|PowerUserAccess)", policy.policy))
    ])
    error_message = "An agent-context image-build policy references an administrator policy."
  }

  # Each project gets its OWN role. A shared role means one project's buildspec
  # runs with every other project's permissions, which is how a single
  # compromised zip reached everything.
  assert {
    condition = length(distinct([
      for name, role in aws_iam_role.project : role.name
    ])) == length(local.projects)
    error_message = "Build projects do not each have a distinct IAM role."
  }

  # Each project's role NAME is derived from its own key, so no two projects can
  # resolve to the same identity. Asserted on names rather than ARNs because the
  # mock provider hands every role the same fixture ARN (see mock_resource above)
  # and an ARN comparison would pass without testing anything.
  assert {
    condition = alltrue([
      for name in keys(local.projects) :
      aws_iam_role.project[name].name == "adp-test-codebuild-${name}"
    ])
    error_message = "A build role's name is not derived from its own project key, so projects could share an identity."
  }

  assert {
    condition     = length(keys(aws_iam_role.project)) == length(local.projects)
    error_message = "The number of build roles does not match the number of build projects."
  }

  assert {
    condition = length(distinct([
      for key, role in aws_iam_role.agent_context_image : role.name
    ])) == length(local.agent_context_images)
    error_message = "Agent-context image projects do not each have a distinct IAM role."
  }
}

run "no_build_role_can_escalate_its_own_privileges" {
  command = apply

  # A build that can create a role and attach a policy to it does not need to be
  # GRANTED admin — it mints an admin role and passes it to a service it can
  # invoke. So no identity-mutating action may appear in any Allow, and the
  # boundary must Deny the set outright (a Deny in a boundary cannot be
  # out-voted by any policy later attached to a bounded role).
  assert {
    condition = alltrue(flatten([
      for name, policy in aws_iam_role_policy.project : [
        for statement in jsondecode(policy.policy).Statement : [
          for action in try(flatten([statement.Action]), []) :
          !can(regex("^(iam:(Create|Attach|Put|Update|Delete|Pass|Add)|sts:AssumeRole)", action))
          if statement.Effect == "Allow"
        ]
      ]
    ]))
    error_message = "A per-project build policy allows an identity-mutating or role-assuming action."
  }

  assert {
    condition = alltrue([
      for action in [
        "iam:CreateRole",
        "iam:CreatePolicy",
        "iam:AttachRolePolicy",
        "iam:PutRolePolicy",
        "iam:PassRole",
        "iam:CreateUser",
        "iam:CreateAccessKey",
        "sts:AssumeRole",
        ] : contains(flatten([
          for statement in jsondecode(aws_iam_policy.codebuild_boundary.policy).Statement :
          try(flatten([statement.Action]), [])
          if statement.Effect == "Deny"
      ]), action)
    ])
    error_message = "The build permissions boundary does not DENY the privilege-escalation action set."
  }

  # A boundary is an UPPER BOUND, not a grant: effective permission is the
  # identity policy INTERSECTED with the boundary. A Deny-only boundary permits
  # nothing and breaks every build on the next apply — which is exactly the
  # pressure that gets a broad grant reattached as a hotfix. The ceiling
  # statement is therefore load-bearing and asserted, not incidental.
  assert {
    condition = length([
      for statement in jsondecode(aws_iam_policy.codebuild_boundary.policy).Statement :
      statement
      if statement.Effect == "Allow" && statement.Sid == "BoundaryCeiling"
    ]) == 1
    error_message = "The build boundary has no Allow ceiling, so it would permit nothing and every build would fail."
  }

  assert {
    condition = alltrue(concat(
      [for name, role in aws_iam_role.project :
        role.permissions_boundary == aws_iam_policy.codebuild_boundary.arn
      ],
      [for key, role in aws_iam_role.agent_context_image :
        role.permissions_boundary == aws_iam_policy.codebuild_boundary.arn
      ],
    ))
    error_message = "A build role is not capped by the build permissions boundary."
  }
}

run "no_build_role_reaches_another_projects_resources" {
  command = apply

  # The shared role could read every secret and write every bucket. Scoping is
  # what makes one compromised buildspec stay one compromised project, so no
  # Allow may name a bare "*" resource except for the genuinely global,
  # unprivileged calls (ecr:GetAuthorizationToken, sts:GetCallerIdentity).
  assert {
    condition = alltrue(flatten([
      for name, policy in aws_iam_role_policy.project : [
        for statement in jsondecode(policy.policy).Statement :
        contains(["EcrAuth", "CallerIdentity"], statement.Sid)
        if statement.Effect == "Allow" && contains(flatten([statement.Resource]), "*")
      ]
    ]))
    error_message = "A per-project build policy grants a wildcard resource outside the two unprivileged global calls."
  }

  # Reading the buildspec zip is required; WRITING it is the supply-chain hole —
  # a build that can rewrite the source another build executes can run code as
  # that other build's identity.
  assert {
    condition = contains(flatten([
      for statement in jsondecode(aws_iam_policy.codebuild_boundary.policy).Statement :
      try(flatten([statement.Action]), [])
      if statement.Effect == "Deny" && statement.Sid == "DenyBuildInputTampering"
    ]), "s3:PutObject")
    error_message = "The boundary does not deny builds writing to the buildspec source prefix."
  }

  # The buildspec zips live in the Terraform STATE bucket. State holds every
  # resource attribute, including generated secrets, so a build must not read it.
  assert {
    condition = length([
      for statement in jsondecode(aws_iam_policy.codebuild_boundary.policy).Statement :
      statement
      if statement.Effect == "Deny" && statement.Sid == "DenyTerraformStateAccess"
    ]) == 1
    error_message = "The boundary does not deny builds reading Terraform state."
  }

  assert {
    condition = length([
      for statement in jsondecode(aws_iam_policy.codebuild_boundary.policy).Statement :
      statement
      if statement.Effect == "Deny" && statement.Sid == "DenySecretAndParameterReads"
    ]) == 1
    error_message = "The boundary does not deny builds reading secrets and parameters."
  }

  # Push only where the contract says. A project with no ecr_repos gets no push
  # permission at all, so the two scanners cannot publish an image.
  assert {
    condition = alltrue([
      for name, project in local.projects :
      !can(regex("OwnEcrRepositories", aws_iam_role_policy.project[name].policy))
      if length(lookup(project, "ecr_repos", [])) == 0
    ])
    error_message = "A project with no declared ECR repositories still has image-push permission."
  }

  assert {
    condition = length(flatten([
      for name, project in local.projects : lookup(project, "ecr_repos", [])
      ])) == length(distinct(flatten([
        for name, project in local.projects : lookup(project, "ecr_repos", [])
    ])))
    error_message = "Two CodeBuild projects can write the same ECR repository."
  }

  assert {
    condition = alltrue([
      for name, policy in aws_iam_role_policy.project :
      alltrue([
        for statement in jsondecode(policy.policy).Statement :
        toset(try(flatten([statement.Action]), [])) == toset(["ecr:CreateRepository"])
        if statement.Sid == "EcrRepositoryBootstrap"
      ])
    ])
    error_message = "An ECR bootstrap grant includes lifecycle or tagging mutation beyond exact repository creation."
  }

  assert {
    condition = alltrue([
      for key, policy in aws_iam_role_policy.agent_context_image :
      toset(flatten([
        for statement in jsondecode(policy.policy).Statement : flatten([statement.Resource])
        if statement.Sid == "OwnEcrRepository"
        ])) == toset([
        "arn:aws:ecr:us-east-1:123456789012:repository/adp-test-agent-context-${key}"
      ])
    ])
    error_message = "An agent-context build can write outside its one exact ECR repository."
  }
}

run "host_container_privilege_is_an_explicit_reviewed_allow_list" {
  command = apply

  # #5674 asked for privileged_mode to be dropped where images are not built.
  # Reading the buildspecs showed no such project exists here: all projects invoke a
  # container runtime, including the two Lambda-layer builds, which compile
  # dependencies inside the Lambda runtime image for binary compatibility.
  # Rather than silently keep a broad default, every use must now carry a
  # recorded reason — so adding a project cannot inherit privilege by accident.
  assert {
    condition = alltrue([
      for name, project in local.projects :
      trimspace(lookup(project, "privileged_why", "")) != ""
      if lookup(project, "privileged", false)
    ])
    error_message = "A project sets privileged without recording why it is required."
  }

  assert {
    condition = alltrue([
      for name, project in local.projects :
      !lookup(project, "privileged", false)
      if trimspace(lookup(project, "privileged_why", "")) == ""
    ])
    error_message = "A project records no privilege reason but still runs privileged."
  }

  assert {
    condition = alltrue([
      for name, project in aws_codebuild_project.main :
      project.environment[0].privileged_mode == local.projects[name].privileged
    ])
    error_message = "A CodeBuild project's privileged_mode does not match its declared contract."
  }
}

run "smoke_source_and_service_identity_are_project_bound" {
  command = plan
  assert {
    condition = alltrue([for name, role in aws_iam_role.project :
      jsondecode(role.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceArn"] == "arn:aws:codebuild:us-east-1:123456789012:project/adp-test-${name}"
    ])
    error_message = "A CodeBuild identity can be assumed by another project."
  }
  assert {
    condition = alltrue([for name, policy in aws_iam_role_policy.project :
      one([for s in jsondecode(policy.policy).Statement : s if s.Sid == "BuildSourceRead"]).Resource == "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-test-${name}/*"
    ])
    error_message = "A project can read another project's source archive."
  }
  assert {
    condition     = !can(regex("ecr:|kms:|secretsmanager:", aws_iam_role_policy.gateway_pr.policy))
    error_message = "The arbitrary-source PR role can publish or access credential/encryption services."
  }
  assert {
    condition     = !contains(keys(aws_codebuild_project.main), "gateway-smoke") && contains(keys(aws_codebuild_project.main), "gateway-build")
    error_message = "PR validation must reuse the existing gateway-build project."
  }
  assert {
    condition = jsondecode(aws_iam_role.gateway_pr.assume_role_policy).Statement[0].Condition.StringEquals == {
      "aws:SourceArn"     = "arn:aws:codebuild:us-east-1:123456789012:project/adp-test-gateway-build"
      "aws:SourceAccount" = "123456789012"
    }
    error_message = "Only the existing gateway project in this account may use the PR role."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.gateway_pr.policy).Statement : s if s.Sid == "PrSourceRead"]).Resource == "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-test-gateway-build-pr/*" && one([for s in jsondecode(aws_iam_role_policy.gateway_pr.policy).Statement : s if s.Sid == "DenyOtherSource"]).NotResource == "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-test-gateway-build-pr/*"
    error_message = "PR validation must read only PR archives, separate from trusted release input."
  }
  assert {
    condition     = aws_iam_role.gateway_pr.permissions_boundary == aws_iam_policy.codebuild_boundary.arn && !contains(one([for s in jsondecode(aws_iam_role_policy.gateway_pr.policy).Statement : s if s.Sid == "DenyOtherApis"]).NotAction, "ecr:PutImage")
    error_message = "PR validation must explicitly deny publication and retain the build boundary."
  }
}


run "executor_build_has_exact_generated_identity_and_resources" {
  command = apply
  assert {
    condition = (
      aws_codebuild_project.main["superplane-executor"].service_role == "arn:aws:iam::123456789012:role/adp-test-codebuild-superplane-executor" &&
      aws_iam_role.project["superplane-executor"].permissions_boundary == aws_iam_policy.codebuild_boundary.arn &&
      aws_codebuild_project.main["superplane-executor"].source[0].buildspec == "modules/domain-apps/superplane/releases/buildspecs/executor.yml" &&
      aws_codebuild_project.main["superplane-executor"].source[0].location == "adp-terraform-state-123456789012/codebuild/src/adp-test-superplane-executor/explicit-source-required.zip" &&
      aws_codebuild_project.main["superplane-executor"].logs_config[0].cloudwatch_logs[0].group_name == "/aws/codebuild/adp-test-superplane-executor" &&
      aws_codebuild_project.main["superplane-executor"].environment[0].privileged_mode
    )
    error_message = "Executor must use its bounded role, own source/logs, maintained buildspec and explicitly reviewed Docker privilege."
  }
  assert {
    condition = toset([for statement in jsondecode(aws_iam_role_policy.project["superplane-executor"].policy).Statement : statement.Sid]) == toset([
      "OwnBuildLogs", "BuildSourceRead", "EcrAuth", "OwnEcrRepositories", "EcrRepositoryBootstrap"
    ])
    error_message = "Executor build must not acquire scanner, shared-artifact or other optional grants."
  }
  assert {
    condition = alltrue([for statement in jsondecode(aws_iam_role_policy.project["superplane-executor"].policy).Statement :
      statement.Effect == "Allow" && toset(flatten([statement.Resource])) == toset(
        statement.Sid == "OwnBuildLogs" ? [
          "arn:aws:logs:us-east-1:123456789012:log-group:/aws/codebuild/adp-test-superplane-executor",
          "arn:aws:logs:us-east-1:123456789012:log-group:/aws/codebuild/adp-test-superplane-executor:*"
        ] : statement.Sid == "BuildSourceRead" ? ["arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-test-superplane-executor/*"] :
        statement.Sid == "EcrAuth" ? ["*"] : ["arn:aws:ecr:us-east-1:123456789012:repository/adp-superplane-executor"]
      )
    ])
    error_message = "Every executor statement must remain scoped to exact source/log/repository resources, including unlisted sibling repositories."
  }
}
