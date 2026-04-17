# =============================================================================
# ARC Runner — Helm releases for Actions Runner Controller
# =============================================================================
# Deploys the ARC controller and runner scale set onto the shared EKS cluster.
# =============================================================================

resource "kubernetes_namespace" "arc_system" {
  metadata {
    name = "arc-systems"
    labels = {
      "app.kubernetes.io/part-of" = "actions-runner-controller"
    }
  }
}

resource "kubernetes_namespace" "arc_runners" {
  metadata {
    name = var.runner_namespace
    labels = {
      "app.kubernetes.io/part-of" = "actions-runner-controller"
    }
  }
}

# ARC Controller
resource "helm_release" "arc_controller" {
  name       = "arc"
  namespace  = kubernetes_namespace.arc_system.metadata[0].name
  repository = "oci://ghcr.io/actions/actions-runner-controller-charts"
  chart      = "gha-runner-scale-set-controller"
  version    = "0.10.1"

  values = [
    yamlencode({
      replicaCount = 1
    })
  ]
}

# GitHub App credentials for the runner scale set
data "aws_secretsmanager_secret_version" "app_id" {
  secret_id = var.github_app_id_secret_name
}

data "aws_secretsmanager_secret_version" "app_key" {
  secret_id = var.github_app_private_key_secret_name
}

# K8s secret consumed by the runner scale set helm chart. The chart expects
# the key `github_app_id`, `github_app_installation_id`, `github_app_private_key`.
resource "kubernetes_secret" "arc_runner" {
  metadata {
    name      = "github-arc-secret"
    namespace = kubernetes_namespace.arc_runners.metadata[0].name
  }

  data = {
    github_app_id              = data.aws_secretsmanager_secret_version.app_id.secret_string
    github_app_installation_id = var.github_app_installation_id
    github_app_private_key     = data.aws_secretsmanager_secret_version.app_key.secret_string
  }

  type = "Opaque"
}

# ARC Runner Scale Set (org-level)
resource "helm_release" "arc_runner_set" {
  name       = "arc-runner-org"
  namespace  = kubernetes_namespace.arc_runners.metadata[0].name
  repository = "oci://ghcr.io/actions/actions-runner-controller-charts"
  chart      = "gha-runner-scale-set"
  version    = "0.10.1"

  values = [
    yamlencode({
      githubConfigUrl    = var.github_repo != "" ? "https://github.com/${var.github_org}/${var.github_repo}" : "https://github.com/${var.github_org}"
      githubConfigSecret = kubernetes_secret.arc_runner.metadata[0].name
      maxRunners         = 10
      minRunners         = 0
      template = {
        metadata = {
          annotations = {
            "karpenter.sh/do-not-disrupt" = "true"
          }
        }
        spec = {
          serviceAccountName = kubernetes_service_account.runner.metadata[0].name
          containers = [{
            name  = "runner"
            image = "ghcr.io/actions/actions-runner:latest"
            resources = {
              requests = {
                cpu    = "500m"
                memory = "1Gi"
              }
              limits = {
                cpu    = "4"
                memory = "8Gi"
              }
            }
          }]
        }
      }
    })
  ]

  depends_on = [
    helm_release.arc_controller,
    kubernetes_secret.arc_runner,
  ]
}

# Service account for runner pods (IRSA)
resource "kubernetes_service_account" "runner" {
  metadata {
    name      = "github-runner-sa"
    namespace = kubernetes_namespace.arc_runners.metadata[0].name
    annotations = {
      "eks.amazonaws.com/role-arn" = var.runner_role_arn
    }
  }
}
