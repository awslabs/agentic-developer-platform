# Gateway releases need code publication and namespaced Kubernetes access, but
# not the infrastructure role's IAM, Terraform, or cluster-wide authority.
# Provision this profile only after the gateway and its stable secrets exist.
variable "enable_gateway_backend_deployment" {
  type    = bool
  default = false
}

data "aws_ssm_parameter" "gateway_backend_api_url" {
  count = var.enable_gateway_backend_deployment ? 1 : 0
  name  = "/adp/${var.environment}/gateway/apigw-invoke-url"
}

data "aws_secretsmanager_secret" "gateway_backend" {
  for_each = var.enable_gateway_backend_deployment ? toset([
    "token-secret-key", "internal-api-key", "magic-link-secret",
  ]) : toset([])
  name = "adp/${var.environment}/gateway/${each.key}"
}

data "aws_kms_key" "gateway_backend_secrets" {
  count  = var.enable_gateway_backend_deployment ? 1 : 0
  key_id = "alias/aws/secretsmanager"
}

data "aws_kms_key" "gateway_backend_lambda" {
  count  = var.enable_gateway_backend_deployment ? 1 : 0
  key_id = "alias/aws/lambda"
}

# The agent-authority SecureString parameters the gateway deploy renders into the
# ConfigMap (/adp/<env>/gateway/agent-authority-*, agent-task-source-*,
# run-report-signing-key) are encrypted with webhook-ingress's DynamoDB CMK
# (agent-authority-bootstrap.tf). Without decrypt on that key every ADP-managed
# gateway deploy silently fell back to the renderer's defaults — authority disabled,
# envelope key id "disabled" — and a ready Crew host could not be handed off (dev
# 2026-10-19, #8223). Decrypt is bounded to SSM in this region.
data "aws_kms_key" "gateway_backend_parameters" {
  count  = var.enable_gateway_backend_deployment ? 1 : 0
  key_id = "alias/adp-${var.environment}-webhook-dynamodb"
}

