# =============================================================================
# Outputs — Issue #5042 (U3).
# =============================================================================
# Consumed by the rollout lane and by U19's SkyPilot resource/state handover. Deliberately
# narrow: outputs are the module's public surface, and a broad one invites another module
# to depend on an internal detail.
#
# No secret is output. Secret NAMES are, because they are identifiers the rollout needs in
# order to construct a secret reference; the material itself never enters this module.
# =============================================================================

output "control_plane_role_arn" {
  description = "IRSA role ARN for Superplane API/controller pods."
  value       = aws_iam_role.control_plane.arn
}

output "skypilot_role_arn" {
  description = "IRSA role ARN for the SkyPilot API server."
  value       = aws_iam_role.skypilot.arn
}

output "namespace" {
  description = "Kubernetes namespace owned by the Superplane domain app."
  value       = var.namespace
}

output "skypilot_namespace" {
  description = "Kubernetes namespace for the SkyPilot API service."
  value       = var.skypilot_namespace
}

output "ecr_repository_urls" {
  description = "ECR repository URLs for the pinned Superplane images, keyed by repository name."
  value       = { for name, repo in aws_ecr_repository.superplane : name => repo.repository_url }
}

output "skypilot_image" {
  description = "Digest-pinned SkyPilot API image reference, resolved from releases/superplane.lock.yaml."
  value       = local.skypilot_image
}

output "cors_allowed_origins" {
  description = "The validated CORS origin allowlist actually applied (never '*' with credentials)."
  value       = var.cors_allowed_origins
}

output "state_key_convention" {
  description = <<-EOT
    The per-environment state key this module must be initialised with. Output so an
    operator can confirm from a plan that the environment they targeted is the one whose
    state they are writing, without reading the backend file.
  EOT
  value       = "${var.environment}/modules/superplane/terraform.tfstate"
}

# Named secret references, for the rollout lane to turn into env-var references. Values
# are names; `sensitive` is not set because a secret name is not secret material and
# marking it so would only obscure it in a plan an operator needs to read.
output "secret_names" {
  description = "Secrets Manager secret NAMES (never values) the pods resolve at runtime."
  value = {
    database = var.database_secret_name
    jwt      = var.jwt_secret_name
  }
}
