terraform {
  required_version = ">= 1.7, < 2.0"
  required_providers {
    aws        = { source = "hashicorp/aws", version = "~> 6.0" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.0" }
  }
}
locals {
  active    = var.enabled ? 1 : 0
  route_arn = "${var.api_execution_arn}/${var.stage_name}/POST/tools/validation"
  group     = "adp-validation-service"
}
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}
data "aws_eks_cluster" "validation" {
  count = local.active
  name  = var.cluster_name
}
data "aws_subnet" "validation" {
  for_each = var.enabled ? toset(var.subnet_ids) : toset([])
  id       = each.value
}
# The service must reach the private EKS API as well as the gateway and AWS
# APIs. Reusing another tools Lambda's egress-only group does not admit EKS
# ingress, and attaching the cluster group would inherit unrelated privileges.
resource "aws_security_group" "service" {
  count       = local.active
  name        = "${var.name}-service"
  description = "Dedicated Task validation Lambda"
  vpc_id      = data.aws_eks_cluster.validation[0].vpc_config[0].vpc_id
}
resource "aws_vpc_security_group_egress_rule" "https" {
  count             = local.active
  security_group_id = aws_security_group.service[0].id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
  description       = "HTTPS to gateway, EKS and AWS APIs through private subnet routing"
}
resource "aws_vpc_security_group_ingress_rule" "eks_api" {
  count                        = local.active
  security_group_id            = data.aws_eks_cluster.validation[0].vpc_config[0].cluster_security_group_id
  referenced_security_group_id = aws_security_group.service[0].id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  description                  = "Task validation service to private EKS API only"
}
resource "aws_dynamodb_table" "jobs" {
  count                       = local.active
  name                        = "${var.name}-jobs"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "event_id"
  range_key                   = "arrived_at"
  deletion_protection_enabled = true
  attribute {
    name = "event_id"
    type = "S"
  }
  attribute {
    name = "arrived_at"
    type = "S"
  }
  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true }
}
resource "aws_cloudwatch_log_group" "service" {
  count             = local.active
  name              = "/aws/lambda/${var.name}"
  retention_in_days = 30
}
resource "aws_iam_role" "service" {
  count = local.active
  name  = "${var.name}-service"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" }
  }] })
}
resource "aws_iam_role_policy" "service" {
  count = local.active
  role  = aws_iam_role.service[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.service[0].arn}:*" },
    { Effect = "Allow", Action = ["execute-api:Invoke"], Resource = [for route in ["tool-authorize", "artifact", "repository-source"] : "${var.api_execution_arn}/${var.stage_name}/POST/internal/v1/agent/task/${route}"] },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.jobs[0].arn },
    { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = "arn:${data.aws_partition.current.partition}:lambda:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:function:${var.name}" },
    { Effect = "Allow", Action = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets", "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"], Resource = "*" }
  ] })
}
resource "aws_lambda_function" "service" {
  count                          = local.active
  function_name                  = var.name
  role                           = aws_iam_role.service[0].arn
  package_type                   = "Image"
  image_uri                      = var.image_uri
  architectures                  = ["x86_64"]
  timeout                        = 180
  memory_size                    = 2048
  reserved_concurrent_executions = 16
  ephemeral_storage { size = 2048 }
  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = [aws_security_group.service[0].id]
  }
  environment {
    variables = {
      ADP_VALIDATION_SERVICE_ENABLED  = tostring(var.capability_enabled)
      ADP_VALIDATION_TABLE            = aws_dynamodb_table.jobs[0].name
      ADP_VALIDATION_WORKER_ROLES     = join(",", sort(tolist(var.worker_role_arns)))
      ADP_TASK_AUTHORITY_ENDPOINT     = var.authority_endpoint
      ADP_VALIDATION_CLUSTER_NAME     = var.cluster_name
      ADP_VALIDATION_CLUSTER_ENDPOINT = data.aws_eks_cluster.validation[0].endpoint
      ADP_VALIDATION_CLUSTER_CA       = data.aws_eks_cluster.validation[0].certificate_authority[0].data
      ADP_VALIDATION_NAMESPACE        = var.validation_namespace
    }
  }
  lifecycle {
    precondition {
      condition     = var.api_execution_arn == "arn:${data.aws_partition.current.partition}:execute-api:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:${var.rest_api_id}" && alltrue([for arn in var.worker_role_arns : startswith(arn, "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/")])
      error_message = "Validation API and worker roles must belong to the selected AWS account and region."
    }
    precondition {
      condition     = var.isolation_qualified && var.image_uri != "" && length(var.subnet_ids) > 0 && length(var.worker_role_arns) > 0 && var.agent_registry_table_name != ""
      error_message = "Activation requires qualified isolation, immutable service image, private connectivity and explicit worker roles."
    }
    precondition {
      condition     = var.authority_endpoint == "https://${var.rest_api_id}.execute-api.${data.aws_region.current.region}.${data.aws_partition.current.dns_suffix}/${var.stage_name}/internal/v1/agent/task"
      error_message = "Task authority must use the selected API and stage."
    }
    precondition {
      condition     = data.aws_eks_cluster.validation[0].vpc_config[0].endpoint_private_access && alltrue([for subnet in data.aws_subnet.validation : subnet.vpc_id == data.aws_eks_cluster.validation[0].vpc_config[0].vpc_id && !subnet.map_public_ip_on_launch])
      error_message = "Validation requires a private EKS endpoint and private subnets in its VPC."
    }
  }
  depends_on = [aws_cloudwatch_log_group.service, aws_iam_role_policy.service, aws_vpc_security_group_egress_rule.https, aws_vpc_security_group_ingress_rule.eks_api]
}
resource "aws_lambda_function_event_invoke_config" "service" {
  count                        = local.active
  function_name                = aws_lambda_function.service[0].function_name
  maximum_event_age_in_seconds = 60
  maximum_retry_attempts       = 0
}
resource "aws_eks_access_entry" "service" {
  count             = local.active
  cluster_name      = var.cluster_name
  principal_arn     = aws_iam_role.service[0].arn
  kubernetes_groups = [local.group]
  type              = "STANDARD"
}
# Namespace, quota, restricted Pod Security and deny-all policy are managed by
# webhook-ingress/infra/codex-validation.tf. No permissions reach worker roles.
resource "kubernetes_role_binding" "service" {
  count = local.active
  metadata {
    name      = "validation-service"
    namespace = var.validation_namespace
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = "validation-host"
  }
  subject {
    kind      = "Group"
    name      = local.group
    api_group = "rbac.authorization.k8s.io"
  }
}
resource "kubernetes_cluster_role_binding" "namespace" {
  count = local.active
  metadata { name = "validation-service-namespace" }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = "adp-validation-namespace-read"
  }
  subject {
    kind      = "Group"
    name      = local.group
    api_group = "rbac.authorization.k8s.io"
  }
}
resource "aws_api_gateway_resource" "service" {
  count       = local.active
  rest_api_id = var.rest_api_id
  parent_id   = var.tools_parent_resource_id
  path_part   = "validation"
}
resource "aws_api_gateway_method" "service" {
  count         = local.active
  rest_api_id   = var.rest_api_id
  resource_id   = aws_api_gateway_resource.service[0].id
  http_method   = "POST"
  authorization = "AWS_IAM"
}
resource "aws_api_gateway_integration" "service" {
  count                   = local.active
  rest_api_id             = var.rest_api_id
  resource_id             = aws_api_gateway_resource.service[0].id
  http_method             = aws_api_gateway_method.service[0].http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.service[0].invoke_arn
  timeout_milliseconds    = 29000
}
resource "aws_lambda_permission" "api" {
  count          = local.active
  statement_id   = "ValidationApiOnly"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.service[0].function_name
  principal      = "apigateway.amazonaws.com"
  source_arn     = local.route_arn
  source_account = data.aws_caller_identity.current.account_id
}
resource "aws_iam_role_policy" "worker" {
  for_each = var.enabled ? var.worker_role_arns : toset([])
  name     = "${var.name}-invoke"
  role     = element(reverse(split("/", each.value)), 0)
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "execute-api:Invoke", Resource = local.route_arn
  }] })
}
output "service_role_arn" { value = try(aws_iam_role.service[0].arn, null) }
output "route_id" { value = try(aws_api_gateway_resource.service[0].id, null) }

resource "aws_dynamodb_table_item" "registry" {
  count      = local.active
  table_name = var.agent_registry_table_name
  hash_key   = "agent_id"
  item = jsonencode({
    agent_id              = { S = var.name }
    role_arn              = { S = aws_iam_role.service[0].arn }
    agent_name            = { S = var.name }
    org_id                = { S = "__platform__" }
    team_id               = { S = "__agents__" }
    owner                 = { S = "platform" }
    scope                 = { S = "internal" }
    requires_run_identity = { BOOL = false }
    status                = { S = "active" }
    allowed_models        = { L = [] }
    budget_config_id      = { S = "" }
    description           = { S = "Dedicated Task validation transport; Task proof authorizes each operation" }
  })
}
