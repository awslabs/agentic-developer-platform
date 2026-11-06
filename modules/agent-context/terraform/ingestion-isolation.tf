# #4720: only the short-lived admission init container can receive queue work.
# Content workers have neither a service-account token nor shared AWS authority.
data "aws_partition" "ingestion" {}

locals {
  ingestion_receiver_actions = ["sqs:ReceiveMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"]
  ingestion_receiver_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = local.ingestion_receiver_actions
      Resource = module.sqs_ingestion.queue_arn
    }]
  })
}

resource "aws_iam_policy" "ingestion_receiver_boundary" {
  name   = "${local.name_prefix}-ingestion-receiver-boundary"
  policy = local.ingestion_receiver_policy
}

resource "aws_iam_role" "ingestion_receiver" {
  name                 = "${local.name_prefix}-ingestion-receiver"
  permissions_boundary = aws_iam_policy.ingestion_receiver_boundary.arn
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Action    = "sts:AssumeRoleWithWebIdentity"
        Principal = { Federated = local.oidc_provider_arn }
        Condition = { StringEquals = {
          "${local.oidc_issuer}:sub" = "system:serviceaccount:${var.namespace}:ingestion-receiver"
          "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
        } }
      },
      {
        Effect    = "Allow"
        Action    = "sts:AssumeRole"
        Principal = { AWS = data.aws_iam_role.keda_operator.arn }
      }
    ]
  })
}

resource "aws_iam_role_policy" "ingestion_receiver" {
  name   = "ingestion-queue-only"
  role   = aws_iam_role.ingestion_receiver.id
  policy = local.ingestion_receiver_policy
}

resource "aws_iam_role_policy" "ingestion_keda_assume" {
  name = "${local.name_prefix}-receiver-polling"
  role = data.aws_iam_role.keda_operator.name
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Resource = aws_iam_role.ingestion_receiver.arn }]
  })
}

# The gateway verifies the persisted asset and attempt before every operation.
# Its credentials stay in the gateway; content workers get exact-object URLs.
resource "aws_iam_role_policy" "ingestion_control" {
  name = "${local.name_prefix}-ingestion-control"
  role = data.terraform_remote_state.platform.outputs.gateway_service_irsa_role_name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "arn:${data.aws_partition.ingestion.partition}:s3:::${local.bucket_name}/*" },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = "arn:${data.aws_partition.ingestion.partition}:s3:::${local.bucket_name}" },
      { Effect = "Allow", Action = ["dynamodb:PutItem"], Resource = module.dynamodb_state.table_arn },
      { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"], Resource = aws_kms_key.dynamodb.arn },
      { Effect = "Allow", Action = ["sqs:DeleteMessage"], Resource = module.sqs_ingestion.queue_arn },
      {
        Effect = "Allow"
        Action = ["s3vectors:CreateIndex", "s3vectors:PutVectors", "s3vectors:QueryVectors", "s3vectors:ListVectors", "s3vectors:DeleteVectors"]
        Resource = [
          "arn:${data.aws_partition.ingestion.partition}:s3vectors:${var.aws_region}:${var.account_id}:bucket/${module.s3_vectors.vector_bucket_name}",
          "arn:${data.aws_partition.ingestion.partition}:s3vectors:${var.aws_region}:${var.account_id}:bucket/${module.s3_vectors.vector_bucket_name}/index/*"
        ]
      }
      ], var.neptune_enabled ? [{
        Effect   = "Allow"
        Action   = ["neptune-db:ReadDataViaQuery", "neptune-db:WriteDataViaQuery", "neptune-db:DeleteDataViaQuery"]
        Resource = "arn:${data.aws_partition.ingestion.partition}:neptune-db:${var.aws_region}:${var.account_id}:${module.neptune_serverless[0].cluster_resource_id}/*"
    }] : [])
  })
}

resource "kubernetes_config_map_v1" "ingestion_control" {
  metadata {
    name      = "adp-ingestion-control"
    namespace = "adp-gateway"
  }
  data = merge({
    INGESTION_ISOLATION_VERSION  = "1"
    AGENT_CONTEXT_S3_BUCKET      = local.bucket_name
    AGENT_CONTEXT_VECTORS_BUCKET = module.s3_vectors.vector_bucket_name
    S3_VECTORS_SHARD_COUNT       = tostring(var.s3_vectors_shard_count)
    INGESTION_STATE_TABLE        = module.dynamodb_state.table_name
    INGESTION_QUEUE_URL          = module.sqs_ingestion.queue_url
    INGESTION_REFRESH_ISSUER     = "https://${trimprefix(local.oidc_issuer, "https://")}"
    INGESTION_REFRESH_SUBJECT    = "system:serviceaccount:${var.namespace}:ingestion-refresh"
    INGESTION_NEPTUNE_ENDPOINT   = var.neptune_enabled ? module.neptune_serverless[0].cluster_endpoint : ""
    }, var.product_context_storage_enabled ? {
    PRODUCT_CONTEXT_STORAGE_ROLE_ARN    = module.product_context_storage[0].session_role_arn
    PRODUCT_CONTEXT_DEFAULT_KMS_KEY_ARN = module.product_context_storage[0].shared_key_arn
    PRODUCT_CONTEXT_KMS_ALIAS_PREFIX    = "adp-context-${var.environment}"
  } : {})
}

output "ingestion_receiver_role_arn" {
  value       = aws_iam_role.ingestion_receiver.arn
  description = "Queue-only identity for the ingestion admission init container"
}

resource "aws_ssm_parameter" "ingestion_receiver_role" {
  name  = "/adp/${var.environment}/agent-context/ingestion-receiver-role-arn"
  type  = "String"
  value = aws_iam_role.ingestion_receiver.arn
}
