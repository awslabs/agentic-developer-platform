# =============================================================================
# Superplane control-plane inputs — Issue #5042 (U3), EPIC #4910.
# =============================================================================
# The `validation` blocks in this file are the enforcement mechanism for acceptance
# criteria 3 and 4. They are here, on the variables, rather than in a lint script,
# because a variable validation fails during `terraform plan` — before anything is
# created, in the plan lane, with no AWS credentials needed. A grep-based CI check can be
# skipped by a path filter or by adding a file the glob does not match; a validation on
# the input the module cannot run without, cannot.
# =============================================================================

variable "environment" {
  type        = string
  description = "Environment name. Also selects the state key (<environment>/modules/superplane/terraform.tfstate)."
  default     = "dev"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,15}$", var.environment))
    error_message = "environment must be lowercase alphanumeric with hyphens (it becomes part of resource names and the state key)."
  }
}

variable "aws_region" {
  type        = string
  description = "AWS region for the control plane."
  default     = "us-east-1"
}

# ---------------------------------------------------------------------------
# THE PROVENANCE RULE (acceptance criterion 3)
#
# This variable has NO DEFAULT, and that absence is the entire point.
#
# The rule is provenance, not value class. An account id is not a secret (design §7:
# "Account IDs are non-secret metadata but still access-controlled"), and an account
# deliberately selected for a deployment is a legitimate tfvars input. What is forbidden
# is INHERITING the upstream snapshot's accounts as a default — because a default is
# what makes a deploy target an account nobody chose.
#
# THREE HARDCODED SITES, TWO DISTINCT VALUES — and the difference matters.
#
# The planning analysis counts "three hardcoded account IDs" in the snapshot. That is
# three *sites*, not three values: the second and third are the same account.
#
#   605440105851  upstream's ECR registry and state bucket — ci-controller.yml,
#                 ci-platform-monitor.yml, infra/skypilot-api/config.env
#                 (AWS_ACCOUNT_ID), and a hardcoded state backend
#                 `bucket = "superplane-terraform-state-605440105851"` with no variable
#   938500344975  upstream's test cluster — site 2 is
#                 `deploy/db-seed-job.yaml` (an EKS cluster ARN in a *different* account
#                 from the one used elsewhere); site 3 is
#                 `infra/skypilot-api/README.md`
#                 (ASSUME_ROLE_ARN=...:role/OrganizationAccountAccessRole)
#
# So the deny list below has TWO entries, and that is deliberate rather than an omission.
#
# A THIRD VALUE WAS CHECKED AND DELIBERATELY NOT BLOCKED. 193832579677 appears in the
# snapshot too, which makes it look like a third upstream account — but it is ADP's OWN
# account. The snapshot's `tmp/agent-*.yml` are copies of ADP's workflows, and the same
# literal is live on main today in .github/workflows/agent-developer.yml and eight
# siblings as `${{ vars.BEADS_S3_BUCKET || 'adp-beads-state-193832579677' }}`. Blocking
# it would reject a legitimate ADP account for looking like it came from the snapshot,
# which is value-class reasoning — precisely the error the provenance rule exists to
# avoid. Presence in the snapshot is not the test; ownership is.
#
# Note the shape of the real hazard: `X || 'literal'`, a fallback that works silently
# until the repo variable is unset and then targets upstream's account instead of
# failing. Reproducing that here would be the defect. So the variable is required (no
# default -> `terraform plan` stops and asks), and the validation rejects those two
# specific values even if supplied explicitly, since a deliberate choice of upstream's
# account is still upstream's account.
#
# Any OTHER 12-digit account id passes. That is the provenance distinction, and
# tests/no_inherited_defaults.tftest.hcl asserts all three halves: the two upstream
# accounts are rejected, a deliberately supplied account passes, and ADP's own
# beads-bucket account passes.
# ---------------------------------------------------------------------------
variable "account_id" {
  type        = string
  description = <<-EOT
    The ADP-owned AWS account this environment deploys into. REQUIRED — there is
    deliberately no default, so a deploy must name its target account explicitly rather
    than inherit one. Must not be an upstream snapshot account.
  EOT

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be a 12-digit AWS account id."
  }

  validation {
    # Two entries, not three — see the comment above. 193832579677 is ADP's own account
    # and is deliberately absent from this list.
    condition = !contains(
      ["605440105851", "938500344975"],
      var.account_id
    )
    error_message = <<-EOT
      account_id is an upstream AISuperPlane snapshot account (605440105851 = upstream
      ECR/state bucket, 938500344975 = upstream test cluster). These are not ADP
      accounts. Supply the ADP-owned account for this environment. See variables.tf for
      why this is blocked.
    EOT
  }
}

