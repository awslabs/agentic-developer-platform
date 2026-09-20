# Publish one Terraform-owned configuration source consumed directly by the
# gateway pod in every deployment path, including deploy-all.sh. No per-account
# SSM edits or kubectl set-env commands are required for authority configuration.
locals {
  worker_gateway_config = merge({
    AGENT_AUTHORITY_ENABLED               = tostring(var.agent_authority_enabled)
    PERSONA_MODEL_MAPPING_ENABLED         = tostring(var.persona_model_mapping_enabled)
    PERSONA_MODEL_PRODUCER_ROLES          = join(",", concat([aws_iam_role.lambda_execution.arn], var.persona_model_additional_producer_roles))
    ADP_RUN_TASKS_ENABLED                 = tostring(var.agent_authority_enabled)
    ADP_DOOR_SERVICE_URL                  = var.agent_door_service_url
    AGENT_RUN_LOGS_BUCKET                 = aws_s3_bucket.agent_run_logs.bucket
    AGENT_FALLBACK_BUCKET                 = aws_s3_bucket.agent_run_logs.bucket
    ADP_WORK_CLAIMS_ENABLED               = tostring(var.agent_authority_enabled)
    ADP_WORK_CLAIM_PRODUCER_ROLES         = aws_iam_role.lambda_execution.arn
    AGENT_WORKER_IMAGE_DIGESTS            = join(",", sort(tolist(var.agent_authority_worker_image_digests)))
    AGENT_CONTROL_ENVELOPE_KEY_ID         = local.agent_authority_key_id
    AGENT_TASK_SOURCE_ROLE_ARN            = aws_iam_role.agent_scaledjob.arn
    AGENT_TASK_SOURCE_EKS_CLUSTER         = local.eks_cluster_name
    AGENT_TASK_SOURCE_ISOLATION_CONFIRMED = tostring(var.agent_task_source_isolation_confirmed)
    }, var.agent_authority_enabled ? {
    AGENT_AUTHORITY_TABLE    = aws_dynamodb_table.agent_authority.name
    WEBHOOK_EVENTS_TABLE     = aws_dynamodb_table.webhook_events.name
    AGENT_DISPATCH_QUEUE_URL = aws_sqs_queue.agent_submit.url
    ADP_RUN_TASK_QUEUE_URL   = aws_sqs_queue.agent_submit.url
  } : {})
}

resource "kubernetes_config_map" "worker_gateway" {
  count = local.agent_authority_provisioned ? 1 : 0
  metadata {
    name      = "adp-worker-authority-config"
    namespace = var.gateway_namespace
  }
  data = local.worker_gateway_config
}

resource "terraform_data" "worker_gateway_rollout" {
  count = local.agent_authority_provisioned ? 1 : 0
  triggers_replace = {
    configuration  = sha256(jsonencode(local.worker_gateway_config))
    marker_version = var.agent_authority_enabled ? data.aws_secretsmanager_secret_version.worker_marker[0].version_id : "disabled"
    rollout_script = filesha256("${path.module}/../scripts/rollout-worker-gateway.sh")
  }
  provisioner "local-exec" {
    command = "bash \"${path.module}/../scripts/rollout-worker-gateway.sh\""
    environment = {
      ADP_CLUSTER           = local.eks_cluster_name
      ADP_REGION            = var.aws_region
      ADP_NAMESPACE         = var.gateway_namespace
      ADP_AUTHORITY_ENABLED = tostring(var.agent_authority_enabled)
    }
  }
  depends_on = [
    kubernetes_config_map.worker_gateway,
    kubernetes_secret.agent_authority,
    kubernetes_secret.worker_run_services,
    aws_iam_role_policy.gateway_authorized_dispatch,
    aws_iam_role_policy_attachment.gateway_authorized_dispatch,
    kubernetes_cluster_role_binding.gateway_agent_tokenreview,
    kubernetes_role_binding.gateway_agent_pod_read,
    terraform_data.worker_security_rollout,
  ]
}

# The gateway-owned tick is planned before this module on fresh installations.
# A path lookup lets its first pass see no wiring; the normal second pass can
# consume these non-secret facts once webhook infrastructure exists.
resource "aws_ssm_parameter" "worker_runtime_wiring" {
  name = "/adp/${var.environment}/webhook-ingress/worker-runtime/wiring"
  type = "String"
  value = jsonencode({
    webhook_events_table       = aws_dynamodb_table.webhook_events.name
    webhook_events_kms_key_arn = aws_kms_key.dynamodb.arn
    dispatch_queue_arn         = aws_sqs_queue.agent_submit.arn
    dispatch_queue_url         = aws_sqs_queue.agent_submit.url
  })
}
