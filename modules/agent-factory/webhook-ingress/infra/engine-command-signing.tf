# =============================================================================
# Engine-Command Signing Key (Issue #4539)
# =============================================================================
# HMAC-SHA256 keyring used to sign the canonical authority envelope for an
# `@agent-engine` command, and to verify it on the gateway orchestration tick.
#
# WHY THIS KEY EXISTS AT ALL
# --------------------------
# An `@agent-engine` command reaches the tick as a DynamoDB row, never as a call:
# this Lambda marks the row `engine_command_status=pending` and the tick finds it on
# the sparse `engine-command-index`. The row therefore carries *who asked for what, on
# which plan* — tenant, installation, repository, issue, commenter id, author kind,
# command body. Before #4539 those were ordinary mutable attributes, so anything able
# to write the row could choose the acting identity and routing target of a human
# approval, and the tick had no way to tell a delivered tuple from an authored one.
#
# WHY IT IS NOT THE MARKER-SIGNING KEY
# ------------------------------------
# `aws_secretsmanager_secret.marker_signing_key` (#3178) is deliberately READABLE by
# the agent worker cohort: the worker signs correlation markers with it, and
# `scaledjob-iam.tf`'s `MarkerSigningKeyKMSDecrypt` grants the pre-authority role a
# `kms:ViaService` + `kms:EncryptionContext:SecretARN`-scoped decrypt for exactly that
# secret. Reusing it here would hand every worker the ability to mint a command
# envelope naming any tenant, any commenter and any repository — the precise forgery
# this issue exists to close, with a signature attached to make it look verified.
#
# So: a separate secret, on a SEPARATE CMK, readable by exactly two principals.
#
# WHY A SEPARATE CMK AND NOT THE SHARED WEBHOOK-SECRETS ONE
# ---------------------------------------------------------
# `scaledjob-iam.tf`'s `SecretsManagerOps` grants the pre-authority worker role
# `secretsmanager:GetSecretValue` on `secret:adp/*` — which matches this secret's
# name. On the shared `alias/adp-<env>-webhook-secrets` CMK, the ONLY thing standing
# between that role and this key would be the absence of a `kms:Decrypt` grant, i.e.
# the same single control #4028 documented as load-bearing for the marker key. A
# dedicated CMK makes the isolation structural instead: the key policy names its
# readers, so a future widening of a `secret:adp/*` Allow cannot reach it, and the
# `DenyOtherEncryptionKeys` / `DenyDirectKMS` statements in the authority boundary
# deny it for the authority cohort as well.
#
# NO SECRET VALUE IN TERRAFORM STATE, OUTPUTS, GIT OR LOGS
# --------------------------------------------------------
# Terraform provisions the CONTAINER and the grants; it never learns the key. The
# initial version is a placeholder that BOTH implementations treat as "no key at all"
# (#4128: a signature computed under a value published in this repo would report
# success, which is strictly worse than no verification), and `ignore_changes` keeps
# the real value out of every subsequent plan and state refresh. Seeding and rotation
# are operator actions performed with the AWS CLI — see
# `docs/runbooks/engine-command-signing-key-rotation.md`.
#
# Only ARNs are output or written to SSM. An ARN is not sensitive; it is exactly what
# both deploy units need in order to read the secret they are entitled to read.
#
# ACTIVATION ORDER (this file does not activate anything)
# ------------------------------------------------------
# Commands stay disabled (`FEATURE_ORCHESTRATION_ENGINE_ENABLED` unset) → apply this →
# seed the keyring → deploy signer and verifier → reconcile/quarantine pre-signing
# pending rows → prove the worker cohort cannot read the key → exercise positive and
# negative cases → only then request activation. The runbook is the authority on that
# sequence; nothing here short-circuits it, and a missing or placeholder key fails
# closed (every command quarantined) rather than open.
# =============================================================================

# -----------------------------------------------------------------------------
# Dedicated CMK
# -----------------------------------------------------------------------------

