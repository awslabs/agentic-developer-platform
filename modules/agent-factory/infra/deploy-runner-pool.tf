# Dedicated deployment runner pool (`arc-runner-deploy`). Deploy/apply workflows
# target it so they never queue behind CI test shards on `arc-runner-org`; see
# modules/deploy-runner-pool/main.tf for the 2026-10-20 evidence. It references
# the controller, namespace, registration secret and service account the
# arc_runner module installs, by name, so a targeted additive apply never touches
# the live org pool or the shared runner role:
#
#   terraform plan  -target=module.deploy_runner_pool -var-file=terraform.tfvars -out=/tmp/deploy-pool.tfplan
#   terraform apply /tmp/deploy-pool.tfplan
#
# (self-managed from the dev box with environments/<env>/modules/agent-factory-backend.tfvars;
# the GitHub-dispatched apply has no deployment role for dev). Apply BEFORE merging
# a workflow `runs-on: arc-runner-deploy` change: GitHub has no runner fallback,
# so a label with no listener leaves jobs queued forever.
module "deploy_runner_pool" {
  count            = var.enable_github_apps ? 1 : 0
  source           = "./modules/deploy-runner-pool"
  runner_namespace = var.runner_namespace
  github_org       = var.github_org
  github_repo      = var.github_repo
  runner_image     = local.runner_image

  # The org pool must exist first: this release registers with its secret and
  # runs under its service account.
  depends_on = [module.arc_runner]
}

output "deploy_runner_label" {
  description = "runs-on label of the dedicated deployment pool (empty when GitHub Apps are disabled)."
  value       = var.enable_github_apps ? module.deploy_runner_pool[0].runs_on_label : ""
}
