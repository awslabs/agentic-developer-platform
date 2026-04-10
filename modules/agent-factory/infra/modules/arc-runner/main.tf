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

# ARC Runner Scale Set (org-level)
resource "helm_release" "arc_runner_set" {
  name       = "arc-runner-org"
  namespace  = kubernetes_namespace.arc_runners.metadata[0].name
  repository = "oci://ghcr.io/actions/actions-runner-controller-charts"
  chart      = "gha-runner-scale-set"
  version    = "0.10.1"

  values = [
    yamlencode({
      githubConfigUrl = "https://github.com/${var.github_org}"
      # GitHub App auth is configured via the runner scale set's
      # githubConfigSecret — created separately via scripts/setup-secrets.sh
      maxRunners = 10
      minRunners = 0
      template = {
        spec = {
          serviceAccountName = "github-runner-sa"
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

  depends_on = [helm_release.arc_controller]
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
