# Gateway-only signing material lives in the gateway namespace, outside the
# worker's Secrets Manager adp/* read grant. Workers have no Kubernetes Secret
# read permission. Terraform state is sensitive and remains in the platform
# backend, which is outside the worker's beads/log/evidence S3 grants.
resource "random_password" "agent_run_credential" {
  count   = var.agent_authority_enabled ? 1 : 0
  length  = 64
  special = false
}

resource "tls_private_key" "agent_control_envelope" {
  count     = var.agent_authority_enabled ? 1 : 0
  algorithm = "ED25519"
}

resource "tls_private_key" "agent_control_envelope_secondary" {
  count     = var.agent_authority_enabled ? 1 : 0
  algorithm = "ED25519"
}

locals {
  agent_control_key_slots = var.agent_authority_enabled ? {
    primary   = tls_private_key.agent_control_envelope[0]
    secondary = tls_private_key.agent_control_envelope_secondary[0]
  } : {}
  agent_control_active_key = var.agent_authority_enabled ? local.agent_control_key_slots[var.agent_control_signing_key_slot] : null
  agent_control_verification_keys = {
    for slot, key in local.agent_control_key_slots : substr(sha256(key.public_key_pem), 0, 16) => key.public_key_pem
    if var.agent_control_publish_both_keys || slot == var.agent_control_signing_key_slot
  }
  agent_authority_key_id = var.agent_authority_enabled ? substr(sha256(local.agent_control_active_key.public_key_pem), 0, 16) : ""
  agent_authority_env_block = var.agent_authority_enabled ? join("\n", [
    "                  - name: ADP_AGENT_AUTHORITY_ENABLED",
    "                    value: \"true\"",
    "                  - name: ADP_AGENT_CONTROL_ENDPOINT",
    "                    value: ${data.aws_ssm_parameter.gateway_apigw_invoke_url.value}/internal/v1/agent",
    "                  - name: ADP_WORKLOAD_TOKEN_FILE",
    "                    value: /var/run/adp-workload/token",
    "                  - name: ADP_CONTROL_ENVELOPE_KEYS",
    "                    value: '${jsonencode(local.agent_control_verification_keys)}'",
    "                  - name: ADP_CONTROL_ENVELOPE_KEYS_FILE",
    "                    value: /var/run/adp-control-keys/keys.json",
  ]) : ""
  agent_authority_mount_block = var.agent_authority_enabled ? join("\n", [
    "                volumeMounts:",
    "                  - name: adp-workload-identity",
    "                    mountPath: /var/run/adp-workload",
    "                    readOnly: true",
    "                  - name: adp-control-verification-keys",
    "                    mountPath: /var/run/adp-control-keys",
    "                    readOnly: true",
  ]) : ""
  agent_authority_volume_block = var.agent_authority_enabled ? join("\n", [
    "            volumes:",
    "              - name: adp-workload-identity",
    "                projected:",
    "                  sources:",
    "                    - serviceAccountToken:",
    "                        audience: adp-agent-bootstrap",
    "                        expirationSeconds: 3600",
    "                        path: token",
    "              - name: adp-control-verification-keys",
    "                configMap:",
    "                  name: adp-control-verification-keys",
  ]) : ""
}

resource "kubernetes_secret" "agent_authority" {
  count = var.agent_authority_enabled ? 1 : 0
  metadata {
    name      = "agent-authority-signing"
    namespace = var.gateway_namespace
  }
  data = {
    run-credential-key   = random_password.agent_run_credential[0].result
    envelope-signing-key = local.agent_control_active_key.private_key_pem
  }
  lifecycle {
    precondition {
      condition     = length(var.agent_authority_worker_image_digests) > 0
      error_message = "Enabling agent authority requires approved worker image digests."
    }
  }
}

resource "kubernetes_config_map" "agent_control_verification_keys" {
  count = var.agent_authority_enabled ? 1 : 0
  metadata {
    name      = "adp-control-verification-keys"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name
  }
  data = { "keys.json" = jsonencode(local.agent_control_verification_keys) }
}

resource "kubernetes_cluster_role" "gateway_agent_tokenreview" {
  count = var.agent_authority_enabled ? 1 : 0
  metadata { name = "adp-${var.environment}-gateway-agent-tokenreview" }
  rule {
    api_groups = ["authentication.k8s.io"]
    resources  = ["tokenreviews"]
    verbs      = ["create"]
  }
}

resource "kubernetes_cluster_role_binding" "gateway_agent_tokenreview" {
  count = var.agent_authority_enabled ? 1 : 0
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
  count = var.agent_authority_enabled ? 1 : 0
  metadata {
    name      = "gateway-agent-pod-read"
    namespace = kubernetes_namespace.adp_agents.metadata[0].name
  }
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["get"]
  }
}

resource "kubernetes_role_binding" "gateway_agent_pod_read" {
  count = var.agent_authority_enabled ? 1 : 0
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
