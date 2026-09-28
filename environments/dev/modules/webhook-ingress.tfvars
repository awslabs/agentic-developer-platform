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

# Protected worker source f1f776f59fd3be3ba9956d90c646a7d39ce1af02; reviewed Codex deadline and refusal reporting.
agent_image = "879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:cdde81fb9cf747676942136c2e68de51bf7e3bb6dd9bbeefee613abcaac69d1e"

# Task API T4 was OOMKilled at 8Gi during worker regression tests (2026-09-24).
# Reserve additional node capacity as well as raising the per-worker ceiling.
agent_worker_memory_request = "8Gi"
agent_worker_memory_limit   = "16Gi"

# Protected workers use workload proof and gateway run services.
agent_authority_prepared   = true
agent_authority_enabled    = true
task_api_worker_enabled    = true
task_api_admission_enabled = true
task_api_human_enabled     = true
task_api_recovery_enabled  = true

# Existing ingress resolves canonical identities through the protected gateway.
gateway_api_url                 = "https://59o2rakc50.execute-api.us-east-1.amazonaws.com/dev"
internal_api_key_parameter_name = "/adp/dev/gateway/internal-api-key"

# Controlled rollout: legacy workers drained; customer source has no platform EKS access.
agent_authority_runtime_ready          = true
agent_authority_legacy_workers_drained = true
agent_task_source_isolation_confirmed  = true
agent_legacy_worker_admin_retired      = true
agent_worker_admission_paused          = false
shared_run_reporting_enabled           = true
shared_worker_continuation_enabled     = false
agent_authority_worker_image_digests   = ["sha256:1cb3550ee64d72b3b5261ccba7c874378a309d71ca277cb985dc4c911edce51f", "sha256:2923326ff83e0335cbf9e17a0c0f80b6c2ab0b01c70c76fac9fd059851b6eb94", "sha256:3d1275bea78f6b400ddc7402822abf4785c2be284b7ffcc9d6d47e7710522010", "sha256:3d19d3fba77538bba11d57c9ef05026e54b70bf465515bca42746f3534cf96e4", "sha256:5d3e952c1be21b1a2656b783fbebf0d653893469d9bc17e7f0ca623cef3f8572", "sha256:6beab1ad04b9249aa72b2f25bf6b8bbfc3f8ecfc82eac250ac2eb8e083819d67", "sha256:7c1541d515717d54da677797ebd37cac67b39f3e8de116cd6e391f0de08b201d", "sha256:8a3af964d0a786a6c27b5e66d44ac32e8e451c23d375046663936d2007658db1", "sha256:a396eeae0ebd032866b33550a876aeae7a317d0bce00163608d790b4bafc55c3", "sha256:beae9a4b9e6c6ebd4f442eebf2a65965d077b072a76baa0563caa689289979d4", "sha256:cdde81fb9cf747676942136c2e68de51bf7e3bb6dd9bbeefee613abcaac69d1e", "sha256:cf0678b1bef6f1562eab17a06a12568081350b6eac7f187bd1df6b2166ee95cf", "sha256:e25a54c3037f9bde26a3aa400e3af9bf367288e7a6977cece2d508f5f259fe27", "sha256:f14998af44cc77df24365371675ffe7fef54878d7a254c83e65a9dcbcbfdbabe", "sha256:f90e802c40b20edffd7ae7ccaa7ba6af1072158e7ae349535d6a1728e2bfa63d"]

# Six-hour Task deadline plus startup/cleanup headroom for the owning pod.
agent_pod_deadline_seconds = 22200

# Task tools enabled for qualified file analysis and URL investigation.
task_persona_tools = {
  agent-task-cyber = ["cyber.triage", "cyber.static", "cyber.result", "cyber.common_crawl_scan", "cyber.common_crawl_result", "cyber.common_crawl_read", "cyber.browser_start", "cyber.browser_step", "cyber.browser_inspect", "cyber.browser_close"]
}

# Independent tool service; protected workers receive only this exact route.
task_tool_invoke_resources = [
  "arn:aws:execute-api:us-east-1:879318057152:59o2rakc50/dev/POST/tools/cyber",
  "arn:aws:execute-api:us-east-1:879318057152:59o2rakc50/dev/POST/tools/cyber/common-crawl"
]

# Authenticated Agent Activity explanation stream; independent of mutation controls.
agent_explanations_enabled = true
# Explicit operator activation on 2026-09-25; acceptance evidence remains tracked separately.
agent_control_enabled = true
