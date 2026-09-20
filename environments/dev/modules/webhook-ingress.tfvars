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
