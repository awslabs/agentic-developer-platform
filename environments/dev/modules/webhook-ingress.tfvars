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

# Common Crawl-first cyber investigations (#5842), preserving deployed worker source.
# Worker and browser broker share this digest; protected-worker migration stays off.
agent_image = "879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:6ee9ad36ba31666872a6d653d1a3814a2f8308c99b8ac36778239dc88c7aa204"

# The matching broker and worker support session-owner capabilities.
domain_app_images = {
  cyber-browser = "879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:6ee9ad36ba31666872a6d653d1a3814a2f8308c99b8ac36778239dc88c7aa204"
}

domain_app_settings = {
  cyber = {
    common_crawl_partitions = "CC-MAIN-2026-39,CC-MAIN-2026-34,CC-MAIN-2026-30"
    session_owner_routing   = "true"
  }
}
