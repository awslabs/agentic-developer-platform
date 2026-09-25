resource "kubernetes_service_account" "agent_workflow" {
  count = var.enable_agent_workflow_runner ? 1 : 0
  metadata {
    name        = "agent-workflow-sa"
    namespace   = var.runner_namespace
    annotations = { "eks.amazonaws.com/role-arn" = var.agent_workflow_role_arn }
  }
}
resource "helm_release" "agent_workflow" {
  count      = var.enable_agent_workflow_runner ? 1 : 0
  name       = "arc-runner-agent"
  namespace  = var.runner_namespace
  repository = "oci://ghcr.io/actions/actions-runner-controller-charts"
  chart      = "gha-runner-scale-set"
  version    = "0.13.1"
  values = [yamlencode({
    controllerServiceAccount = { namespace = var.controller_namespace, name = var.controller_service_account }
    githubConfigUrl          = var.github_repo != "" ? "https://github.com/${var.github_org}/${var.github_repo}" : "https://github.com/${var.github_org}"
    githubConfigSecret       = var.github_config_secret
    runnerScaleSetName       = "arc-runner-agent"
    minRunners               = 0
    maxRunners               = 5
    template = {
      metadata = { annotations = { "karpenter.sh/do-not-disrupt" = "true" } }
      spec = {
        serviceAccountName = kubernetes_service_account.agent_workflow[0].metadata[0].name
        containers = [{
          name    = "runner"
          image   = var.runner_image == "" ? "ghcr.io/actions/actions-runner:latest" : var.runner_image
          command = ["/home/runner/run.sh"]
          env     = [{ name = "GITHUB_ACTIONS_RUNNER_CHANNEL_TIMEOUT", value = "120" }]
          resources = {
            requests = { cpu = "4", memory = "4Gi" }
            limits   = { cpu = "4", memory = "8Gi" }
          }
        }]
    } }
  })]
  lifecycle {
    precondition {
      condition     = can(regex("^arn:aws:iam::[0-9]{12}:role/.+-agent-workflow$", var.agent_workflow_role_arn))
      error_message = "The agent pool must use its distinct agent-workflow role, never the shared runner role."
    }
  }
}