locals {
  gateway_backend_account = data.aws_caller_identity.current.account_id
  gateway_backend_cluster = "arn:aws:eks:${var.aws_region}:${local.gateway_backend_account}:cluster/${var.cluster_name}"
  gateway_backend_project = "arn:aws:codebuild:${var.aws_region}:${local.gateway_backend_account}:project/${var.name_prefix}-gateway-build"
  gateway_backend_repo    = "arn:aws:ecr:${var.aws_region}:${local.gateway_backend_account}:repository/adp-gateway"
  gateway_backend_lambdas = flatten([for name in [
    "bedrockgw-${var.environment}-pricing-refresh",
    "bedrockgw-${var.environment}-budget-usage-tracker",
    "adp-${var.environment}-orchestration-tick",
    ] : [
    "arn:aws:lambda:${var.aws_region}:${local.gateway_backend_account}:function:${name}",
    # UpdateFunctionCode authorizes the mutable code revision as $LATEST.
    "arn:aws:lambda:${var.aws_region}:${local.gateway_backend_account}:function:${name}:$LATEST",
  ]])
  gateway_backend_secret_arns = [for secret in data.aws_secretsmanager_secret.gateway_backend : secret.arn]
  gateway_backend_kms_key     = var.enable_gateway_backend_deployment ? data.aws_kms_key.gateway_backend_secrets[0].arn : ""
  gateway_backend_lambda_key  = var.enable_gateway_backend_deployment ? data.aws_kms_key.gateway_backend_lambda[0].arn : ""
  gateway_backend_kms_condition = { StringEquals = {
    "kms:ViaService"                  = "secretsmanager.${var.aws_region}.amazonaws.com"
    "kms:EncryptionContext:SecretARN" = local.gateway_backend_secret_arns
  } }
  gateway_backend_parameter_key       = var.enable_gateway_backend_deployment ? data.aws_kms_key.gateway_backend_parameters[0].arn : ""
  gateway_backend_parameter_condition = { StringEquals = { "kms:ViaService" = "ssm.${var.aws_region}.amazonaws.com" } }
  # Deployment and profile selection serialize on one target-specific item.
  # This grants no Terraform state-lock, table-management or tenant-data access.
  gateway_capacity_actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
  gateway_capacity_table   = "arn:aws:dynamodb:${var.aws_region}:${local.gateway_backend_account}:table/adp-terraform-locks"
  gateway_capacity_key     = "adp-gateway-capacity/${local.gateway_backend_account}/${var.aws_region}/${var.environment}/${var.cluster_name}/adp-gateway"
  gateway_capacity_grant = {
    Sid = "GatewayCapacityLock", Effect = "Allow", Action = local.gateway_capacity_actions, Resource = local.gateway_capacity_table,
    Condition = {
      "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = [local.gateway_capacity_key] }
      Null                        = { "dynamodb:LeadingKeys" = "false" }
    }
  }
  gateway_backend_api_id = var.enable_gateway_backend_deployment ? try(
    regex("^https://([a-z0-9]+)\\.execute-api\\.", nonsensitive(data.aws_ssm_parameter.gateway_backend_api_url[0].value))[0],
    "",
  ) : ""
  gateway_backend_api_arn = "arn:aws:apigateway:${var.aws_region}::/restapis/${local.gateway_backend_api_id}/*"
  # The shared release observes these existing components after publication.
  # Keep reads separate from deploy authority and bound them in the ceiling too.
  gateway_observer_api          = "arn:aws:execute-api:${var.aws_region}:${local.gateway_backend_account}:${local.gateway_backend_api_id}/${var.environment}/POST/internal/v1/runtime-proof/schema"
  gateway_observer_bucket       = "arn:aws:s3:::adp-${var.environment}-deployment-evidence-${local.gateway_backend_account}"
  gateway_observer_prefix       = "deployment-evidence/v1/"
  gateway_observer_distribution = "arn:aws:cloudfront::${local.gateway_backend_account}:distribution/${local.frontend_cloudfront_id}"
  gateway_observer_actions      = ["execute-api:Invoke", "s3:GetObject", "s3:GetObjectVersion", "s3:ListBucket", "cloudfront:GetDistribution"]
  gateway_observer_resources    = [local.gateway_observer_api, local.gateway_observer_bucket, "${local.gateway_observer_bucket}/${local.gateway_observer_prefix}*", local.gateway_observer_distribution]
  gateway_backend_ssm_read = [
    "arn:aws:ssm:${var.aws_region}:${local.gateway_backend_account}:parameter/adp/${var.environment}/gateway/*",
    "arn:aws:ssm:${var.aws_region}:${local.gateway_backend_account}:parameter/adp/${var.environment}/agent-context/ingestion-queue-url",
    "arn:aws:ssm:${var.aws_region}:${local.gateway_backend_account}:parameter/adp/${var.environment}/agent-gateway/*",
    "arn:aws:ssm:${var.aws_region}:${local.gateway_backend_account}:parameter/adp/${var.environment}/webhook-ingress/*",
    # Dev Box (#7998, optional module): the ConfigMap render reads the module's
    # published values; without this grant every ADP-managed gateway deploy
    # rendered BG_DEV_BOX_* empty and disabled admission on dev (2026-10-17).
    "arn:aws:ssm:${var.aws_region}:${local.gateway_backend_account}:parameter/adp/${var.environment}/dev-box/*",
    "arn:aws:ssm:${var.aws_region}:${local.gateway_backend_account}:parameter/adp/${var.environment}/gitlab/oidc-client-id",
    # The tools release publishes the façade/gateway roles and assertion key here;
    # the gateway configmap renders them into ADP_TOOLS_* so the gateway can
    # verify the façade identity on /internal/v1/tools/authorize.
    "arn:aws:ssm:${var.aws_region}:${local.gateway_backend_account}:parameter/adp/${var.environment}/tools/*",
    # The clean-room evaluations run as this identity and bind themselves to the
    # dev account before touching anything.
    "arn:aws:ssm:${var.aws_region}:${local.gateway_backend_account}:parameter/adp/${var.environment}/platform/account-id",
  ]
  gateway_backend_ssm_write = [for name in [
    "internal-alb-arn", "internal-alb-dns", "internal-alb-security-group-ids",
    "internal-plane-alb-arn", "internal-plane-alb-dns", "internal-plane-alb-security-group-ids",
  ] : "arn:aws:ssm:${var.aws_region}:${local.gateway_backend_account}:parameter/adp/${var.environment}/gateway/${name}"]
  gateway_backend_cfn_objects = [for name in [
    "aws_role_v1", "aws_role_v2", "aws_role_deploy_v1",
  ] : "arn:aws:s3:::${local.frontend_bucket}/cfn-templates/${name}.yaml"]
  gateway_backend_queues = [for suffix in [
    "pricing-delivery-failure", "pricing-execution-failure", "pricing-alarm-inbox",
  ] : "arn:aws:sqs:${var.aws_region}:${local.gateway_backend_account}:bedrockgw-${var.environment}-${suffix}"]
  gateway_backend_pricing_rule  = "arn:aws:events:${var.aws_region}:${local.gateway_backend_account}:rule/bedrockgw-${var.environment}-pricing-refresh-schedule"
  gateway_backend_pricing_topic = "arn:aws:sns:${var.aws_region}:${local.gateway_backend_account}:bedrockgw-${var.environment}-pricing-alarms"
  gateway_backend_actions = [
    "sts:GetCallerIdentity", "eks:DescribeCluster", "iam:GetRole",
    "ssm:GetParameter", "ssm:PutParameter", "secretsmanager:GetSecretValue",
    "s3:PutObject", "codebuild:StartBuild", "codebuild:BatchGetBuilds",
    "ecr:DescribeImages", "lambda:GetFunction", "lambda:GetFunctionConfiguration",
    "lambda:GetFunctionEventInvokeConfig", "lambda:UpdateFunctionCode", "lambda:InvokeFunction",
    "events:DescribeRule", "events:ListTargetsByRule", "events:DisableRule", "events:EnableRule",
    "sqs:GetQueueUrl", "sqs:GetQueueAttributes", "sns:ListSubscriptionsByTopic",
    "cloudwatch:DescribeAlarms", "cloudfront:GetDistributionConfig",
    "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTags",
    "apigateway:GET",
    # Direct token counting is the evaluation's independent reference for the
    # gateway's count_tokens answer; it reads nothing and writes nothing.
    "bedrock:CountTokens",
  ]
}

