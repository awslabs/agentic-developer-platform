# =============================================================================
# RDS Bootstrap Module
# =============================================================================
# Runs a one-shot Kubernetes Job that executes:
#   GRANT rds_iam TO <user>;
#
# This is required on fresh databases so that IAM-authenticated connections
# succeed. Without it, Postgres rejects IAM tokens even though IAM auth is
# enabled at the RDS level.
#
# Idempotency:
# - `GRANT rds_iam TO <user>` is idempotent in PostgreSQL.
# - The Job name includes a hash of the RDS instance ID so it only re-runs
#   when the database is recreated. ttlSecondsAfterFinished auto-cleans
#   completed Jobs so `terraform apply` is a no-op on subsequent runs.
# =============================================================================

# ---------------------------------------------------------------------------
# IRSA: Service Account + IAM Role for the bootstrap Job
# ---------------------------------------------------------------------------
# The Job needs secretsmanager:GetSecretValue to read the master password.

resource "aws_iam_role" "rds_bootstrap" {
  name = "${var.name_prefix}-rds-bootstrap"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = var.oidc_provider_arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${var.oidc_issuer}:sub" = "system:serviceaccount:${var.namespace}:${var.name_prefix}-rds-bootstrap"
            "${var.oidc_issuer}:aud" = "sts.amazonaws.com"
          }
        }
      }
    ]
  })

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-rds-bootstrap"
    Purpose = "rds-bootstrap-job"
  })
}

resource "aws_iam_role_policy" "rds_bootstrap_secrets" {
  name = "${var.name_prefix}-rds-bootstrap-secrets"
  role = aws_iam_role.rds_bootstrap.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = var.master_user_secret_arn
      }
    ]
  })
}

resource "kubernetes_service_account" "rds_bootstrap" {
  metadata {
    name      = "${var.name_prefix}-rds-bootstrap"
    namespace = var.namespace
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.rds_bootstrap.arn
    }
  }
}

# ---------------------------------------------------------------------------
# Kubernetes Namespace (read-only lookup)
# ---------------------------------------------------------------------------
# The namespace is owned by other modules / the backend-deploy step. Treat
# it as read-only here so we don't fight for ownership. If it doesn't exist
# the data source fails and the Job can't run — that's the correct signal.

data "kubernetes_namespace" "gateway" {
  metadata {
    name = var.namespace
  }
}

# ---------------------------------------------------------------------------
# Bootstrap Job
# ---------------------------------------------------------------------------
# Uses amazonlinux:2023-minimal with psql, jq, and awscli installed inline
# to keep the image public & trusted. The Job:
#   1. Reads the master password from Secrets Manager
#   2. Connects to Postgres
#   3. Runs GRANT rds_iam TO <user>
#
# ttlSecondsAfterFinished = 120 ensures the completed pod is cleaned up
# quickly, preventing "already exists" errors on the next terraform apply.
# ---------------------------------------------------------------------------

resource "kubernetes_job" "grant_rds_iam" {
  metadata {
    # Include a short hash of the RDS instance ID so the Job name changes
    # (and therefore re-runs) when the database is recreated.
    name      = "${var.name_prefix}-rds-bootstrap-${substr(sha256(var.rds_instance_id), 0, 8)}"
    namespace = var.namespace
  }

  spec {
    ttl_seconds_after_finished = 120
    backoff_limit              = 6

    template {
      metadata {
        labels = {
          app     = "${var.name_prefix}-rds-bootstrap"
          purpose = "one-shot-ddl"
        }
      }

      spec {
        service_account_name    = kubernetes_service_account.rds_bootstrap.metadata[0].name
        restart_policy          = "OnFailure"
        active_deadline_seconds = 600

        container {
          name  = "bootstrap"
          image = "public.ecr.aws/amazonlinux/amazonlinux:2023"

          command = ["/bin/bash", "-c"]
          args = [<<-EOT
            set -euo pipefail
            echo "=== RDS Bootstrap: GRANT rds_iam TO $DB_USER ==="
            # Package name note: on Amazon Linux 2023 the AWS CLI package is
            # `awscli-2` (not `aws-cli`). Verified via `dnf list available '*awscli*'`
            # in a live AL2023 pod. Do NOT redirect install stderr — a silent
            # "No matches found" leads to a cryptic `aws: command not found`
            # at the secretsmanager step.
            dnf install -y postgresql15 jq awscli-2
            MASTER_JSON=$(aws secretsmanager get-secret-value \
              --secret-id "$SECRET_ID" \
              --region "$AWS_REGION" \
              --query SecretString --output text)
            MASTER_USER=$(echo "$MASTER_JSON" | jq -r .username)
            MASTER_PASS=$(echo "$MASTER_JSON" | jq -r .password)
            PGPASSWORD="$MASTER_PASS" psql \
              -h "$DB_HOST" -U "$MASTER_USER" -d "$DB_NAME" -p 5432 \
              -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_auth_members WHERE roleid = (SELECT oid FROM pg_roles WHERE rolname='rds_iam') AND member = (SELECT oid FROM pg_roles WHERE rolname='$DB_USER')) THEN EXECUTE 'GRANT rds_iam TO $DB_USER'; RAISE NOTICE 'Granted rds_iam to $DB_USER'; ELSE RAISE NOTICE 'rds_iam already granted to $DB_USER'; END IF; END \$\$;"
            echo "=== RDS Bootstrap complete ==="
          EOT
          ]

          env {
            name  = "SECRET_ID"
            value = var.master_user_secret_arn
          }
          env {
            name  = "DB_HOST"
            value = var.db_host
          }
          env {
            name  = "DB_USER"
            value = var.db_username
          }
          env {
            name  = "DB_NAME"
            value = var.db_name
          }
          env {
            name  = "AWS_REGION"
            value = var.aws_region
          }

          resources {
            requests = {
              cpu    = "100m"
              memory = "256Mi"
            }
            limits = {
              cpu    = "250m"
              memory = "512Mi"
            }
          }
        }
      }
    }
  }

  wait_for_completion = true

  timeouts {
    create = "15m"
  }

  depends_on = [
    kubernetes_service_account.rds_bootstrap,
    data.kubernetes_namespace.gateway,
  ]
}
