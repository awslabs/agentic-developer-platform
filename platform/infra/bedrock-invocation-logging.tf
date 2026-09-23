# EPIC #4997: provider request/response logging is a regional account singleton.
# Own it once in the shared platform, not in each gateway or agent stack.
module "bedrock_invocation_logging" {
  automation_permissions_boundary_arn = var.automation_permissions_boundary_arn
  source                              = "./modules/bedrock-invocation-logging"
  count                               = var.manage_bedrock_invocation_logging ? 1 : 0

  name_prefix       = local.name_prefix
  common_tags       = local.common_tags
  enabled           = var.bedrock_invocation_logging_enabled
  retention_in_days = var.bedrock_invocation_log_retention_days
}

output "bedrock_invocation_logging" {
  description = "Account/region Bedrock invocation logging destinations; null when owned outside this platform state"
  value = var.manage_bedrock_invocation_logging ? {
    enabled        = module.bedrock_invocation_logging[0].enabled
    region         = module.bedrock_invocation_logging[0].region
    log_group_name = module.bedrock_invocation_logging[0].log_group_name
    bucket_name    = module.bedrock_invocation_logging[0].bucket_name
    key_arn        = module.bedrock_invocation_logging[0].key_arn
  } : null
}