resource "aws_kms_key" "engine_command_signing" {
  description = "Encrypts the engine-command attribution signing keyring (issue #4539)"
  # A command may sit in the table for as long as the events TTL, and a key deleted
  # under it makes every pending command unverifiable — which fails closed, but
  # loudly and irrecoverably. 30 days is the maximum window and buys time to notice.
  deletion_window_in_days = 30
  enable_key_rotation     = false # The KEYRING rotates; see the runbook. AWS-managed
  # rotation would re-encrypt the container without changing the
  # HMAC key, which is not the rotation this design needs.

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Account root retains administrative control. Without this the key is
        # unmanageable — including undeletable — which AWS rejects outright.
        Sid       = "AllowAccountAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        # The named readers, and only via Secrets Manager on this one secret. The
        # encryption-context condition is what makes "this role may decrypt exactly
        # the engine-command keyring" true rather than "this role may decrypt
        # anything encrypted with this key" — Secrets Manager passes SecretARN in
        # the KMS encryption context on every GetSecretValue.
        #
        # The signer (this module's webhook Lambda) and the verifier (the gateway
        # orchestration tick) are the ONLY entries. Deliberately not the agent
        # worker role, not the supervisor, and not the ARC runner role.
        Sid    = "AllowSignerAndVerifierDecrypt"
        Effect = "Allow"
        Principal = {
          AWS = compact([
            aws_iam_role.lambda_execution.arn,
            var.engine_command_verifier_role_arn,
          ])
        }
        Action   = ["kms:Decrypt", "kms:DescribeKey"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService"                  = "secretsmanager.${var.aws_region}.amazonaws.com"
            "kms:EncryptionContext:SecretARN" = "arn:aws:secretsmanager:${var.aws_region}:${local.account_id}:secret:adp/${var.environment}/webhook-ingress/engine-command-signing-key-*"
          }
        }
      },
      {
        # Belt and braces against a future edit to this file, a Deny in an SCP
        # gap, or an IAM policy that grants kms:Decrypt on "*". The worker cohort
        # holds `secretsmanager:GetSecretValue` on `secret:adp/*`, so an
        # unconditional key-policy Allow appearing here later would silently open
        # the forgery path. Naming the roles in an explicit Deny means adding an
        # Allow is not enough to grant them access.
        #
        # Both worker roles: the pre-authority `agent_scaledjob` role and, when
        # the flag is on, `agent_authority_worker`. The authority boundary already
        # carries `DenyAllSecrets`, but that policy is only attached while
        # `agent_authority_enabled` is true, and a boundary is an IAM control on a
        # role — this is a resource control on the key, so it holds even for a
        # principal that acquires new identity policies.
        Sid    = "DenyAgentWorkerCohort"
        Effect = "Deny"
        Principal = {
          AWS = compact([
            aws_iam_role.agent_scaledjob.arn,
            var.agent_authority_enabled ? aws_iam_role.agent_authority_worker[0].arn : "",
          ])
        }
        Action   = "kms:*"
        Resource = "*"
      },
    ]
  })

  tags = {
    Purpose = "engine-command-signing"
    Issue   = "4539"
  }
}

resource "aws_kms_alias" "engine_command_signing" {
  name          = "alias/${local.name_prefix}-engine-command-signing"
  target_key_id = aws_kms_key.engine_command_signing.key_id
}

# -----------------------------------------------------------------------------
# The keyring secret
# -----------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "engine_command_signing_key" {
  name        = "adp/${var.environment}/webhook-ingress/engine-command-signing-key"
  description = "HMAC-SHA256 keyring for engine-command attribution envelopes (issue #4539). Signer: webhook Lambda. Verifier: gateway orchestration tick. NOT readable by agent workers."
  kms_key_id  = aws_kms_key.engine_command_signing.arn

  # Non-zero, unlike the neighbouring secrets. A deleted keyring makes every pending
  # command unverifiable; a recovery window is the difference between "restore it" and
  # "every command in the table is permanently refused".
  recovery_window_in_days = 7

  tags = {
    Purpose  = "engine-command-signing"
    Rotation = "manual-with-overlap"
    Issue    = "4539"
  }
}

resource "aws_secretsmanager_secret_version" "engine_command_signing_key" {
  secret_id = aws_secretsmanager_secret.engine_command_signing_key.id

  # A PLACEHOLDER, and both implementations know it by name: `command_signing.py` and
  # `command_attribution.py` each carry this exact string in `_PLACEHOLDER_KEYS` and
  # treat it as "no key available". So a deployed-but-unseeded environment refuses
  # every command instead of signing and verifying under a value that ships in git —
  # which would report success (#4128).
  #
  # The shape below is the real keyring shape so that an operator seeding it has a
  # template, but `active_key_id` names no usable material.
  secret_string = jsonencode({
    active_key_id = "PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND"
    keys = {
      PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND = "PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND"
    }
  })

  lifecycle {
    # The real value is written out of band and must never enter state, a plan diff,
    # or CI logs. Without this, the first apply after seeding would show the operator's
    # key as a diff and then overwrite it with the placeholder.
    ignore_changes = [secret_string]
  }
}

