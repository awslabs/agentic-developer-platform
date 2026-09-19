# Portable webhook module inputs. Terraform auto-loads this file, including
# callers that do not pass -var-file. Keep environment-specific values in
# environments/<environment>/modules/webhook-ingress.tfvars (or .tfvars.json).
# The shared wrapper requires an explicit environment and region and loads only
# that environment's overlay. Defaults are declared in variables.tf.

# GitLab is not currently in use. Keep its unauthenticated API Gateway method,
# Lambda, and token-bearing worker path absent until the integration is
# explicitly configured and its security regression suite passes.
gitlab_webhook_enabled = false

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

# The nightly security rule remains off until its producer IAM and measured
# delivery window are qualified (#4450 / #4559). Repository/org belong to the
# selected environment overlay. The deployment hold also applies to overlays.
enable_eventbridge_security_agent_rule = false
