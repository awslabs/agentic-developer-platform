mock_provider "aws" {
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::000000000000:policy/adp-dev-l5-worker-ceiling" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
}

variables {
  environment                 = "dev"
  aws_region                  = "us-east-1"
  aws_account_id              = "000000000000"
  github_repository           = "example/platform"
  github_environment          = "platform-deploy-dev"
  state_bucket                = "example-state"
  artifact_bucket             = "example-artifacts"
  agent_registry_table_name   = "example-registry"
  gateway_role_name           = "example-gateway"
  gateway_api_arn             = "arn:aws:execute-api:us-east-1:000000000000:example/*/*/internal/l5/*"
  rds_connect_arn             = "arn:aws:rds-db:us-east-1:000000000000:dbuser:db-example/l5_worker"
  vpc_id                      = "vpc-example"
  platform_security_group_ids = ["sg-database", "sg-endpoints"]
}

run "bounded_release_authority" {
  command = apply

  assert {
    condition = one([for grant in jsondecode(aws_iam_policy.grants["services"].policy).Statement : grant if grant.Sid == "Registry"]) == {
      Sid      = "Registry", Effect = "Allow",
      Action   = ["dynamodb:DescribeTable", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"],
      Resource = ["arn:aws:dynamodb:us-east-1:000000000000:table/example-registry"],
      Condition = { "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = [
        "l5-worker", "l5-devops-worker", "l5-transform-worker", "l5-well-architected-worker", "l5-finops-worker",
        "l5-repair", "l5-devops-repair", "l5-transform-repair", "l5-well-architected-repair", "l5-finops-repair",
        "adp-dev-l5-devops-native", "adp-dev-l5-devops-readback"
      ] } }
    }
    error_message = "Release registration must admit exactly the five worker and repair identities and the two DevOps event identities in the selected table, without wildcard keys."
  }

  assert {
    condition = contains(local.api_actions, "dynamodb:UpdateItem") && anytrue([
      for statement in jsondecode(aws_iam_policy.deployment.policy).Statement :
      statement.Effect == "Allow" && contains(statement.Action, "dynamodb:UpdateItem")
      ]) && alltrue([
      for statement in local.worker_grants : !contains(statement.Action, "dynamodb:UpdateItem")
    ])
    error_message = "Terraform must be able to update retained registry rows through the deployment boundary without granting registry writes to workers."
  }

  assert {
    condition = one([for g in local.deployment_grants : g if g.Sid == "DescribeQueueParameters"]) == {
      Sid       = "DescribeQueueParameters", Effect = "Allow", Action = ["ssm:DescribeParameters"], Resource = ["*"],
      Condition = { StringEquals = { "aws:RequestedRegion" = "us-east-1" } }
    }
    error_message = "Terraform must read parameter metadata in the selected region without granting unrelated parameter values."
  }

  assert {
    condition = (one([for g in local.deployment_grants : g.Resource if g.Sid == "CreateGroupsInSelectedVpc"]) == [local.vpc] &&
      !can(one([for g in local.deployment_grants : g if g.Sid == "CreateGroupsInSelectedVpc"]).Condition) &&
      one([for g in local.deployment_grants : g.Resource if g.Sid == "CreateServiceGroups"]) == ["arn:aws:ec2:us-east-1:000000000000:security-group/*"] &&
    one([for g in local.deployment_grants : g.Condition.StringEquals["aws:RequestTag/adp:module"] if g.Sid == "CreateServiceGroups"]) == "L5-services")
    error_message = "Security group creation must separately authorize the existing VPC and require tags on each new group."
  }

  assert {
    condition = one([for g in local.deployment_grants : g if g.Sid == "ServiceGroupEgress"]) == {
      Sid      = "ServiceGroupEgress", Effect = "Allow", Action = ["ec2:AuthorizeSecurityGroupEgress"],
      Resource = ["arn:aws:ec2:us-east-1:000000000000:security-group/*"],
      Condition = {
        ArnEquals    = { "ec2:Vpc" = "arn:aws:ec2:us-east-1:000000000000:vpc/vpc-example" }
        StringEquals = { "ec2:ResourceTag/adp:module" = "L5-services", "ec2:ResourceTag/adp:environment" = "dev" }
      }
      } && one([for g in local.deployment_grants : g if g.Sid == "CreateServiceEgressRules"]) == {
      Sid       = "CreateServiceEgressRules", Effect = "Allow", Action = ["ec2:AuthorizeSecurityGroupEgress"],
      Resource  = ["arn:aws:ec2:us-east-1:000000000000:security-group-rule/*"],
      Condition = { StringEquals = { "aws:RequestTag/adp:module" = "L5-services", "aws:RequestTag/adp:environment" = "dev" } }
      } && alltrue([
        for g in local.deployment_grants : !contains(g.Action, "ec2:AuthorizeSecurityGroupEgress") || contains(["ServiceGroupEgress", "CreateServiceEgressRules"], g.Sid)
    ])
    error_message = "Tagged egress creation must authorize both resources, requiring the selected VPC/environment on the existing group and matching request tags on the new rule, with no broader egress grant."
  }

  assert {
    condition = alltrue([
      for policy in concat([aws_iam_policy.deployment.policy, aws_iam_policy.worker_boundary.policy], [for p in aws_iam_policy.grants : p.policy]) : length(policy) <= 6144
    ])
    error_message = "Every managed IAM policy must fit its actual AWS document limit."
  }

  assert {
    condition = jsondecode(aws_iam_role.deployment.assume_role_policy).Statement[0].Condition.StringEquals == {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub" = "repo:example/platform:environment:platform-deploy-dev"
    }
    error_message = "Only the selected repository and protected environment may assume deployment authority."
  }

  assert {
    condition = alltrue([
      for grant in local.deployment_grants :
      !contains(grant.Action, "iam:AttachRolePolicy") &&
      !contains(grant.Action, "iam:CreatePolicyVersion") &&
      !contains(grant.Action, "iam:DeleteRolePermissionsBoundary")
    ]) && !contains(local.roles, "arn:aws:iam::000000000000:role/adp-dev-l5-trusted-deployment")
    error_message = "A release must not expand its own authority or the Gateway's transport policy."
  }

  assert {
    condition = alltrue([
      for grant in local.deployment_grants :
      !contains(grant.Action, "lambda:AddPermission") || try(grant.Condition.StringEquals["lambda:Principal"] == "events.amazonaws.com", false)
    ]) && !contains(local.api_actions, "lambda:CreateFunctionUrlConfig")
    error_message = "Deployment may authorize EventBridge invocation, never public Lambda access."
  }

  assert {
    condition     = one([for s in local.deployment_grants : s.Condition.ArnEquals["iam:PermissionsBoundary"] if s.Sid == "CreateBoundedWorkers"]) == aws_iam_policy.worker_boundary.arn
    error_message = "New workers must retain the operator-owned boundary."
  }

  assert {
    condition = alltrue([
      for resource in one([for s in local.deployment_grants : s.Resource if s.Sid == "ServiceState"]) :
      !strcontains(resource, "/deployment/") && startswith(resource, "arn:aws:s3:::example-state/l5-services/dev/us-east-1/")
    ]) && length(one([for s in local.deployment_grants : s.Resource if s.Sid == "ServiceState"])) == 8
    error_message = "Service release must not read or write the authority-bootstrap state."
  }

  assert {
    condition = alltrue([
      for s in local.worker_grants : s.Effect != "Allow" || alltrue([
        for action in s.Action : !startswith(action, "iam:") && !startswith(action, "secretsmanager:") && !startswith(action, "sts:")
      ])
    ]) && one([for s in local.worker_grants : s.Resource if contains(s.Action, "rds-db:connect")]) == [var.rds_connect_arn]
    error_message = "Workers use one database principal and delegated provider authority, with no ambient credential-store access."
  }

  assert {
    condition     = length(flatten(values(local.grant_groups))) == length(local.deployment_grants)
    error_message = "Every required release action must be represented exactly once in the split IAM documents."
  }

  assert {
    condition = one([for s in local.worker_grants : s.Resource if contains(s.Action, "lambda:InvokeFunction")]) == [
      "arn:aws:lambda:us-east-1:000000000000:function:adp-dev-l5-finops-executor"
    ] && contains(local.lambdas, "arn:aws:lambda:us-east-1:000000000000:function:adp-dev-l5-finops-executor")
    error_message = "Worker invocation and deployment must name only the installed private FinOps executor."
  }

  assert {
    condition = alltrue([
      for g in local.grant_groups.images :
      g.Sid == "ExecutorRegistryLogin" ? g.Action == ["ecr:GetAuthorizationToken"] : g.Resource == local.executor_repositories
    ]) && !contains(local.api_actions, "ecr:DeleteRepository") && !contains(local.roles, "arn:aws:iam::000000000000:role/adp-dev-l5-finops-executor")
    error_message = "Image publication must be scoped to the service repository; the executor shares its bounded worker role."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_policy.deployment.policy).Statement :
      !anytrue([for a in s.Action : contains(local.ecr_actions, a)]) || try(s.Resource == local.executor_repositories, false)
      ]) && alltrue([
      for s in local.worker_grants : alltrue([for a in s.Action : !startswith(a, "ecr:")])
    ])
    error_message = "The boundary must deny other repositories, and application workers must not publish images."
  }

  assert {
    condition     = !contains(local.api_actions, "eks:DescribeCluster") && !contains(local.api_actions, "eks:CreateAccessEntry")
    error_message = "Default deployment has no cluster discovery or cluster authorization-management authority."
  }

  assert {
    condition     = length(local.transform_artifact_grants) == 0
    error_message = "Unselected Transform infrastructure must not grant access to an executor artifact bucket."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_policy.deployment.policy).Statement :
      !anytrue([for a in s.Action : contains(concat(local.iam_actions, local.lambda_calls), a)]) || s.Resource != "*"
      ]) && alltrue([
      for s in jsondecode(aws_iam_policy.deployment.policy).Statement :
      !contains(s.Action, "iam:PassRole") || try(s.Condition.StringEquals["iam:PassedToService"] == "lambda.amazonaws.com", false)
    ])
    error_message = "Compact boundaries must preserve scoped IAM/Lambda access and the Lambda-only role handoff."
  }

  assert {
    condition = alltrue([
      for s in local.deployment_grants :
      !contains(s.Action, "lambda:PutFunctionRecursionConfig") || try(toset(s.Resource) == toset(local.polling_workers), false)
    ]) && length(local.polling_workers) == 5
    error_message = "Intentional recursion controls must be limited to the five durable queue workers."
  }

  assert {
    condition = contains(one([
      for s in local.worker_grants : s.Condition.ArnEquals["lambda:SourceFunctionArn"] if s.Effect == "Deny"
    ]), "arn:aws:lambda:us-east-1:000000000000:function:adp-dev-l5-finops-executor")
    error_message = "The browser executor must retain the workload ENI denial when sharing the worker role."
  }
}

