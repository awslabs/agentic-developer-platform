# Portable gateway defaults; no platform-account identities or activation receipts.
cost_center                        = "engineering"
rds_instance_class                 = "db.t3.medium"
rds_allocated_storage              = 20
redis_node_type                    = "cache.t3.micro"
cognito_custom_domain              = ""
enable_api_gateway                 = true
create_test_users                  = true
enable_github_auth_broker          = true
github_auth_allowlist_mode         = "platform"
github_auth_allow_open_signup      = false
github_auth_allowed_orgs           = ""
enable_chat_logging                = true
enable_agent_context_sqs           = false
agent_context_ingestion_queue_arn  = ""
enable_mantle_passthrough          = true
enable_lambda_reserved_concurrency = false
persona_model_mapping_enabled      = true
# Task API, protected authority and model probes retain module defaults until
# configured and qualified in this account. Never copy another account's proof.
