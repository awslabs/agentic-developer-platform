# Default variable values for the webhook-ingress module.
# All variables in variables.tf have sensible defaults; this file exists so
# the CI's `terraform plan -var-file=terraform.tfvars` step succeeds.
# Override per-env via a separate tfvars file passed to `apply` when needed.

environment = "dev"
aws_region  = "us-east-1"

# Issue #2928: disable reserved concurrency in dev — the SCP-locked sandbox
# account (979157915401) has its unreserved pool pinned at the 100 floor and
# reserving even 1 unit triggers PutFunctionConcurrency failure. Same class
# as #2910 (gateway). The variables.tf default stays true, so prod (and any
# env with headroom) keeps reserved-concurrency throttle isolation.
enable_lambda_reserved_concurrency = false

# Issue #1630: enable Claude Agent SDK OTel telemetry → ADOT Collector →
# CloudWatch (logs + metrics) + X-Ray (traces). Deploys the collector in
# adp-agents and adds OTEL_* env to the agent-worker ScaledJob. Non-blocking;
# content unmasking (prompts/tool I/O) stays OFF (deferred data-governance).
enable_agent_otel = true

# Provider-level default_tags (in main.tf) now supply Project/Module/ManagedBy/
# Owner/CostCenter for every resource. The overlapping `tags` map that used to
# live here re-declared those keys and fought default_tags, so it was removed
# (#888). Resource-level `merge(var.tags, {...})` callers still work — var.tags
# defaults to {} in variables.tf.