run "selected_transform_cluster_discovery" {
  command = apply
  variables {
    transform_cluster_name = "example-private-executor"
  }

  assert {
    condition = one([for g in local.worker_grants : g.Resource if contains(g.Action, "eks:DescribeCluster")]) == [
      "arn:aws:eks:us-east-1:000000000000:cluster/example-private-executor"
      ] && one([for g in local.deployment_grants : g.Resource if g.Sid == "TransformClusterDiscovery"]) == [
      "arn:aws:eks:us-east-1:000000000000:cluster/example-private-executor"
    ]
    error_message = "Cluster discovery is restricted to the selected account, region and cluster."
  }

  assert {
    condition     = !anytrue([for action in local.api_actions : startswith(action, "eks:") && action != "eks:DescribeCluster"])
    error_message = "Discovery cannot create access entries, grant cluster policies or modify the cluster."
  }

  assert {
    condition = alltrue([
      for grant in local.transform_artifact_grants :
      grant.Condition.ArnEquals["aws:PrincipalArn"] == "arn:aws:iam::000000000000:role/adp-dev-l5-transform-worker"
      ]) && one([for grant in local.transform_artifact_grants : grant.Resource if contains(grant.Action, "s3:GetObjectVersion")]) == [
      "arn:aws:s3:::adp-dev-l5-transform-000000000000/l5/transform/artifacts/*"
      ] && one([for grant in local.transform_artifact_grants : grant.Resource if contains(grant.Action, "s3:GetObject")]) == [
      "arn:aws:s3:::adp-dev-l5-transform-000000000000/l5/transform/artifacts/*/checkpoints/*"
    ] && one([for grant in local.transform_artifact_grants : grant.Condition.StringLike["s3:prefix"] if contains(grant.Action, "s3:ListBucket")]) == "l5/transform/artifacts/*/checkpoints/*"
    error_message = "Only the selected Transform worker may read versioned artifacts and discover its retained checkpoints."
  }

  assert {
    condition = alltrue([
      for grant in local.worker_grants : alltrue([for resource in grant.Resource : endswith(resource, ":*")])
      if contains(grant.Action, "logs:PutLogEvents")
    ])
    error_message = "Worker log writes need the selected function stream paths only, keeping the ceiling within IAM's size limit."
  }

  assert {
    condition = alltrue([
      for policy in concat([aws_iam_policy.deployment.policy, aws_iam_policy.worker_boundary.policy], [for p in aws_iam_policy.grants : p.policy]) : length(policy) <= 6144
    ]) && length(flatten(values(local.grant_groups))) == length(local.deployment_grants)
    error_message = "Selected-cluster policies must remain complete and fit AWS policy size limits."
  }
}

