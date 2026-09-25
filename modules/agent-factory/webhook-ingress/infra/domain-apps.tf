# Platform composition only; implementation and release settings belong to the app.
module "cyber" {
  source                  = "../../../domain-apps/cyber/infra/platform-integration"
  name_prefix             = local.name_prefix
  aws_region              = var.aws_region
  environment             = var.environment
  account_id              = local.account_id
  namespace               = kubernetes_namespace.adp_agents.metadata[0].name
  oidc_provider_arn       = local.oidc_provider_arn
  oidc_issuer             = local.oidc_issuer
  worker_role_name        = aws_iam_role.agent_scaledjob.id
  broker_image            = lookup(var.domain_app_images, "cyber-browser", "")
  common_crawl_partitions = compact(split(",", lookup(lookup(var.domain_app_settings, "cyber", {}), "common_crawl_partitions", "")))
  task_url_tools_enabled  = lookup(lookup(var.domain_app_settings, "cyber", {}), "task_url_tools_enabled", "false") == "true"
  tools_endpoint          = lookup(lookup(var.domain_app_settings, "cyber", {}), "tools_endpoint", "")
  browser_mode            = lookup(lookup(var.domain_app_settings, "cyber", {}), "browser_mode", "broker")
  browser_broker_enabled  = lookup(lookup(var.domain_app_settings, "cyber", {}), "browser_broker_enabled", "true") == "true"
  session_owner_routing   = lookup(lookup(var.domain_app_settings, "cyber", {}), "session_owner_routing", "false") == "true"
}

locals {
  domain_worker_environment        = module.cyber.worker_environment
  domain_worker_artifact_resources = module.cyber.worker_artifact_resources
  domain_worker_egress             = module.cyber.worker_egress
}

# Retain these moves for existing installations. Resource names, identities,
# namespace and compatibility image remain unchanged; no state push/import is needed.

moved {
  from = aws_iam_role_policy.agent_scaledjob_browser_deny
  to   = module.cyber.aws_iam_role_policy.agent_scaledjob_browser_deny
}

moved {
  from = aws_iam_policy.url_analysis_browser_broker_boundary
  to   = module.cyber.aws_iam_policy.url_analysis_browser_broker_boundary
}

moved {
  from = aws_iam_role.url_analysis_browser_broker
  to   = module.cyber.aws_iam_role.url_analysis_browser_broker
}

moved {
  from = aws_iam_role_policy.url_analysis_browser_broker
  to   = module.cyber.aws_iam_role_policy.url_analysis_browser_broker
}

moved {
  from = kubernetes_service_account.url_analysis_browser_broker
  to   = module.cyber.kubernetes_service_account.url_analysis_browser_broker
}

moved {
  from = kubernetes_deployment.url_analysis_browser_broker
  to   = module.cyber.kubernetes_deployment.url_analysis_browser_broker
}

moved {
  from = kubernetes_service.url_analysis_browser_broker
  to   = module.cyber.kubernetes_service.url_analysis_browser_broker
}

moved {
  from = kubernetes_network_policy.url_analysis_browser_broker
  to   = module.cyber.kubernetes_network_policy.url_analysis_browser_broker
}