resource "aws_iam_policy" "gateway_backend_ceiling" {
  count       = var.enable_gateway_backend_deployment ? 1 : 0
  name        = "${var.name_prefix}-gateway-backend-ceiling"
  description = "Finite API ceiling for the gateway backend publisher"
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    # DenyOtherAPIs provides the finite non-KMS action ceiling. Avoid repeating
    # its action list here so the managed boundary stays below IAM's 6144 bytes.
    # KMS is excluded here and still needs the conditioned grants below.
    { Sid = "AllowDeclaredAPIs", Effect = "Allow", NotAction = "kms:*", Resource = "*" },
    { Sid = "DenyOtherAPIs", Effect = "Deny", NotAction = concat(local.gateway_backend_actions, local.gateway_capacity_actions, local.gateway_observer_actions, ["kms:Decrypt", "kms:DescribeKey"]), Resource = "*" },
    { Sid = "DenyOtherObservations", Effect = "Deny", Action = local.gateway_observer_actions, NotResource = concat(local.gateway_observer_resources, local.gateway_l5_release_objects) },
    { Sid       = "DenyOtherEvidencePrefixes", Effect = "Deny", Action = "s3:ListBucket", Resource = local.gateway_observer_bucket,
      Condition = { StringNotLike = { "s3:prefix" = "${local.gateway_observer_prefix}*" } },
    },
    # Explicit denies keep attached/resource policies from admitting other keys.
    { Sid = "DenyOtherCapacityTables", Effect = "Deny", Action = "dynamodb:*", NotResource = local.gateway_capacity_table },
    { Sid       = "DenyOtherCapacityKeys", Effect = "Deny", Action = "dynamodb:*", Resource = "*",
      Condition = { "ForAnyValue:StringNotEquals" = { "dynamodb:LeadingKeys" = [local.gateway_capacity_key] } },
    },
    { Sid       = "DenyMissingCapacityKey", Effect = "Deny", Action = "dynamodb:*", Resource = "*",
      Condition = { Null = { "dynamodb:LeadingKeys" = "true" } },
    },
    # The AWS-managed Secrets Manager key is shared across the account. Limit
    # decrypt to this key, this service, and the three stable gateway secrets.
    { Sid = "AllowGatewaySecretDecrypt", Effect = "Allow", Action = "kms:Decrypt", Resource = local.gateway_backend_kms_key, Condition = local.gateway_backend_kms_condition },
    { Sid = "AllowGatewayParameterDecrypt", Effect = "Allow", Action = "kms:Decrypt", Resource = local.gateway_backend_parameter_key, Condition = local.gateway_backend_parameter_condition },
    { Sid = "AllowLambdaKeyDescription", Effect = "Allow", Action = "kms:DescribeKey", Resource = local.gateway_backend_lambda_key },
    { Sid = "DenyOtherSecrets", Effect = "Deny", Action = "secretsmanager:GetSecretValue", NotResource = local.gateway_backend_secret_arns },
    { Sid = "DenyOtherExecutables", Effect = "Deny", Action = ["lambda:UpdateFunctionCode", "lambda:InvokeFunction"], NotResource = local.gateway_backend_lambdas },
    # Both APIs evaluate against the same project, even when polling a build ID.
    # One equivalent statement leaves room for the opt-in L5 object scope.
    { Sid = "DenyOtherBuilds", Effect = "Deny", Action = ["codebuild:StartBuild", "codebuild:BatchGetBuilds"], NotResource = local.gateway_backend_project },
    { Sid = "DenyOtherClusters", Effect = "Deny", Action = "eks:DescribeCluster", NotResource = local.gateway_backend_cluster },
    { Sid = "DenyOtherParameterWrites", Effect = "Deny", Action = "ssm:PutParameter", NotResource = local.gateway_backend_ssm_write },
    { Sid = "DenyOtherSchedules", Effect = "Deny", Action = ["events:DisableRule", "events:EnableRule"], NotResource = local.gateway_backend_pricing_rule },
    { Sid = "DenyOtherUploads", Effect = "Deny", Action = "s3:PutObject", NotResource = concat(
      ["arn:aws:s3:::adp-terraform-state-${local.gateway_backend_account}/codebuild/src/${var.name_prefix}-gateway-build/*"],
      ["arn:aws:s3:::adp-${var.environment}-deployment-evidence-${local.gateway_backend_account}/deployment-evidence/v1/*"],
      local.gateway_backend_cfn_objects,
      local.gateway_l5_release_objects,
    ) },
  ] })
}

