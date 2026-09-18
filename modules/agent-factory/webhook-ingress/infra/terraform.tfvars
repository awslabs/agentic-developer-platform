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

# GitLab is not currently in use. Keep its unauthenticated API Gateway method,
# Lambda, and token-bearing worker path absent until the integration is
# explicitly configured and its security regression suite passes.
gitlab_webhook_enabled = false

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

# Issue #4272 (·A-3 Phase 1): route the agent run's GitHub token through the
# gateway gatekeeper (POST /internal/v1/github-installation-token) instead of
# minting it in-pod from the platform GitHub App private key. When true, the
# key is never read in the agent pod and GH_APP_PRIVATE_KEY is not exported —
# the gateway mints a token scoped to the run's own installation + assigned
# repo, after confirming the installation belongs to the run's tenant.
#
# Flipped on 2026-08-28, then REVERTED the same day after it took down all
# agent dispatch on dev. Root cause: the worker's broker client mints against
# ADP_GATEWAY_ENDPOINT, which is the PUBLIC API-Gateway edge URL. The gatekeeper
# route /internal/v1/github-installation-token is internal-only and the edge
# refuses it with HTTP 403 "Not available from the edge". Because the design has
# NO in-pod fallback ("fail the run loudly at bootstrap"), every worker died at
# boot before it could clone or comment — KEDA respawned them into the same
# crash. The pre-flip canary passed only because it hit the internal ALB via
# curl directly, not through the edge the worker actually uses.
#
# DO NOT re-enable until the worker broker client targets the in-cluster gateway
# service URL (e.g. http://bedrockgateway.adp-gateway.svc.cluster.local) for the
# /internal route, and an end-to-end WORKER run (not a curl canary) is verified
# to mint via the gatekeeper. Rollback is a webhook-ingress apply (this change);
# in-flight pods keep the setting they started with.
gh_token_broker_enabled = false

# Issue #4450 / design note #4559 §8: the nightly security agent's root-dispatch
# EventBridge rule, its target, and the service-identity row it fails closed
# without.
#
# The repo and org are set here so they are reviewed values rather than
# whatever a later ad-hoc apply passes (they are inert while the rule is off —
# every resource is `count = enable ? 1 : 0`).
#
# The enable flag stays FALSE in this committed file on purpose, and flipping it
# is the deliberate follow-up, not a merge side effect. This module
# AUTO-APPLIES on any push under infra/**, while the runner's
# `events:PutEvents` grant lands via a manual agent-factory-infra-apply. #4559
# §8 requires the IAM grant FIRST — the rule without it means the pipeline's
# first emit fails AccessDeniedException. Committing `true` here would invert
# that order on merge. So: run agent-factory-infra-apply.yml, then set this to
# true (or pass -var) and run webhook-ingress-deploy.yml.
#
# Nothing emits until someone dispatches the nightly workflow, so the rule being
# absent until then changes no behaviour.
enable_eventbridge_security_agent_rule = false
eventbridge_security_agent_repo        = "aws-e/adp"
eventbridge_security_agent_org         = "aws-e"
