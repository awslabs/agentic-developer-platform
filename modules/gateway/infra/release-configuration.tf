# Operator inputs retained privately across portable release upgrades.
# Image selection remains owned by the release deployment.
output "release_configuration" {
  description = "Account-local deployment settings for subsequent release upgrades."
  sensitive   = true
  value = {
    agent_context_ingestion_queue_arn       = var.agent_context_ingestion_queue_arn
    cognito_custom_domain                   = var.cognito_custom_domain
    cost_center                             = var.cost_center
    create_test_users                       = var.create_test_users
    enable_agent_context_sqs                = var.enable_agent_context_sqs
    enable_api_gateway                      = var.enable_api_gateway
    enable_chat_logging                     = var.enable_chat_logging
    enable_github_auth_broker               = var.enable_github_auth_broker
    enable_lambda_reserved_concurrency      = var.enable_lambda_reserved_concurrency
    enable_mantle_passthrough               = var.enable_mantle_passthrough
    enable_task_api_route                   = var.enable_task_api_route
    github_auth_allow_open_signup           = var.github_auth_allow_open_signup
    github_auth_allowed_orgs                = var.github_auth_allowed_orgs
    github_auth_allowlist_mode              = var.github_auth_allowlist_mode
    orchestration_agent_authority_enabled   = var.orchestration_agent_authority_enabled
    orchestration_dispatch_repo             = var.orchestration_dispatch_repo
    orchestration_engine_enabled            = var.orchestration_engine_enabled
    persona_model_mapping_enabled           = var.persona_model_mapping_enabled
    persona_model_probe_destination_enabled = var.persona_model_probe_destination_enabled
    persona_model_probe_external_id         = var.persona_model_probe_external_id
    rds_allocated_storage                   = var.rds_allocated_storage
    rds_instance_class                      = var.rds_instance_class
    redis_node_type                         = var.redis_node_type
    task_api_artifact_bucket_name           = var.task_api_artifact_bucket_name
    task_api_flags                          = var.task_api_flags
    task_api_lambda_function_name           = var.task_api_lambda_function_name
    task_api_lambda_invoke_arn              = var.task_api_lambda_invoke_arn
    task_api_prerequisites_enabled          = var.task_api_prerequisites_enabled
    task_api_runtime_bindings               = var.task_api_runtime_bindings
  }
}
