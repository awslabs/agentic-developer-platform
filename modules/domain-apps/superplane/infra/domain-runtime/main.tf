data "aws_caller_identity" "current" {}

data "aws_eks_cluster" "selected" {
  name = var.cluster_name
}

data "aws_iam_openid_connect_provider" "selected" {
  arn = "arn:aws:iam::${var.account_id}:oidc-provider/${trimprefix(var.oidc_issuer, "https://")}"
}

data "aws_iam_role" "keda_operator" {
  name = trimprefix(var.keda_operator_role_arn, "arn:aws:iam::${var.account_id}:role/")
}

locals {
  prefix    = "adp-${var.environment}-superplane-domain"
  queue     = "${local.prefix}-operations"
  worker    = "${local.prefix}-worker"
  observer  = "${local.prefix}-observer"
  issuer    = trimprefix(var.oidc_issuer, "https://")
  queue_arn = "arn:aws:sqs:${var.region}:${var.account_id}:${local.queue}"
  tags = {
    "adp.aws-e.io/installation" = var.installation_id
    "adp.aws-e.io/component"    = "superplane-domain-runtime"
  }
  target_verified = (
    data.aws_caller_identity.current.account_id == var.account_id &&
    startswith(data.aws_caller_identity.current.arn, "arn:aws:sts::${var.account_id}:assumed-role/${trimprefix(var.operator_role_arn, "arn:aws:iam::${var.account_id}:role/")}/") &&
    data.aws_eks_cluster.selected.arn == "arn:aws:eks:${var.region}:${var.account_id}:cluster/${var.cluster_name}" &&
    data.aws_eks_cluster.selected.identity[0].oidc[0].issuer == var.oidc_issuer &&
    data.aws_iam_openid_connect_provider.selected.url == local.issuer &&
    data.aws_iam_role.keda_operator.arn == var.keda_operator_role_arn
  )
  gateway_prefix = "arn:aws:execute-api:${var.region}:${var.account_id}:${var.api_id}/${var.api_stage}/POST/internal/v1/controller-execution/"
  worker_routes = [
    "task/acquire", "bootstrap", "task/heartbeat", "renew", "task/ack",
    "lease", "task/status", "authority", "recovery/scope", "recovery/authority",
    "recovery/observe", "recovery/inventory", "recovery/lifecycle",
    "recovery/account-creation", "recovery/bootstrap", "recovery/settlement",
  ]
}

resource "aws_sqs_queue" "operations" {
  name                       = local.queue
  fifo_queue                 = false
  sqs_managed_sse_enabled    = true
  visibility_timeout_seconds = 3600
  message_retention_seconds  = 345600
  tags                       = local.tags

  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = local.target_verified
      error_message = "The selected account, EKS cluster, OIDC provider or KEDA operator role changed."
    }
  }
}

resource "aws_iam_role" "worker" {
  name = local.worker
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = data.aws_iam_openid_connect_provider.selected.arn }
      Condition = { StringEquals = {
        "${local.issuer}:aud" = "sts.amazonaws.com"
        "${local.issuer}:sub" = "system:serviceaccount:${var.namespace}:superplane-paid-worker"
      } }
    }]
  })
  tags = local.tags

  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = local.target_verified
      error_message = "The selected account, EKS cluster, OIDC provider or KEDA operator role changed."
    }
  }
}

resource "aws_iam_role_policy" "worker" {
  name = "${local.worker}-invoke"
  role = aws_iam_role.worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "execute-api:Invoke"
      Resource = [for route in local.worker_routes : "${local.gateway_prefix}${route}"]
    }]
  })
}

resource "aws_iam_role" "observer" {
  name = local.observer
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { AWS = data.aws_iam_role.keda_operator.arn }
    }]
  })
  tags = local.tags

  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = local.target_verified
      error_message = "The selected account, EKS cluster, OIDC provider or KEDA operator role changed."
    }
  }
}

resource "aws_iam_role_policy" "observer" {
  name = "${local.observer}-attributes"
  role = aws_iam_role.observer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "sqs:GetQueueAttributes"
      Resource = local.queue_arn
    }]
  })
}
