# Server-managed signing material is never included in worker payloads. Gateway
# pods read the Kubernetes Secret; the tick reads the same key from encrypted SSM.
# Worker role permissions are unchanged. Shared AdministratorAccess does not
# provide tamper-proof IAM isolation from SSM, Kubernetes or Terraform state.
resource "random_password" "agent_run_credential" {
  count   = 1
  length  = 64
  special = false
}

resource "tls_private_key" "agent_control_envelope" {
  count     = local.agent_authority_provisioned ? 1 : 0
  algorithm = "ED25519"
}

resource "tls_private_key" "agent_control_envelope_secondary" {
  count     = local.agent_authority_provisioned ? 1 : 0
  algorithm = "ED25519"
}

locals {
  agent_control_key_slots = local.agent_authority_provisioned ? {
    primary   = tls_private_key.agent_control_envelope[0]
    secondary = tls_private_key.agent_control_envelope_secondary[0]
  } : {}
  agent_control_active_key = local.agent_authority_provisioned ? local.agent_control_key_slots[var.agent_control_signing_key_slot] : null
  agent_control_verification_keys = {
    for slot, key in local.agent_control_key_slots : substr(sha256(key.public_key_pem), 0, 16) => key.public_key_pem
    if var.agent_control_publish_both_keys || slot == var.agent_control_signing_key_slot
  }
  agent_authority_key_id = local.agent_authority_provisioned ? substr(sha256(local.agent_control_active_key.public_key_pem), 0, 16) : ""
  # Reporting needs the existing gateway endpoint in both IAM modes. The
  # authority flag and pod-proof fields remain protected-mode only.
  # Shared with the disposable evaluation worker: preparation must not require
  # activating the ordinary ScaledJob just to obtain its protected pod fields.
  agent_authority_pod = jsondecode(file("${path.module}/protected-worker-pod.json"))
  agent_authority_env_block = var.agent_authority_enabled ? "                  ${indent(18, yamlencode(concat(local.agent_authority_pod.container.env, [
    { name = "ADP_AGENT_CONTROL_ENDPOINT", value = "${data.aws_ssm_parameter.gateway_apigw_invoke_url.value}/internal/v1/agent" },
    { name = "ADP_CONTROL_ENVELOPE_KEYS", value = jsonencode(local.agent_control_verification_keys) },
    ])))}" : join("\n", concat([
    "                  - name: ADP_AGENT_CONTROL_ENDPOINT",
    "                    value: ${data.aws_ssm_parameter.gateway_apigw_invoke_url.value}/internal/v1/agent",
    ], var.task_api_worker_enabled ? [
      "                  - name: ADP_TASK_API_WORKER_ENABLED",
      "                    value: \"true\"",
      "                  - name: ADP_WORKLOAD_TOKEN_FILE",
      "                    value: /var/run/adp-workload/token",
  ] : []))
  agent_authority_mount_block = var.agent_authority_enabled ? "                ${indent(16, yamlencode({ volumeMounts = local.agent_authority_pod.container.volumeMounts }))}" : var.task_api_worker_enabled ? "                ${indent(16, yamlencode({ volumeMounts = [local.agent_authority_pod.container.volumeMounts[0]] }))}" : ""
  agent_authority_volume_block = var.agent_authority_enabled ? "            ${indent(12, yamlencode({ volumes = local.agent_authority_pod.volumes }))}" : var.task_api_worker_enabled ? "            ${indent(12, yamlencode({ volumes = [local.agent_authority_pod.volumes[0]] }))}" : ""

}

resource "kubernetes_secret" "agent_authority" {
  count = 1
  metadata {
    name      = "agent-authority-signing"
    namespace = var.gateway_namespace
  }
  data = merge({
    run-credential-key = random_password.agent_run_credential[0].result
    }, local.agent_authority_provisioned ? {
    envelope-signing-key = local.agent_control_active_key.private_key_pem
  } : {})
  lifecycle {
    precondition {
      condition     = !var.agent_authority_enabled || length(var.agent_authority_worker_image_digests) > 0
      error_message = "Enabling agent authority requires approved worker image digests."
    }
  }
}

resource "kubernetes_config_map" "agent_control_verification_keys" {
  count = local.agent_authority_provisioned ? 1 : 0
  metadata {
    name      = "adp-control-verification-keys"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name
  }
  data = { "keys.json" = jsonencode(local.agent_control_verification_keys) }
}

resource "kubernetes_cluster_role" "gateway_agent_tokenreview" {
  count = local.agent_authority_provisioned ? 1 : 0
  metadata { name = "adp-${var.environment}-gateway-agent-tokenreview" }
  rule {
    api_groups = ["authentication.k8s.io"]
    resources  = ["tokenreviews"]
    verbs      = ["create"]
  }
}

resource "kubernetes_cluster_role_binding" "gateway_agent_tokenreview" {
  count = local.agent_authority_provisioned ? 1 : 0
  metadata { name = "adp-${var.environment}-gateway-agent-tokenreview" }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role.gateway_agent_tokenreview[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = "gateway-service"
    namespace = var.gateway_namespace
  }
}

resource "kubernetes_role" "gateway_agent_pod_read" {
  count = local.agent_authority_provisioned ? 1 : 0
  metadata {
    name      = "gateway-agent-pod-read"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name
  }
  rule {
    api_groups = [""]
    resources  = ["pods"]
    # Abort recovery retains exact pods with a finalizer, discovers pending
    # reports even without SQL work claims, then removes its own finalizer.
    # The gateway tests UID and resourceVersion on every JSON patch.
    verbs = ["get", "list", "patch"]
  }
  # The owning Job's deadline includes bootstrap and previous pod attempts.
  # No worker receives Kubernetes API permissions or a caller-selected lookup.
  rule {
    api_groups = ["batch"]
    resources  = ["jobs"]
    verbs      = ["get"]
  }
}

resource "kubernetes_role_binding" "gateway_agent_pod_read" {
  count = local.agent_authority_provisioned ? 1 : 0
  metadata {
    name      = "gateway-agent-pod-read"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.gateway_agent_pod_read[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = "gateway-service"
    namespace = var.gateway_namespace
  }
}

resource "aws_ssm_parameter" "agent_authority_enabled" {
  name   = "/adp/${var.environment}/gateway/agent-authority-enabled"
  type   = "SecureString"
  key_id = aws_kms_key.dynamodb.arn
  value  = tostring(var.agent_authority_enabled)
}

resource "aws_ssm_parameter" "agent_authority_worker_images" {
  name   = "/adp/${var.environment}/gateway/agent-authority-worker-images"
  type   = "SecureString"
  key_id = aws_kms_key.dynamodb.arn
  value  = length(var.agent_authority_worker_image_digests) > 0 ? join(",", sort(tolist(var.agent_authority_worker_image_digests))) : "disabled"
}

resource "aws_ssm_parameter" "agent_authority_key_id" {
  name   = "/adp/${var.environment}/gateway/agent-authority-key-id"
  type   = "SecureString"
  key_id = aws_kms_key.dynamodb.arn
  value  = var.agent_authority_enabled ? local.agent_authority_key_id : "disabled"
}

# The tick reuses the exact gateway key. Only the parameter name is exported in
# runtime wiring; key plaintext never appears in outputs or Lambda environment.
resource "aws_ssm_parameter" "agent_run_reporting_key" {
  name   = "/adp/${var.environment}/gateway/run-report-signing-key"
  type   = "SecureString"
  key_id = aws_kms_key.dynamodb.arn
  value  = random_password.agent_run_credential[0].result
}