resource "aws_iam_role" "gateway_backend" {
  count                = var.enable_gateway_backend_deployment ? 1 : 0
  name                 = "${var.name_prefix}-gateway-trusted-deployment"
  max_session_duration = 3600
  permissions_boundary = aws_iam_policy.gateway_backend_ceiling[0].arn
  lifecycle {
    # The opt-in variable must be retained on every later operator plan.
    # Forgetting it must fail the plan instead of silently deleting CI access.
    prevent_destroy = true
  }
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:${var.repository}:environment:adp-gateway-deploy-${var.environment}",
    } },
  }] })
}

resource "aws_iam_role_policy" "gateway_backend" {
  count = var.enable_gateway_backend_deployment ? 1 : 0
  name  = "gateway-backend-release"
  role  = aws_iam_role.gateway_backend[0].id
  lifecycle {
    precondition {
      condition     = local.gateway_backend_api_id != ""
      error_message = "Gateway API invoke URL must resolve to one deployed REST API ID."
    }
  }
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "sts:GetCallerIdentity", Resource = "*" },
    local.gateway_capacity_grant,
    { Effect = "Allow", Action = "execute-api:Invoke", Resource = local.gateway_observer_api },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "${local.gateway_observer_bucket}/${local.gateway_observer_prefix}*" },
    { Effect    = "Allow", Action = "s3:ListBucket", Resource = local.gateway_observer_bucket,
      Condition = { StringLike = { "s3:prefix" = "${local.gateway_observer_prefix}*" } },
    },
    { Effect = "Allow", Action = "cloudfront:GetDistribution", Resource = local.gateway_observer_distribution },
    # The clean-room evaluation's independent reference for the gateway's
    # count_tokens answer; reads nothing and writes nothing.
    { Effect = "Allow", Action = "bedrock:CountTokens", Resource = "*" },
    { Effect = "Allow", Action = "eks:DescribeCluster", Resource = local.gateway_backend_cluster },
    { Effect = "Allow", Action = "iam:GetRole", Resource = "arn:aws:iam::${local.gateway_backend_account}:role/adp-${var.environment}-role-gateway-service" },
    { Effect = "Allow", Action = "ssm:GetParameter", Resource = local.gateway_backend_ssm_read },
    { Effect = "Allow", Action = "ssm:PutParameter", Resource = local.gateway_backend_ssm_write },
    { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = local.gateway_backend_secret_arns },
    { Effect = "Allow", Action = "kms:Decrypt", Resource = local.gateway_backend_kms_key, Condition = local.gateway_backend_kms_condition },
    { Effect = "Allow", Action = "kms:Decrypt", Resource = local.gateway_backend_parameter_key, Condition = local.gateway_backend_parameter_condition },
    { Effect = "Allow", Action = "kms:DescribeKey", Resource = local.gateway_backend_lambda_key },
    { Effect = "Allow", Action = "s3:PutObject", Resource = concat(
      ["arn:aws:s3:::adp-terraform-state-${local.gateway_backend_account}/codebuild/src/${var.name_prefix}-gateway-build/*"],
      local.gateway_backend_cfn_objects,
    ) },
    { Effect = "Allow", Action = "codebuild:StartBuild", Resource = local.gateway_backend_project },
    { Effect = "Allow", Action = "codebuild:BatchGetBuilds", Resource = local.gateway_backend_project },
    { Effect = "Deny", Action = "codebuild:StartBuild", Resource = local.gateway_backend_project,
      Condition = { Null = { "codebuild:serviceRole" = "false" }, ArnNotEquals = {
        "codebuild:serviceRole" = "arn:aws:iam::${local.gateway_backend_account}:role/${var.name_prefix}-codebuild-gateway-build",
      } },
    },
    { Effect = "Allow", Action = "ecr:DescribeImages", Resource = local.gateway_backend_repo },
    { Effect = "Allow", Action = ["lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:GetFunctionEventInvokeConfig", "lambda:UpdateFunctionCode", "lambda:InvokeFunction"], Resource = local.gateway_backend_lambdas },
    { Effect = "Allow", Action = ["events:DescribeRule", "events:ListTargetsByRule", "events:DisableRule", "events:EnableRule"], Resource = local.gateway_backend_pricing_rule },
    { Effect = "Allow", Action = ["sqs:GetQueueUrl", "sqs:GetQueueAttributes"], Resource = local.gateway_backend_queues },
    { Effect = "Allow", Action = "sns:ListSubscriptionsByTopic", Resource = local.gateway_backend_pricing_topic },
    { Effect = "Allow", Action = ["cloudwatch:DescribeAlarms", "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeTags"], Resource = "*" },
    { Effect = "Allow", Action = "cloudfront:GetDistributionConfig", Resource = "arn:aws:cloudfront::${local.gateway_backend_account}:distribution/${local.frontend_cloudfront_id}" },
    { Effect = "Allow", Action = "apigateway:GET", Resource = local.gateway_backend_api_arn },
  ] })
}

