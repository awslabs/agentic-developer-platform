# =============================================================================
# Runtime configuration exposed to the control plane — Issue #5042 (U3).
# =============================================================================
# These SSM parameters are how the rollout lane and the pods learn what Terraform
# decided, without either side hardcoding it. The rollout reads them (see
# superplane-k8s-deploy.yml) so that "which role does the service account annotate with"
# has one answer produced by apply, not two answers that can drift.
#
# WHY CONFIGURATION AND NOT SECRETS
#
# Only non-secret values are written here: role ARNs, an origin allowlist, a namespace, a
# region. Secret NAMES are written (they are identifiers, not material); secret VALUES
# are not, and cannot be — this module never receives them. Terraform state is not a
# secret store, and `aws_ssm_parameter` with a secret value writes that value to state in
# plaintext regardless of the parameter's own type.
# =============================================================================

locals {
  # The CORS allowlist, validated in variables.tf to exclude "*" while credentials are
  # allowed. Serialized as JSON because the API's own setting (`cors_origins`) is a list.
  cors_origins_json = jsonencode(var.cors_allowed_origins)

  parameter_prefix = "/adp/${var.environment}/superplane"

  # The digest-pinned SkyPilot image, read from U2's lock rather than restated. The lock
  # is the single place that records what runs; a second copy here could disagree with it.
  # `resolved_digest`'s guarantee in resolve_lock.py is that anything in `images` is
  # digest-addressed, and tests/lock_pin.tftest.hcl re-asserts the shape from this side.
  skypilot_digest = local.lock.images["skypilot-api"]
  skypilot_image = format(
    "%s/%s@%s",
    local.lock.image_sources["skypilot-api"].registry,
    local.lock.image_sources["skypilot-api"].repository,
    local.skypilot_digest,
  )
}

resource "aws_ssm_parameter" "control_plane_role_arn" {
  name        = "${local.parameter_prefix}/control-plane-role-arn"
  description = "IRSA role for Superplane API/controller pods."
  type        = "String"
  value       = aws_iam_role.control_plane.arn
}

resource "aws_ssm_parameter" "skypilot_role_arn" {
  name        = "${local.parameter_prefix}/skypilot-role-arn"
  description = "IRSA role for the SkyPilot API server."
  type        = "String"
  value       = aws_iam_role.skypilot.arn
}

resource "aws_ssm_parameter" "namespace" {
  name        = "${local.parameter_prefix}/namespace"
  description = "Kubernetes namespace owned by the Superplane domain app."
  type        = "String"
  value       = var.namespace
}

# The SkyPilot namespace, published for the same reason as the one above and NOT derivable
# from it: `var.namespace` and `var.skypilot_namespace` are separate inputs with separate
# defaults (`superplane` and `skypilot`), and irsa.tf scopes a different OIDC `sub`
# condition to each. A rollout that guessed one from the other would render manifests whose
# service accounts cannot assume their roles — which surfaces as opaque AWS 403s from
# inside the pod, not as a rollout failure.
#
# Added by PR #5283's finding-4 repair: the rollout lane must validate BOTH namespace
# inputs against the rendered objects, and it can only do that if both are published.
resource "aws_ssm_parameter" "skypilot_namespace" {
  name        = "${local.parameter_prefix}/skypilot-namespace"
  description = "Kubernetes namespace for the SkyPilot API service (U2 lock: skypilot_config.namespace)."
  type        = "String"
  value       = var.skypilot_namespace
}

# The region the rollout renders into pod environment variables for the IRSA/STS exchange.
# Published rather than restated in the workflow: the workflow's own AWS_REGION is a
# separate literal, and two literals that must agree are a drift waiting to happen.
resource "aws_ssm_parameter" "aws_region" {
  name        = "${local.parameter_prefix}/aws-region"
  description = "Region the control plane runs in; rendered into pod env for the IRSA/STS exchange."
  type        = "String"
  value       = var.aws_region
}

# The allowlist the API must apply instead of upstream's `["*"]`. Written by Terraform so
# the value the pods read is the one that passed validation — the rollout does not get to
# supply its own.
resource "aws_ssm_parameter" "cors_allowed_origins" {
  name        = "${local.parameter_prefix}/cors-allowed-origins"
  description = "Explicit CORS origin allowlist for the domain API (never '*' with credentials)."
  type        = "String"
  value       = local.cors_origins_json
}

# Secret NAMES, not values. The pod resolves these through its scoped IRSA role at
# runtime; nothing here lets a reader of state or a plan recover the material.
resource "aws_ssm_parameter" "database_secret_name" {
  name        = "${local.parameter_prefix}/database-secret-name"
  description = "Name of the Secrets Manager secret holding database connection details."
  type        = "String"
  value       = var.database_secret_name
}

# The schema boundary the migration lane renders into the Job's `search_path` and version
# table. Published rather than hardcoded in the workflow for the same reason as the
# namespaces: the value that constrains name resolution in the database must be the value
# that passed `variables.tf`'s `public`-rejecting validation, not a second literal in a
# workflow that nothing validates.
resource "aws_ssm_parameter" "database_schema" {
  name        = "${local.parameter_prefix}/database-schema"
  description = "Postgres schema owned by the domain app; sole search_path entry for the migration Job."
  type        = "String"
  value       = var.database_schema
}

resource "aws_ssm_parameter" "jwt_secret_name" {
  name        = "${local.parameter_prefix}/jwt-secret-name"
  description = "Name of the Secrets Manager secret holding the JWT signing key."
  type        = "String"
  value       = var.jwt_secret_name
}

resource "aws_ssm_parameter" "skypilot_image" {
  name        = "${local.parameter_prefix}/skypilot-image"
  description = "Digest-pinned SkyPilot API image, resolved from releases/superplane.lock.yaml."
  type        = "String"
  value       = local.skypilot_image
}

# The GPU workspace target. Empty is meaningful — see variables.tf: it means no
# Kubernetes workspace cluster is configured, so GPU workloads cannot silently land on
# the ADP management cluster.
resource "aws_ssm_parameter" "workspace_cluster_context" {
  name        = "${local.parameter_prefix}/workspace-cluster-context"
  description = "Kubernetes context for GPU workspace scheduling. Empty = not configured."
  type        = "String"
  value       = var.workspace_cluster_context != "" ? var.workspace_cluster_context : "none"
}