# ---------------------------------------------------------------------------
# CORS (acceptance criterion 4)
#
# The app used to ship `cors_origins: list[str] = ["*"]` (src/superplane-api/app/config.py)
# while `app/main.py` added CORSMiddleware with `allow_credentials=True`. That pairing lets
# any origin make credentialed calls against the domain API.
#
# As of issue #5682 (A02) the app-side default is `[]` and `resolve_cors_origins()` refuses
# "*" at startup, so the dangerous default is no longer inheritable from the image. This
# allowlist is KEPT rather than relaxed: it catches the wildcard at plan time, before a
# deploy that would otherwise fail on startup, and it is the layer that survives someone
# reintroducing a permissive app default later.
#
# Starlette reflects arbitrary requesting origins on credentialed wildcard
# preflights. Explicit origins keep browser policy bounded. This is separate from
# authentication: a successful preflight neither supplies a bearer token nor proves
# that a site can read an authenticated response.
#
# So: an explicit allowlist, and `*` is rejected as a MEMBER — not merely as the whole
# value, because ["https://legit.example", "*"] is exactly as permissive as ["*"] and far
# likelier to survive review.
#
# WHY BOTH RULES LIVE ON `cors_allowed_origins`
#
# The natural-looking arrangement is to put the credentials-pairing rule on
# `cors_allow_credentials`, since that is the variable that makes the wildcard dangerous.
# It does not work, and the failure is silent: Terraform SKIPS a validation whose
# referenced variable is already invalid, so the origin-format rule below (which rejects
# `*` as not scheme-qualified) always fires first and the pairing rule never evaluates.
# Verified during development — a test asserting the pairing rule fires reported "the
# checkable object was expected to report an error but did not", i.e. the rule was
# unreachable dead code that read as enforcement.
#
# So the pairing rule is declared HERE, on the variable whose value carries the wildcard.
# `cors_allow_credentials` is readable from this block, and this block always evaluates.
# tests/cors_allowlist.tftest.hcl pins the resulting behaviour, including the case where
# credentials are off — a wildcard with no credentials is a genuinely different (and
# defensible) security model, so it is not conflated with the dangerous pairing.
# ---------------------------------------------------------------------------
variable "cors_allowed_origins" {
  type        = list(string)
  description = <<-EOT
    Explicit origin allowlist for the domain API. Must never contain "*" while
    cors_allow_credentials is true. No default: an allowlist that defaults to something
    is an allowlist somebody forgot to write.
  EOT

  validation {
    condition     = length(var.cors_allowed_origins) > 0
    error_message = "cors_allowed_origins must list at least one origin explicitly."
  }

  # The dangerous pairing, checked first so its specific message is what an operator sees
  # when they inherit upstream's default rather than the generic format complaint.
  validation {
    condition = !(
      var.cors_allow_credentials && contains(var.cors_allowed_origins, "*")
    )
    error_message = <<-EOT
      cors_allowed_origins contains "*" while cors_allow_credentials is true, allowing
      arbitrary origins through credentialed CORS preflight. The app also refuses this
      at startup (issue #5682); plan-time validation catches it before rollout.
      Replace "*" with an explicit allowlist.
    EOT
  }

  validation {
    condition = alltrue([
      for origin in var.cors_allowed_origins :
      can(regex("^https?://[A-Za-z0-9.:-]+$", origin))
    ])
    error_message = "each cors_allowed_origins entry must be a scheme-qualified origin (https://host[:port]) — not a bare hostname and not a wildcard pattern."
  }
}

variable "cors_allow_credentials" {
  type        = bool
  description = <<-EOT
    Whether the domain API allows credentialed cross-origin requests. The rule pairing
    this with the allowlist is declared on cors_allowed_origins, not here — see the
    comment above that variable for why putting it here made it unreachable.
  EOT
  default     = true
}

# ---------------------------------------------------------------------------
# SECRET REFERENCES, NOT SECRET VALUES (acceptance criterion 3, second half)
#
# The snapshot's migration job carries its database credentials inline:
#
#   DATABASE_URL: postgresql+asyncpg://superplane:<inline password>@postgres...
#
# and its API config defaults `jwt_secret_key` to a committed placeholder string. Both
# are applied artifacts, so both would ship into a running environment and would have to
# be rotated everywhere once noticed.
#
# This module therefore accepts only Secrets Manager secret NAMES. The validations reject
# anything that looks like a value rather than a reference: a connection URI, an embedded
# `user:pass@`, or a long opaque string. The pod resolves the secret at runtime; the
# secret material never enters Terraform state, a plan output or a manifest.
#
# DECISION 2 NOTE: taking the database as a reference is also what keeps this module
# neutral on the unresolved shared-vs-separate-instance question. It makes NO isolation,
# backup, retention or restore claim about whatever the secret points at.
# ---------------------------------------------------------------------------
variable "database_secret_name" {
  type        = string
  description = <<-EOT
    Name of the pre-seeded Secrets Manager secret holding the Superplane database
    connection details. A NAME, never a value or a URI. Seeded out-of-band via the ADP
    vault path. This module does not create the database — Decision 2 (shared instance
    vs. separate instance) is unresolved, so no durability property is claimed here.
  EOT

  validation {
    condition     = can(regex("^[A-Za-z0-9/_+=.@-]{1,512}$", var.database_secret_name))
    error_message = "database_secret_name must be a Secrets Manager secret name."
  }

  validation {
    condition     = !can(regex("://", var.database_secret_name)) && !can(regex("[^/]+:[^/@]+@", var.database_secret_name))
    error_message = <<-EOT
      database_secret_name looks like a connection string, not a secret name. Inline
      credentials must not enter an applied artifact — this is exactly the upstream
      db-migrate-job.yaml defect (DATABASE_URL with an inline password). Pass the name of
      a Secrets Manager secret instead.
    EOT
  }
}

# ---------------------------------------------------------------------------
# THE DATABASE SCHEMA BOUNDARY (migration lane, PR #5283 finding 6)
#
# This is the mechanism that replaced a grep. The migration lane previously established
# its database boundary by scanning migration SQL for `ALTER TABLE <gateway table>`.
# Reproduced against the shipped pattern with the real 38-name denylist, two of three
# evasions walked straight through it:
#
#   op.drop_table("users")                                   -> not caught (Alembic's
#                                                               Python API emits none of
#                                                               the words scanned for)
#   op.execute('ALTER TABLE public."request_logs" DROP ...')  -> not caught (schema
#                                                               qualification)
#
# A text scan cannot enumerate the ways to name a table. So the boundary is the database
# session instead: this schema is the ONLY entry in the migration Job's `search_path`, and
# Alembic's version table lives inside it. An unqualified statement then cannot resolve a
# table in another module's schema, and the two chains cannot share bookkeeping.
#
# WHAT THIS DOES NOT ESTABLISH, STATED PLAINLY
#
# `search_path` constrains NAME RESOLUTION, not PRIVILEGE. A schema-qualified statement
# still reaches another schema if the connecting role has rights there. The grant that
# would close that — USAGE on this schema and nothing else — is made on the database, and
# Decision 2 (shared instance vs. separate instance) is unresolved, so this module does
# not know what database it connects to and cannot make it. No isolation, backup,
# retention or restore property is claimed here.
#
# `public` is rejected because it is where a shared instance's other tenants live, and it
# is the schema the reproduced evasions resolved into.
# ---------------------------------------------------------------------------
variable "database_schema" {
  type        = string
  description = <<-EOT
    Postgres schema owned by the Superplane domain app. Becomes the sole entry in the
    migration Job's search_path and the home of its Alembic version table. Must not be
    `public`: an unqualified DDL statement that can resolve into `public` can reach
    another module's tables, which is the case a SQL text scan could not catch.
  EOT
  default     = "superplane"

  validation {
    condition     = can(regex("^[a-z_][a-z0-9_]{0,62}$", var.database_schema))
    error_message = "database_schema must be a lowercase unquoted Postgres identifier — it is interpolated into a search_path, and a value needing quoting there is a value that will be mis-parsed."
  }

  validation {
    condition = !contains(
      ["public", "pg_catalog", "information_schema", "pg_toast", "pg_temp"],
      var.database_schema
    )
    error_message = <<-EOT
      database_schema must not be `public` or a Postgres system schema. `public` is where
      a shared instance's other tenants live: with it resolvable, an unqualified
      op.drop_table("users") in a Superplane migration reaches the gateway's users table.
      That is the exact case the SQL text scan this replaced did not catch. Give the
      domain app its own schema.
    EOT
  }
}

variable "jwt_secret_name" {
  type        = string
  description = <<-EOT
    Name of the Secrets Manager secret holding the API's JWT signing key. A NAME, never
    the key itself. Upstream defaults this to a committed placeholder; that value must
    never reach an applied artifact.
  EOT

  validation {
    condition     = can(regex("^[A-Za-z0-9/_+=.@-]{1,512}$", var.jwt_secret_name))
    error_message = "jwt_secret_name must be a Secrets Manager secret name."
  }

  validation {
    # A secret name is a path-like identifier. A signing key is high-entropy and long.
    # Rejecting a long value with no separators catches "somebody pasted the key here"
    # without constraining legitimate names like adp/dev/superplane/jwt-signing-key.
    condition = (
      length(var.jwt_secret_name) <= 64 ||
      can(regex("/", var.jwt_secret_name))
    )
    error_message = "jwt_secret_name looks like a secret VALUE rather than a secret name. Pass the Secrets Manager secret name; the signing key must never enter Terraform."
  }
}

# ---------------------------------------------------------------------------
# Kubernetes identity
#
# The namespace and service accounts the IRSA trust policies below are scoped to. These
# are inputs rather than hardcoded strings so the rollout lane and this module cannot
# drift apart silently — tests/platform_isolation.tftest.hcl asserts the trust policy is
# scoped to this namespace and not to the whole cluster.
# ---------------------------------------------------------------------------
variable "namespace" {
  type        = string
  description = "Kubernetes namespace for the Superplane control plane. Domain-owned; must not be a core ADP namespace."
  default     = "superplane"

  validation {
    condition = !contains(
      ["kube-system", "default", "adp-gateway", "adp-system", "arc-runners"],
      var.namespace
    )
    error_message = "namespace must not be a core ADP or Kubernetes system namespace — the domain app owns its own namespace (platform isolation requirement)."
  }
}

variable "skypilot_namespace" {
  type        = string
  description = "Namespace for the SkyPilot API service. Pinned by U2's lock (skypilot_config.namespace = skypilot)."
  default     = "skypilot"
}

# ---------------------------------------------------------------------------
# GPU workspace target (platform isolation requirement, 2026-09-16)
#
# "GPU workloads target the intended workspace cluster, not the ADP management cluster."
# SkyPilot launches clusters through a Kubernetes context, so the wrong context here
# means GPU jobs land on the management cluster. Empty is the safe default: it means "no
# Kubernetes workspace target configured", and the SkyPilot config then offers only the
# non-Kubernetes clouds from U2's allowed_clouds. Silently defaulting to the current
# cluster is the failure this avoids.
# ---------------------------------------------------------------------------
variable "workspace_cluster_context" {
  type        = string
  description = <<-EOT
    Kubernetes context name for the GPU workspace cluster SkyPilot may schedule onto.
    Empty means no Kubernetes workspace target is configured — GPU workloads must never
    default onto the ADP management cluster.
  EOT
  default     = ""
}

# See the SkyPilot role in irsa.tf for why this is a seam rather than an inline policy:
# SkyPilot's compute-launching permission set is broad, the target account is unresolved,
# and the acceptance that would validate it is deferred. Empty means nothing is granted.
variable "skypilot_compute_policy_arns" {
  type        = list(string)
  description = <<-EOT
    IAM policy ARNs granting the SkyPilot API server its compute-launching permissions.
    Empty by default: the grant is attached by whoever resolves the target account and
    spend authorization (U19 owns the SkyPilot resource/state handover). No placeholder
    policy is invented here.
  EOT
  default     = []

  validation {
    condition = alltrue([
      for arn in var.skypilot_compute_policy_arns :
      can(regex("^arn:aws[a-z-]*:iam::(aws|[0-9]{12}):policy/", arn))
    ])
    error_message = "each entry must be an IAM policy ARN."
  }
}

variable "cost_center" {
  type        = string
  description = "Cost attribution tag for domain-app spend."
  default     = "engineering"
}

variable "manage_image_builds" {
  description = "Explicitly enroll existing image lanes only after coordinated state ownership migration. Installation keeps this false; never disable an already enrolled state without a reviewed transfer."
  type        = bool
  default     = false
}