resource "aws_eks_access_entry" "gateway_backend" {
  count         = var.enable_gateway_backend_deployment ? 1 : 0
  cluster_name  = var.cluster_name
  principal_arn = aws_iam_role.gateway_backend[0].arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "gateway_backend" {
  count         = var.enable_gateway_backend_deployment ? 1 : 0
  cluster_name  = var.cluster_name
  principal_arn = aws_iam_role.gateway_backend[0].arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSAdminPolicy"
  access_scope {
    type       = "namespace"
    namespaces = ["adp-gateway"]
  }
  depends_on = [aws_eks_access_entry.gateway_backend]
}

# The eval workflows (.github/workflows/eval-*.yml) run as this same deployment
# identity and spawn throwaway clean-room pods in `adp-gateway-evals` (namespace
# manifest: modules/gateway/k8s/eval-namespace.yaml; intended pod-only Role:
# modules/agent-factory/infra/runner-rbac.tf, bound to Group
# `adp:trusted-deployment`). This entry carries no Kubernetes groups, so that
# Role grants it nothing, and the Admin policy above is scoped to `adp-gateway`
# only. Grant namespace-scoped Edit in the evals namespace instead of adding the
# group: the group is also bound in adp-gateway, adp-agents and agent-context,
# which this identity must not gain. Edit covers the pod lifecycle, exec and log
# verbs the harnesses use and nothing cluster-wide. Observed 2026-10-11 on flow
# claude-desktop-6850-v2 (#7349 prerequisites) when the live qualification
# could not reach the namespace.
resource "aws_eks_access_policy_association" "gateway_backend_evals" {
  count         = var.enable_gateway_backend_deployment ? 1 : 0
  cluster_name  = var.cluster_name
  principal_arn = aws_iam_role.gateway_backend[0].arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSEditPolicy"
  access_scope {
    type       = "namespace"
    namespaces = ["adp-gateway-evals"]
  }
  depends_on = [aws_eks_access_entry.gateway_backend]
}

output "gateway_backend_deployment_role_arn" {
  value = var.enable_gateway_backend_deployment ? aws_iam_role.gateway_backend[0].arn : null
}
