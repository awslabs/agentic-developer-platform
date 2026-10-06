# Composition only: Superplane owns the deployment definitions and permissions.
# Retain the existing automation backend and public opt-in input.
variable "enable_skypilot_deployment" {
  type    = bool
  default = false
}
module "superplane_skypilot_deployment" {
  source                     = "../../modules/domain-apps/superplane/infra/automation/skypilot-deployment"
  enable_skypilot_deployment = var.enable_skypilot_deployment
  name_prefix                = var.name_prefix
  repository                 = var.repository
  environment                = var.environment
  aws_region                 = var.aws_region
  account_id                 = data.aws_caller_identity.current.account_id
  cluster_name               = var.cluster_name
  github_oidc_provider_arn   = data.aws_iam_openid_connect_provider.github.arn
}

# Same state, stable AWS identities; retain these mappings for existing installs.
moved {
  from = aws_iam_role.skypilot_deployment
  to   = module.superplane_skypilot_deployment.aws_iam_role.skypilot_deployment
}
moved {
  from = aws_iam_role_policy.skypilot_deployment
  to   = module.superplane_skypilot_deployment.aws_iam_role_policy.skypilot_deployment
}
moved {
  from = aws_eks_access_entry.skypilot_deployment
  to   = module.superplane_skypilot_deployment.aws_eks_access_entry.skypilot_deployment
}
moved {
  from = aws_eks_access_policy_association.skypilot_deployment
  to   = module.superplane_skypilot_deployment.aws_eks_access_policy_association.skypilot_deployment
}
