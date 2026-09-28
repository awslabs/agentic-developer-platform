variable "enable_agent_workflow_runner" {
  description = "Create the separate developer identity before installing its ARC pool and routing cutover"
  type        = bool
  default     = false
}
module "agent_workflow_iam" {
  count                  = var.enable_agent_workflow_runner ? 1 : 0
  source                 = "../../infra/modules/agent-workflow-iam"
  name_prefix            = var.project_name
  environment            = var.environment
  aws_region             = var.aws_region
  oidc_provider_arn      = aws_iam_openid_connect_provider.eks.arn
  oidc_issuer            = aws_iam_openid_connect_provider.eks.url
  runner_namespace       = "arc-runners"
  github_org             = var.github_org
  gateway_execution_arns = var.gateway_execution_arns
}
output "agent_workflow_runner" {
  value = try({
    role_arn          = module.agent_workflow_iam[0].role_arn
    service_account   = module.agent_workflow_iam[0].service_account
    runner_label      = module.agent_workflow_iam[0].runner_label
    namespace         = module.agent_workflow_iam[0].namespace
    github_config_url = "https://github.com/${var.github_org}"
  }, null)
}
