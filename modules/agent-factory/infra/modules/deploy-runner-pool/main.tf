# =============================================================================
# Deployment runner pool — a second ARC scale set for deploy/apply workflows
# =============================================================================
# Why a second pool (2026-10-20): the org pool (`arc-runner-org`, modules/arc-runner)
# is shared by every CI shard and every deploy workflow. At 10:00 UTC all 40 org
# runners were Running Gateway CI shards and the tail jobs of gateway-deploy run
# 39393596812 (controller refresh, migrations, smoke) sat `queued` with no runner
# for 20+ minutes after the backend had already rolled out; 13 workflows had
# queued jobs at that moment. Raising the org ceiling (PR #8393) buys headroom,
# but any ceiling shared with test shards is eventually consumed by test shards.
# A pool that only deploy workflows target cannot be starved by them.
#
# Identity model is unchanged: pods run under the SAME service account as the org
# pool (`github-runner-sa`, IRSA to the shared runner role), so a job moved here
# keeps exactly the AWS and cluster access it had on `arc-runner-org`. This is
# deliberately NOT the trusted `arc-runner-deployment` design in
# platform/automation-infra/runner-values.yaml (own GitHub App, runner group
# `adp-deployment`, tainted `adp-trusted-deployment` NodePool, reviewed image
# digest). That design stays the long-term target; `node_selector` and
# `tolerations` below exist so this pool can move onto that NodePool without a
# new module when the trust pieces land.
#
# Like modules/agent-workflow-pool, this module references the already-installed
# controller, namespace and registration secret by name so a targeted additive
# apply (`-target=module.deploy_runner_pool`) never touches the live org pool or
# the shared IAM role.

resource "helm_release" "deploy" {
  name       = "arc-runner-deploy"
  namespace  = var.runner_namespace
  repository = "oci://ghcr.io/actions/actions-runner-controller-charts"
  chart      = "gha-runner-scale-set"
  version    = "0.14.2"

  values = [yamlencode({
    controllerServiceAccount = { namespace = var.controller_namespace, name = var.controller_service_account }
    githubConfigUrl          = var.github_repo != "" ? "https://github.com/${var.github_org}/${var.github_repo}" : "https://github.com/${var.github_org}"
    githubConfigSecret       = var.github_config_secret
    runnerScaleSetName       = "arc-runner-deploy"
    # One warm runner so the first job of a deploy never waits for a pod to
    # register (the same 80 s the org pool removed in #8372). Eight is the most
    # concurrent deploy jobs observed in a saturated hour: gateway-deploy fans
    # out to four tail jobs while two other module deploys run beside it.
    minRunners = var.min_runners
    maxRunners = var.max_runners
    template = merge(
      { metadata = { annotations = { "karpenter.sh/do-not-disrupt" = "true" } } },
      {
        spec = merge(
          {
            serviceAccountName = var.runner_service_account
            containers = [{
              name    = "runner"
              image   = var.runner_image == "" ? "ghcr.io/actions/actions-runner:latest" : var.runner_image
              command = ["/home/runner/run.sh"]
              env     = [{ name = "GITHUB_ACTIONS_RUNNER_CHANNEL_TIMEOUT", value = "120" }]
              # Deploy jobs run kubectl, terraform, aws and wait on CodeBuild and
              # rollouts; they are wait-bound, not test-shard CPU-bound. cpu=2 is
              # half the org pool's measured 3.7-core shard figure and still
              # keeps request == limit for CPU so a terraform plan cannot be
              # starved by a co-tenant mid-apply. Memory keeps the org pool's
              # proven 4Gi request / 8Gi limit (terraform providers + kubectl
              # manifests peak well below it).
              resources = {
                requests = { cpu = "2", memory = "4Gi" }
                limits   = { cpu = "2", memory = "8Gi" }
              }
            }]
          },
          length(var.node_selector) > 0 ? { nodeSelector = var.node_selector } : {},
          length(var.tolerations) > 0 ? { tolerations = var.tolerations } : {},
        )
      },
    )
  })]

  lifecycle {
    precondition {
      condition     = var.max_runners >= var.min_runners && var.min_runners >= 0
      error_message = "max_runners must be at least min_runners."
    }
  }
}
