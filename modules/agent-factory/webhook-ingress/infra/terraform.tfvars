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

# Issue #3436 / GitLab Phase 0 (#3320): keep the GitLab webhook route + Lambda
# deployed. This was enabled ad-hoc during the Phase 0 delivery loop but never
# committed, so subsequent applies (default=false) destroyed /gitlab and its
# Lambda — silently breaking GitLab dispatch. dev == embark1 spike environment;
# envs without a GitLab instance override this to false in their own tfvars.
gitlab_webhook_enabled = true

# Issue #3488 / #3494: adversarial E2E infra gating (dual-path design).
#
# CI path (webhook-ingress-deploy.yml) uses `-var-file=terraform.tfvars`, so
# this value governs existing environments where the Secrets Manager secret
# adp/<env>/gateway/internal-api-key already exists. Set TRUE here.
#
# Fresh-deploy path (deploy-webhook-ingress.sh) does NOT pass -var-file, so
# it picks up `default = false` from variables.tf — safe on new accounts
# where CI hasn't seeded the secret yet.
enable_adversarial_e2e = true
