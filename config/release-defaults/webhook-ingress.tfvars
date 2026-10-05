# Portable webhook settings. Runtime URLs, identities and images are discovered
# from the selected account by the deployment scripts, not copied from dev.
enable_lambda_reserved_concurrency = false
enable_agent_otel                  = true
persona_model_mapping_enabled      = true
# The full gateway exceeds IAM's aggregate inline-policy limit with these grants.
# Use the existing equivalent managed policies on portable installations.
gateway_authority_managed_policies = true
