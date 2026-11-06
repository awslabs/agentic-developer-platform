# Operator-owned bootstrap. The resulting deployment role cannot modify itself,
# its ceiling, or the worker ceiling. Normal service release never applies here.
data "aws_partition" "current" {}
locals {
  arn             = "arn:${data.aws_partition.current.partition}"
  regional        = "${var.aws_region}:${var.aws_account_id}"
  prefix          = "adp-${var.environment}-l5"
  services        = ["security", "devops", "transform", "well_architected", "finops"]
  prefixes        = [for s in local.services : "${local.prefix}${s == "security" ? "" : "-${replace(s, "_", "-")}"}"]
  role_names      = concat(flatten([for p in local.prefixes : ["${p}-worker", "${p}-repair"]]), ["${local.prefix}-devops-native", "${local.prefix}-devops-readback"])
  functions       = concat(local.role_names, ["${local.prefix}-finops-executor"])
  roles           = [for n in local.role_names : "${local.arn}:iam::${var.aws_account_id}:role/${n}"]
  lambdas         = [for n in local.functions : "${local.arn}:lambda:${local.regional}:function:${n}"]
  polling_workers = [for p in local.prefixes : "${local.arn}:lambda:${local.regional}:function:${p}-worker"]
  queues          = flatten([for p in local.prefixes : ["${local.arn}:sqs:${local.regional}:${p}-work", "${local.arn}:sqs:${local.regional}:${p}-work-dlq"]])
  parameters      = concat([for s in local.services : "${local.arn}:ssm:${local.regional}:parameter/adp/${var.environment}/l5-services/${s}/work-queue-url"], ["${local.arn}:ssm:${local.regional}:parameter/adp/${var.environment}/gateway/l5-work-queue-url"])
  logs            = flatten([for n in local.functions : ["${local.arn}:logs:${local.regional}:log-group:/aws/lambda/${n}", "${local.arn}:logs:${local.regional}:log-group:/aws/lambda/${n}:*"]])
  network         = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"]
  registry        = "${local.arn}:dynamodb:${local.regional}:table/${var.agent_registry_table_name}"
  registry_key_grants = var.agent_registry_kms_key_arn == "" ? [] : [
    { Sid = "RegistryKey", Effect = "Allow", Action = ["kms:Decrypt"], Resource = [var.agent_registry_kms_key_arn], Condition = { StringEquals = {
      "kms:ViaService"                               = "dynamodb.${var.aws_region}.amazonaws.com",
      "kms:CallerAccount"                            = var.aws_account_id,
      "kms:EncryptionContext:aws:dynamodb:tableName" = var.agent_registry_table_name
    } } }
  ]
  executor_repositories = [for service in ["finops", "transform"] : "${local.arn}:ecr:${local.regional}:repository/${local.prefix}-${service}-executor"]
  transform_cluster_grants = var.transform_cluster_name == "" ? [] : [
    { Effect = "Allow", Action = ["eks:DescribeCluster"], Resource = ["${local.arn}:eks:${local.regional}:cluster/${var.transform_cluster_name}"] }
  ]
  transform_artifact_bucket = "${local.arn}:s3:::${local.prefix}-transform-${var.aws_account_id}"
  transform_worker_condition = {
    ArnEquals = { "aws:PrincipalArn" = "${local.arn}:iam::${var.aws_account_id}:role/${local.prefix}-transform-worker" }
  }
  transform_artifact_grants = var.transform_cluster_name == "" ? [] : [
    { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObjectVersion"], Resource = ["${local.transform_artifact_bucket}/l5/transform/artifacts/*"], Condition = local.transform_worker_condition },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["${local.transform_artifact_bucket}/l5/transform/artifacts/*/checkpoints/*"], Condition = local.transform_worker_condition },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = [local.transform_artifact_bucket], Condition = merge(local.transform_worker_condition, { StringLike = { "s3:prefix" = "l5/transform/artifacts/*/checkpoints/*" } }) }
  ]
  transport = "${local.arn}:iam::${var.aws_account_id}:policy/${local.prefix}-transport"
  vpc       = "${local.arn}:ec2:${local.regional}:vpc/${var.vpc_id}"
  groups    = [for id in var.platform_security_group_ids : "${local.arn}:ec2:${local.regional}:security-group/${id}"]
  worker_grants = concat([
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = [for arn in local.logs : arn if endswith(arn, ":*")] },
    { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility", "sqs:SendMessage"], Resource = local.queues },
    { Effect = "Allow", Action = ["rds-db:connect"], Resource = [var.rds_connect_arn] },
    { Effect = "Allow", Action = ["execute-api:Invoke"], Resource = [var.gateway_api_arn] },
    { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = ["${local.arn}:lambda:${local.regional}:function:${local.prefix}-finops-executor"] },
    { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"], Resource = [for s in local.services : "${local.arn}:s3:::${var.artifact_bucket}/l5/${s}/work/*"] },
    { Effect = "Allow", Action = local.network, Resource = ["*"] },
    { Effect = "Deny", Action = local.network, Resource = ["*"], Condition = { ArnEquals = { "lambda:SourceFunctionArn" = local.lambdas } } }
  ], local.transform_cluster_grants, local.transform_artifact_grants)
}

resource "aws_iam_policy" "worker_boundary" {
  name   = "${local.prefix}-worker-ceiling"
  policy = jsonencode({ Version = "2012-10-17", Statement = local.worker_grants })
  lifecycle { prevent_destroy = true }
}

locals {
  deployment_grants = concat([
    { Sid = "Identity", Effect = "Allow", Action = ["sts:GetCallerIdentity"], Resource = ["*"] },
    { Sid = "ExecutorRegistryLogin", Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = ["*"] },
    { Sid = "ExecutorRepository", Effect = "Allow", Action = ["ecr:CreateRepository", "ecr:DescribeRepositories", "ecr:GetRepositoryPolicy", "ecr:SetRepositoryPolicy", "ecr:TagResource", "ecr:UntagResource", "ecr:ListTagsForResource"], Resource = local.executor_repositories },
    { Sid = "ExecutorImage", Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:DescribeImages", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], Resource = local.executor_repositories },
    { Sid = "CreateBoundedWorkers", Effect = "Allow", Action = ["iam:CreateRole", "iam:PutRolePermissionsBoundary"], Resource = local.roles,
    Condition = { ArnEquals = { "iam:PermissionsBoundary" = "${local.arn}:iam::${var.aws_account_id}:policy/${aws_iam_policy.worker_boundary.name}" } } },
    { Sid = "MaintainWorkers", Effect = "Allow", Action = ["iam:GetRole", "iam:DeleteRole", "iam:UpdateAssumeRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:GetRolePolicy", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:TagRole", "iam:UntagRole", "iam:ListRoleTags"], Resource = local.roles },
    { Sid = "PassWorkersToLambda", Effect = "Allow", Action = ["iam:PassRole"], Resource = local.roles, Condition = { StringEquals = { "iam:PassedToService" = "lambda.amazonaws.com" } } },
    { Sid = "TransportPolicy", Effect = "Allow", Action = ["iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListPolicyVersions", "iam:ListPolicyTags"], Resource = [local.transport] },
    { Sid = "ReadGatewayRole", Effect = "Allow", Action = ["iam:GetRole", "iam:ListAttachedRolePolicies", "iam:ListRolePolicies", "iam:GetRolePolicy"], Resource = ["${local.arn}:iam::${var.aws_account_id}:role/${var.gateway_role_name}"] },
    { Sid = "Functions", Effect = "Allow", Action = ["lambda:CreateFunction", "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration", "lambda:DeleteFunction", "lambda:TagResource", "lambda:UntagResource", "lambda:ListTags", "lambda:GetPolicy", "lambda:RemovePermission", "lambda:InvokeFunction", "lambda:ListVersionsByFunction", "lambda:GetFunctionCodeSigningConfig"], Resource = local.lambdas },
    { Sid = "BoundedContinuations", Effect = "Allow", Action = ["lambda:GetFunctionRecursionConfig", "lambda:PutFunctionRecursionConfig"], Resource = local.polling_workers },
    { Sid = "ScheduleInvocation", Effect = "Allow", Action = ["lambda:AddPermission"], Resource = local.lambdas, Condition = { StringEquals = { "lambda:Principal" = "events.amazonaws.com" } } },
    { Sid = "CreateConsumer", Effect = "Allow", Action = ["lambda:CreateEventSourceMapping"], Resource = ["*"], Condition = { ArnEquals = { "lambda:FunctionArn" = local.lambdas } } },
    { Sid = "UpdateConsumer", Effect = "Allow", Action = ["lambda:UpdateEventSourceMapping"], Resource = ["${local.arn}:lambda:${local.regional}:event-source-mapping:*"], Condition = { ArnEquals = { "lambda:FunctionArn" = local.lambdas }, StringEquals = { "aws:ResourceTag/adp:module" = "L5-services" } } },
    { Sid = "ReadConsumer", Effect = "Allow", Action = ["lambda:GetEventSourceMapping", "lambda:ListTags"], Resource = ["${local.arn}:lambda:${local.regional}:event-source-mapping:*"] },
    { Sid = "DeleteConsumer", Effect = "Allow", Action = ["lambda:DeleteEventSourceMapping", "lambda:UntagResource"], Resource = ["${local.arn}:lambda:${local.regional}:event-source-mapping:*"], Condition = { StringEquals = { "aws:ResourceTag/adp:module" = "L5-services" } } },
    { Sid = "TagConsumer", Effect = "Allow", Action = ["lambda:TagResource"], Resource = ["${local.arn}:lambda:${local.regional}:event-source-mapping:*"], Condition = { StringEquals = { "aws:RequestTag/adp:module" = "L5-services" } } },
    { Sid = "ReadConsumers", Effect = "Allow", Action = ["lambda:ListEventSourceMappings"], Resource = ["*"] },
    { Sid = "Queues", Effect = "Allow", Action = ["sqs:CreateQueue", "sqs:DeleteQueue", "sqs:GetQueueUrl", "sqs:GetQueueAttributes", "sqs:SetQueueAttributes", "sqs:ListQueueTags", "sqs:TagQueue", "sqs:UntagQueue"], Resource = local.queues },
    { Sid = "Schedules", Effect = "Allow", Action = ["events:PutRule", "events:DescribeRule", "events:DeleteRule", "events:PutTargets", "events:RemoveTargets", "events:ListTargetsByRule", "events:EnableRule", "events:DisableRule", "events:ListTagsForResource", "events:TagResource", "events:UntagResource"], Resource = ["${local.arn}:events:${local.regional}:rule/${local.prefix}*"] },
    { Sid = "Logs", Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:DeleteRetentionPolicy", "logs:ListTagsForResource", "logs:TagResource", "logs:UntagResource", "logs:GetLogEvents", "logs:FilterLogEvents"], Resource = local.logs },
    { Sid = "DescribeLogs", Effect = "Allow", Action = ["logs:DescribeLogGroups", "logs:DescribeLogStreams"], Resource = ["*"] },
    { Sid = "Alarms", Effect = "Allow", Action = ["cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms", "cloudwatch:DescribeAlarms", "cloudwatch:ListTagsForResource", "cloudwatch:TagResource", "cloudwatch:UntagResource"], Resource = ["${local.arn}:cloudwatch:${local.regional}:alarm:${local.prefix}*"] },
    { Sid = "Registry", Effect = "Allow", Action = ["dynamodb:DescribeTable", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"], Resource = [local.registry], Condition = { "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = concat(["l5-worker", "l5-devops-worker", "l5-transform-worker", "l5-well-architected-worker", "l5-finops-worker"], ["l5-repair", "l5-devops-repair", "l5-transform-repair", "l5-well-architected-repair", "l5-finops-repair"], ["${local.prefix}-devops-native", "${local.prefix}-devops-readback"]) } } },
    { Sid = "QueueDiscovery", Effect = "Allow", Action = ["ssm:GetParameter", "ssm:PutParameter", "ssm:DeleteParameter", "ssm:AddTagsToResource", "ssm:RemoveTagsFromResource", "ssm:ListTagsForResource"], Resource = local.parameters },
    # The provider refreshes parameter metadata through a list action that does
    # not support parameter-level resource permissions. Values remain scoped above.
    { Sid = "DescribeQueueParameters", Effect = "Allow", Action = ["ssm:DescribeParameters"], Resource = ["*"], Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } } },
    { Sid = "ReadNetworks", Effect = "Allow", Action = ["ec2:DescribeSecurityGroups", "ec2:DescribeSecurityGroupRules", "ec2:DescribeVpcs", "ec2:DescribeSubnets", "ec2:DescribeNetworkInterfaces"], Resource = ["*"] },
    # Request tags belong to the new security group, not its existing VPC.
    { Sid = "CreateGroupsInSelectedVpc", Effect = "Allow", Action = ["ec2:CreateSecurityGroup"], Resource = [local.vpc] },
    { Sid = "CreateServiceGroups", Effect = "Allow", Action = ["ec2:CreateSecurityGroup"], Resource = ["${local.arn}:ec2:${local.regional}:security-group/*"], Condition = { StringEquals = { "aws:RequestTag/adp:module" = "L5-services" } } },
    { Sid = "TagNewServiceGroups", Effect = "Allow", Action = ["ec2:CreateTags"], Resource = ["${local.arn}:ec2:${local.regional}:security-group/*"], Condition = { StringEquals = { "ec2:CreateAction" = "CreateSecurityGroup", "aws:RequestTag/adp:module" = "L5-services" } } },
    { Sid = "ServiceGroupRules", Effect = "Allow", Action = ["ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress", "ec2:RevokeSecurityGroupEgress", "ec2:DeleteSecurityGroup", "ec2:CreateTags", "ec2:DeleteTags"], Resource = ["${local.arn}:ec2:${local.regional}:security-group/*"], Condition = { StringEquals = { "ec2:ResourceTag/adp:module" = "L5-services" } } },
    # Tag-on-create authorizes both the existing parent group and the new rule.
    # The parent has resource tags/VPC context; only the rule has request tags.
    { Sid = "ServiceGroupEgress", Effect = "Allow", Action = ["ec2:AuthorizeSecurityGroupEgress"], Resource = ["${local.arn}:ec2:${local.regional}:security-group/*"], Condition = {
      ArnEquals    = { "ec2:Vpc" = local.vpc }
      StringEquals = { "ec2:ResourceTag/adp:module" = "L5-services", "ec2:ResourceTag/adp:environment" = var.environment }
    } },
    { Sid = "CreateServiceEgressRules", Effect = "Allow", Action = ["ec2:AuthorizeSecurityGroupEgress"], Resource = ["${local.arn}:ec2:${local.regional}:security-group-rule/*"], Condition = {
      StringEquals = { "aws:RequestTag/adp:module" = "L5-services", "aws:RequestTag/adp:environment" = var.environment }
    } },
    { Sid = "PlatformIngress", Effect = "Allow", Action = ["ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress"], Resource = local.groups },
    { Sid = "RuleMetadata", Effect = "Allow", Action = ["ec2:CreateTags", "ec2:DeleteTags"], Resource = ["${local.arn}:ec2:${local.regional}:security-group-rule/*"] },
    { Sid = "StateBucket", Effect = "Allow", Action = ["s3:ListBucket"], Resource = ["${local.arn}:s3:::${var.state_bucket}"], Condition = { StringLike = { "s3:prefix" = ["l5-services/${var.environment}/*", "env:/"] } } },
    { Sid = "ServiceState", Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = flatten([for s in concat(["foundation", "finops-artifacts", "transform-artifacts"], [for s in local.services : replace(s, "_", "-")]) : ["${local.arn}:s3:::${var.state_bucket}/l5-services/${var.environment}/${var.aws_region}/${s}/*"]]) },
    { Sid = "ArtifactBucket", Effect = "Allow", Action = ["s3:GetBucketVersioning", "s3:GetBucketLocation", "s3:ListBucket"], Resource = ["${local.arn}:s3:::${var.artifact_bucket}"] },
    { Sid = "ReleaseArtifacts", Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"], Resource = ["${local.arn}:s3:::${var.artifact_bucket}/l5/*"] }
  ], local.registry_key_grants, [for grant in local.transform_cluster_grants : merge(grant, { Sid = "TransformClusterDiscovery" })])
  # Each IAM document has a 6,144-character limit. Keep independently scoped
  # grants separate; only the operator can change these policies or the ceiling.
  grant_groups = {
    identity      = [for g in local.deployment_grants : g if startswith(g.Action[0], "iam:") || g.Sid == "Identity"]
    functions     = [for g in local.deployment_grants : g if startswith(g.Action[0], "lambda:") && g.Sid != "BoundedContinuations"]
    continuations = [for g in local.deployment_grants : g if g.Sid == "BoundedContinuations"]
    services      = [for g in local.deployment_grants : g if contains(["Queues", "Schedules", "Logs", "DescribeLogs", "Alarms", "Registry", "QueueDiscovery", "DescribeQueueParameters", "TransformClusterDiscovery"], g.Sid)]
    storage       = [for g in local.deployment_grants : g if startswith(g.Action[0], "s3:") || startswith(g.Action[0], "ec2:") || startswith(g.Action[0], "kms:")]
    images        = [for g in local.deployment_grants : g if startswith(g.Action[0], "ecr:")]
  }
  api_actions       = sort(distinct(flatten([for g in local.deployment_grants : g.Action])))
  iam_actions       = [for a in local.api_actions : a if startswith(a, "iam:")]
  ecr_actions       = [for a in local.api_actions : a if startswith(a, "ecr:") && a != "ecr:GetAuthorizationToken"]
  recursion_actions = ["lambda:GetFunctionRecursionConfig", "lambda:PutFunctionRecursionConfig"]
  # The ceiling must fit IAM's document limit. Identity grants still enumerate
  # the two configuration APIs and restrict them to queue workers only.
  lambda_calls = concat([for action in one([for g in local.deployment_grants : g.Action if g.Sid == "Functions"]) : action if !contains(["lambda:TagResource", "lambda:UntagResource", "lambda:ListTags"], action)], ["lambda:*Recursion*"])
}

resource "aws_iam_policy" "deployment" {
  name = "${local.prefix}-deployment-ceiling"
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = [for action in local.api_actions : action if !contains(concat(local.iam_actions, local.lambda_calls, local.recursion_actions, local.ecr_actions, ["kms:Decrypt"]), action)], Resource = "*" },
    { Effect = "Allow", Action = ["iam:PassRole"], Resource = local.roles, Condition = { StringEquals = { "iam:PassedToService" = "lambda.amazonaws.com" } } },
    { Effect = "Allow", Action = [for action in local.iam_actions : action if action != "iam:PassRole"], Resource = concat(local.roles, [local.transport, "${local.arn}:iam::${var.aws_account_id}:role/${var.gateway_role_name}"]) },
    { Effect = "Allow", Action = local.lambda_calls, Resource = local.lambdas },
    { Effect = "Allow", Action = local.ecr_actions, Resource = local.executor_repositories }
    ], [for grant in local.registry_key_grants : {
      # The identity policy pins the service, account and table context. The
      # boundary independently limits the key, within its 6,144-byte limit.
      Effect = grant.Effect, Action = grant.Action, Resource = grant.Resource
  }]) })
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_policy" "grants" {
  for_each = local.grant_groups
  name     = "${local.prefix}-deployment-${each.key}"
  policy   = jsonencode({ Version = "2012-10-17", Statement = each.value })
}

resource "aws_iam_role" "deployment" {
  name                 = "${local.prefix}-trusted-deployment"
  permissions_boundary = aws_iam_policy.deployment.arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = "${local.arn}:iam::${var.aws_account_id}:oidc-provider/token.actions.githubusercontent.com" }
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:${var.github_repository}:environment:${var.github_environment}"
      } }
    }]
  })
  lifecycle { prevent_destroy = true }
}
resource "aws_iam_role_policy_attachment" "deployment" {
  for_each   = aws_iam_policy.grants
  role       = aws_iam_role.deployment.name
  policy_arn = each.value.arn
}
output "deployment_role_arn" { value = aws_iam_role.deployment.arn }
output "worker_boundary_arn" { value = aws_iam_policy.worker_boundary.arn }
