# Additive S14 prerequisite. Enable and verify the separate pool before routing
# the legacy label workflow to it; no shared runner trust/grants are changed.
variable "enable_agent_workflow_runner" {
  description = "Provision the bounded developer-workflow pool before its explicit routing cutover"
  type        = bool
  default     = false
}
module "agent_workflow_iam" {
  count                  = var.enable_agent_workflow_runner && var.enable_github_apps ? 1 : 0
  source                 = "./modules/agent-workflow-iam"
  name_prefix            = local.name_prefix
  environment            = var.environment
  aws_region             = var.aws_region
  oidc_provider_arn      = local.oidc_provider_arn
  oidc_issuer            = local.oidc_issuer
  runner_namespace       = var.runner_namespace
  github_org             = var.github_org
  gateway_execution_arns = local.runner_gateway_execution_arns
}

# This module references the already-installed controller/namespace/registration
# secret by name. It must not inherit arc_runner's module-level dependency on
# shared IAM: a targeted additive rollout must not replace that live role.
module "agent_workflow_pool" {
  count                        = var.enable_agent_workflow_runner && var.enable_github_apps ? 1 : 0
  source                       = "./modules/agent-workflow-pool"
  enable_agent_workflow_runner = true
  agent_workflow_role_arn      = module.agent_workflow_iam[0].role_arn
  runner_namespace             = var.runner_namespace
  github_org                   = var.github_org
  github_repo                  = var.github_repo
  runner_image                 = local.runner_image
}
