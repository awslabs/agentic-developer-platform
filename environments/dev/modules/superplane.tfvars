# Superplane control-plane variables — dev. Issue #5042 (U3), EPIC #4910.
#
# WHY THIS FILE EXISTS NOW
#
# `deploy-all.sh` Step 12/12 and `undeploy-phases.sh` phase_superplane both pass
# `-var-file=environments/<env>/modules/superplane.tfvars`. U1 pre-wired both to activate the
# moment `infra/control-plane/*.tf` lands, so without this file the very first deploy that
# reached the superplane phase would fail on a missing var-file — after applying everything
# before it.
#
# ACCOUNT_ID IS A PLACEHOLDER, NOT A VALUE
#
# `platform/scripts/bootstrap.sh` rewrites `ACCOUNT_ID` across every
# `environments/**/*.tfvars` with the account the operator is authenticated to
# (`aws sts get-caller-identity`), and `deploy.sh` does the same. Same convention as
# `modules/domain-apps/cyber/infra/terraform.tfvars`.
#
# The literal `ACCOUNT_ID` deliberately does NOT satisfy `var.account_id`'s 12-digit
# validation. An unsubstituted placeholder therefore stops `terraform plan` with a clear
# error instead of planning against something wrong — which is the behaviour acceptance
# criterion 3 wants: no deploy proceeds against an account nobody chose. The environment
# and account for this module are formally UNRESOLVED (see the issue's "Environment
# coverage" note), and inventing one here is exactly what that note forbids.
environment = "dev"
aws_region  = "us-east-1"
account_id  = "ACCOUNT_ID"

# ---------------------------------------------------------------------------
# CORS (acceptance criterion 4)
#
# An explicit allowlist. Upstream defaults this to `["*"]` while its API sets
# `allow_credentials=True`, which lets any origin make credentialed calls — the defect this
# criterion exists to block, and the module's validation rejects that pairing outright.
#
# The origin below is an internal placeholder and MUST be replaced with the real domain
# before the API is exposed. Note which way that fails: a wrong-but-specific origin fails
# CLOSED — browsers reject the cross-origin call and the operator sees a visible CORS error.
# A wildcard would fail OPEN and silently work, which is why an unresolved hostname is not a
# reason to relax the allowlist while the domain is being decided.
# ---------------------------------------------------------------------------
cors_allowed_origins   = ["https://superplane.dev.adp.internal"]
cors_allow_credentials = true

# ---------------------------------------------------------------------------
# Secret REFERENCES, never values (acceptance criterion 3)
#
# Names of Secrets Manager secrets the operator seeds out of band. The module resolves them
# at runtime through its scoped IRSA role, so no credential enters Terraform state, a plan
# output, a CI log or a manifest. Upstream ships an inline `DATABASE_URL` password and a
# committed JWT placeholder; both are applied artifacts, and neither shape is representable
# here — the variables reject anything value-shaped.
#
# DECISION 2 IS UNRESOLVED. Taking the database as a reference is what keeps this file
# neutral on shared-instance vs. separate-instance. This module provisions no database and
# claims NO isolation, backup, retention or restore property about whatever the secret
# points at.
# ---------------------------------------------------------------------------
database_secret_name = "adp/dev/superplane/database"
jwt_secret_name      = "adp/dev/superplane/jwt-signing-key"

# ---------------------------------------------------------------------------
# Kubernetes identity. Domain-owned namespaces; the module rejects core ADP namespaces.
# `skypilot` is pinned by U2's lock (skypilot_config.namespace).
# ---------------------------------------------------------------------------
namespace          = "superplane"
skypilot_namespace = "skypilot"

# ---------------------------------------------------------------------------
# GPU workspace target — EMPTY ON PURPOSE.
#
# "GPU workloads target the intended workspace cluster, not the ADP management cluster"
# (platform isolation requirement, 2026-09-16). SkyPilot schedules through a Kubernetes
# context, so a context naming the management cluster would put GPU jobs on it. Empty means
# "no Kubernetes workspace target configured" and is published as the explicit sentinel
# `none`, so nothing can read it as "unset, therefore use the current cluster".
#
# Set this only when a workspace cluster exists and is the intended GPU target. U19 owns the
# SkyPilot resource/state handover; a live GPU run is not authorized by this story.
# ---------------------------------------------------------------------------
workspace_cluster_context = ""

# SkyPilot's compute-launching IAM grant. Empty by design — see irsa.tf: the permission set
# is broad, the target account is unresolved, and the acceptance that would validate it is
# deferred. Inventing a policy here would either over-provision in an account nobody named
# or under-provision and surface as launch failures that look like SkyPilot bugs.
skypilot_compute_policy_arns = []

cost_center = "engineering"