run "encrypted_registry" {
  command = apply
  variables {
    agent_registry_kms_key_arn = "arn:aws:kms:us-east-1:000000000000:key/00000000-0000-0000-0000-000000000001"
    transform_cluster_name     = "example-cluster"
    gateway_role_name          = "example-gateway-with-a-long-role-name-to-check-policy-size"
  }
  assert {
    condition = alltrue([for policy in [aws_iam_policy.deployment.policy, aws_iam_policy.grants["storage"].policy] :
      one([for s in jsondecode(policy).Statement : s if contains(s.Action, "kms:Decrypt")]).Resource == [var.agent_registry_kms_key_arn]
      ]) && one([for s in jsondecode(aws_iam_policy.grants["storage"].policy).Statement : s if s.Sid == "RegistryKey"]).Condition.StringEquals == {
      "kms:ViaService"                               = "dynamodb.us-east-1.amazonaws.com",
      "kms:CallerAccount"                            = "000000000000",
      "kms:EncryptionContext:aws:dynamodb:tableName" = "example-registry"
    }
    error_message = "Registry decryption must match the selected key in both policies and the table, account and DynamoDB service in the identity policy."
  }
  assert {
    condition     = alltrue([for policy in concat([aws_iam_policy.deployment.policy], [for p in aws_iam_policy.grants : p.policy]) : length(policy) <= 6144])
    error_message = "Encrypted registry support must fit the IAM document limits (boundary: ${length(aws_iam_policy.deployment.policy)})."
  }
}
