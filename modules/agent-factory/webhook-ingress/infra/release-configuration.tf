# Operator inputs retained privately across portable release upgrades.
# Image selection remains owned by the release deployment.
output "release_configuration" {
  description = "Account-local deployment settings for subsequent release upgrades."
  sensitive   = true
  value = {
    agent_authority_enabled                = var.agent_authority_enabled
    agent_authority_legacy_workers_drained = var.agent_authority_legacy_workers_drained
    agent_authority_prepared               = var.agent_authority_prepared
    agent_authority_runtime_ready          = var.agent_authority_runtime_ready
    agent_authority_worker_image_digests   = var.agent_authority_worker_image_digests
    agent_control_enabled                  = var.agent_control_enabled
    agent_explanations_enabled             = var.agent_explanations_enabled
    agent_legacy_worker_admin_retired      = var.agent_legacy_worker_admin_retired
    agent_pod_deadline_seconds             = var.agent_pod_deadline_seconds
    agent_task_source_isolation_confirmed  = var.agent_task_source_isolation_confirmed
    agent_worker_admission_paused          = var.agent_worker_admission_paused
    agent_worker_memory_limit              = var.agent_worker_memory_limit
    agent_worker_memory_request            = var.agent_worker_memory_request
    codex_github_personas                  = var.codex_github_personas
    codex_reviewer_model                   = var.codex_reviewer_model
    codex_task_personas                    = var.codex_task_personas
    enable_adversarial_e2e                 = var.enable_adversarial_e2e
    enable_agent_otel                      = var.enable_agent_otel
    enable_lambda_reserved_concurrency     = var.enable_lambda_reserved_concurrency
    eventbridge_security_agent_org         = var.eventbridge_security_agent_org
    eventbridge_security_agent_repo        = var.eventbridge_security_agent_repo
    gateway_api_url                        = var.gateway_api_url
    gateway_authority_managed_policies     = var.gateway_authority_managed_policies
    internal_api_key_parameter_name        = var.internal_api_key_parameter_name
    persona_model_mapping_enabled          = var.persona_model_mapping_enabled
    shared_run_reporting_enabled           = var.shared_run_reporting_enabled
    shared_worker_continuation_enabled     = var.shared_worker_continuation_enabled
    task_api_admission_enabled             = var.task_api_admission_enabled
    task_api_human_enabled                 = var.task_api_human_enabled
    task_api_recovery_enabled              = var.task_api_recovery_enabled
    task_api_worker_enabled                = var.task_api_worker_enabled
  }
}