# -----------------------------------------------------------------------------
# Reader grants — exactly two principals
# -----------------------------------------------------------------------------

# The SIGNER. Attached to the webhook Lambda role rather than folded into iam.tf's
# inline policy so that "who can read the command-signing key" is one grep away and
# cannot be widened by an unrelated edit to a large shared statement.
resource "aws_iam_role_policy" "webhook_lambda_engine_command_signing" {
  name = "engine-command-signing-key-read"
  role = aws_iam_role.lambda_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadEngineCommandSigningKey"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource = aws_secretsmanager_secret.engine_command_signing_key.arn
      },
      {
        # iam.tf's `SecretsKMSDecrypt` grants this role kms:Decrypt on the SHARED
        # webhook-secrets CMK. This key is a different CMK, so that statement does
        # not reach it and this grant is required. Conditioned the same way the key
        # policy is: via Secrets Manager, for this secret only.
        Sid      = "DecryptEngineCommandSigningKey"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:DescribeKey"]
        Resource = aws_kms_key.engine_command_signing.arn
        Condition = {
          StringEquals = {
            "kms:ViaService"                  = "secretsmanager.${var.aws_region}.amazonaws.com"
            "kms:EncryptionContext:SecretARN" = aws_secretsmanager_secret.engine_command_signing_key.arn
          }
        }
      },
    ]
  })
}

# The VERIFIER — the gateway orchestration tick, a different deploy unit with its own
# Terraform state. Its role ARN is passed in rather than referenced, because this
# module cannot see gateway state. Empty (the default) means the grant is not created:
# an environment that has not yet wired the verifier gets a signer and no verifier,
# which fails closed (every command quarantined with `no_verification_key`) rather
# than granting a role ARN somebody guessed.
resource "aws_iam_role_policy" "engine_command_verifier" {
  count = var.engine_command_verifier_role_arn != "" ? 1 : 0

  name = "engine-command-signing-key-verify"
  role = element(split("/", var.engine_command_verifier_role_arn), length(split("/", var.engine_command_verifier_role_arn)) - 1)

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # READ ONLY, and deliberately no write action of any kind. The verifier has
        # no reason to rotate, tag or replace the keyring, and a verifier that could
        # write it could install a key it also holds — which would make the whole
        # signature meaningless.
        Sid      = "ReadEngineCommandSigningKey"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource = aws_secretsmanager_secret.engine_command_signing_key.arn
      },
      {
        Sid      = "DecryptEngineCommandSigningKey"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:DescribeKey"]
        Resource = aws_kms_key.engine_command_signing.arn
        Condition = {
          StringEquals = {
            "kms:ViaService"                  = "secretsmanager.${var.aws_region}.amazonaws.com"
            "kms:EncryptionContext:SecretARN" = aws_secretsmanager_secret.engine_command_signing_key.arn
          }
        }
      },
    ]
  })
}

# -----------------------------------------------------------------------------
# Discovery
# -----------------------------------------------------------------------------

# ARN only, never the value. The gateway deploy unit reads this parameter to set
# `ENGINE_COMMAND_SIGNING_KEY_SECRET_ARN` on the tick, which is the same env var name
# the signer reads — one key behind two names drifts silently, and the failure mode of
# that drift is every command refused.
resource "aws_ssm_parameter" "engine_command_signing_key_arn" {
  name        = "/adp/${var.environment}/webhook-ingress/engine-command-signing-key-arn"
  description = "Secrets Manager ARN of the engine-command attribution signing keyring (issue #4539)"
  type        = "String"
  value       = aws_secretsmanager_secret.engine_command_signing_key.arn
}

# Outputs live here rather than in outputs.tf, next to the resources and the reasoning
# they belong to. The rule this follows is the one that matters for a signing key: a
# reader looking at "what does this module publish about the command-signing key"
# should not have to establish that nothing in another file publishes the VALUE.
# Both of these are ARNs.

output "engine_command_signing_key_secret_arn" {
  description = "Secrets Manager ARN of the engine-command attribution signing keyring (issue #4539). The gateway deploy unit needs this to grant its tick read access and to set ENGINE_COMMAND_SIGNING_KEY_SECRET_ARN. Never the value."
  value       = aws_secretsmanager_secret.engine_command_signing_key.arn
}

output "engine_command_signing_kms_key_arn" {
  description = "ARN of the dedicated CMK encrypting the engine-command signing keyring (issue #4539). Exported so the verifier's own state can scope its kms:Decrypt to this key rather than to a wildcard."
  value       = aws_kms_key.engine_command_signing.arn
}
