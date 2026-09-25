# Dev-only webhook configuration. The shared wrapper loads this explicitly;
# other environments use portable defaults and their own optional overlay.

# The dev sandbox cannot reserve Lambda concurrency (#2928). Other targets
# retain variables.tf's reserved-concurrency throttle isolation by default.
enable_lambda_reserved_concurrency = false

# Dev OTel export to ADOT / CloudWatch / X-Ray (#1630); content unmasking is off.
enable_agent_otel = true

# Dev CI has the existing gateway internal-api-key secret. The CLI disables
# this for fresh dev deployments until the secret exists. Other environments
# retain the false module default unless explicitly configured.
enable_adversarial_e2e = true

# Reviewed dev dispatch identity, inert while the portable rule flag is false.
# Enabling the rule still requires the producer IAM grant first (#4450 / #4559).
eventbridge_security_agent_repo = "aws-e/adp"
eventbridge_security_agent_org  = "aws-e"

# The existing gateway role has reached AWS's aggregate inline-policy quota.
# These new authority grants use managed policies with identical permissions.
gateway_authority_managed_policies = true

# Saved persona preferences resolve before dispatch; worker authority stays independent.
persona_model_mapping_enabled = true

# Task API image source 029fdfe9d (validated model adapter, artifacts and child exit race); preserves the existing cyber worker runtime.
# Browser broker keeps its independently reviewed image below.
agent_image = "879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:57d938b2dbf37dee4d3dae4f6a5042cfc8d96c8376e9dfd373e145993494f132"

# The matching broker and worker support session-owner capabilities.
domain_app_images = {
  cyber-browser = "879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:073918cf6405bae0158957588eb6acb8c6f3485d04e08fb091659066827b4e24"
}

domain_app_settings = {
  cyber = {
    common_crawl_partitions = "CC-MAIN-2026-39,CC-MAIN-2026-34,CC-MAIN-2026-30,CC-MAIN-2026-25,CC-MAIN-2026-21,CC-MAIN-2026-17"
    session_owner_routing   = "true"
  }
}

# Task API T4 was OOMKilled at 8Gi during worker regression tests (2026-09-24).
# Reserve additional node capacity as well as raising the per-worker ceiling.
agent_worker_memory_request = "8Gi"
agent_worker_memory_limit   = "16Gi"

# Task-only workload proof uses prepared TokenReview RBAC; generic authority stays off.
agent_authority_prepared   = true
agent_authority_enabled    = false
task_api_worker_enabled    = true
task_api_admission_enabled = true
task_api_recovery_enabled  = true

# Existing ingress resolves canonical identities through the protected gateway.
gateway_api_url                 = "https://59o2rakc50.execute-api.us-east-1.amazonaws.com/dev"
internal_api_key_parameter_name = "/adp/dev/gateway/internal-api-key"
