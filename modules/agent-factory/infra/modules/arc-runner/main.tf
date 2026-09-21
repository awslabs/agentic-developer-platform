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
  version    = "0.13.1"

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
  version    = "0.13.1"

  values = [
    yamlencode({
      githubConfigUrl    = var.github_repo != "" ? "https://github.com/${var.github_org}/${var.github_repo}" : "https://github.com/${var.github_org}"
      githubConfigSecret = kubernetes_secret.arc_runner.metadata[0].name
      # 20: deploy + security-scan + agent runs contend for the pool; at 10 the
      # deploy pipeline sat queued behind Security Scan bursts (live-patched
      # 2026-07-03, codified here so the next apply doesn't revert it).
      maxRunners = 20
      minRunners = 0
      # Pod template. Always supply the full container spec (image, command,
      # resources) — the chart has no image-only override and overriding
      # `containers` without setting `command` would make pods run the
      # image's ENTRYPOINT and exit immediately. Default image is the
      # upstream actions-runner when `runner_image` is empty.
      template = {
        metadata = {
          annotations = { "karpenter.sh/do-not-disrupt" = "true" }
        }
        # Pod-level resource requests/limits. Without requests, Karpenter
        # packs multiple runners onto a single c6a.large; their concurrent
        # npm ci / setup-node bursts saturate the node's gp3 EBS IOPS
        # baseline (3000), stalling processes in D-state and causing 5+ min
        # "hangs". Requests push Karpenter to right-size the node, limits
        # prevent one runner starving others.
        #
        # Sizing history — two data-driven revisions, keep both in mind:
        #
        # 1) 2026-07: memory request lowered 4Gi -> 1Gi because observed
        #    steady-state usage was ~16-140Mi (Container Insights, 2h window)
        #    and 4Gi phantom reservation inflated node count. cpu=1 request
        #    kept as the density guard (≈ one runner per vCPU) against the
        #    EBS-IOPS hang above.
        #
        # 2) 2026-08-30: that 2h window turned out to miss the heavy jobs.
        #    Measured under real CI load: one runner at 3.7 cores, another at
        #    2.7Gi; three runners packed on one node drove it to 104% CPU and
        #    a second node to 97% memory, and a job died with "runner lost
        #    communication ... starves it for CPU/Memory" (PR #4476 npm
        #    audit). Requests raised to cpu=2 / memory=4Gi so scheduling
        #    reflects real burst usage: density drops to ~2 runners per
        #    4-vCPU node by CPU and memory overcommit is bounded at 2x
        #    (limit 8Gi vs 4Gi request) instead of 8x. This STRENGTHENS the
        #    IOPS density guard — do not lower either request below this
        #    without node-level CPU/memory data over a window that includes
        #    heavy workflows (full pytest suites, security scans, npm audit).
        #
        # 3) 2026-09-21: cpu request 2 -> 4, matching the limit. Gateway CI was
        #    sharded 4 ways with `pytest -n 4` (#5550), so one Gateway CI run is
        #    now FOUR runners that each genuinely want 4 cores, and runs overlap
        #    routinely (3 concurrent observed on 09-21). At a cpu=2 request the
        #    scheduler placed twice as many runners as the node could actually
        #    feed — the same overbooking revision 2 diagnosed, but now hit on
        #    every gateway PR rather than occasionally. Symptoms measured on
        #    identical code and config: shard throughput fell from 18.2 to 10.4
        #    tests/s between a quiet and a busy cluster (1.75x), node CPU peaked
        #    at 94%, and a timing-sensitive test
        #    (tests/budget/test_pricing_read_timeout.py) flaked under contention.
        #    Revision 2 already measured a runner at 3.7 cores, so cpu=4 is the
        #    honest figure, not a guess.
        #
        #    request == limit for CPU is deliberate: it removes CPU
        #    oversubscription entirely rather than bounding it, so a runner
        #    cannot be starved by a co-tenant mid-test. Density halves (~8
        #    runners per 32-vCPU node instead of ~16), which again STRENGTHENS
        #    the IOPS density guard above. The cost is real: Karpenter will
        #    provision more nodes instead of packing, so this trades AWS spend
        #    for developer wall clock. Do not revert it to buy density back
        #    without first re-measuring shard throughput on a BUSY cluster —
        #    a quiet-cluster measurement will show no difference and will
        #    mislead you.
        #
        # Memory is unchanged: revision 2 measured a peak of 2.7Gi against the
        # 4Gi request, so that one is already honest. Limits stay 4 CPU / 8Gi.
        spec = {
          serviceAccountName = kubernetes_service_account.runner.metadata[0].name
          containers = [
            {
              name    = "runner"
              image   = var.runner_image == "" ? "ghcr.io/actions/actions-runner:latest" : var.runner_image
              command = ["/home/runner/run.sh"]
              resources = {
                requests = { cpu = "4", memory = "4Gi" }
                limits   = { cpu = "4", memory = "8Gi" }
              }
            }
          ]
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
