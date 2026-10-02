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
# - The Job first verifies IAM login, avoiding password auth after rds_iam is granted.
# - Completed Jobs are retained as evidence; the physical RDS resource ID changes
#   the Job name when the database is recreated.
# =============================================================================

# ---------------------------------------------------------------------------
# IRSA: Service Account + IAM Role for the bootstrap Job
# ---------------------------------------------------------------------------
# The Job needs secretsmanager:GetSecretValue to read the master password.

resource "aws_iam_role" "rds_bootstrap" {
  permissions_boundary = var.automation_permissions_boundary_arn
  name                 = "${var.name_prefix}-rds-bootstrap"

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
      },
      {
        Effect   = "Allow"
        Action   = ["rds-db:connect"]
        Resource = var.db_connect_arn
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
# Keep the completed Job so later applies can observe successful bootstrap.
# ---------------------------------------------------------------------------

resource "kubernetes_job" "grant_rds_iam" {
  metadata {
    # Include a short hash of the RDS instance ID so the Job name changes
    # (and therefore re-runs) when the database is recreated.
    name      = "${var.name_prefix}-rds-bootstrap-${substr(sha256(var.db_connect_arn), 0, 8)}"
    namespace = var.namespace
  }

  spec {
    backoff_limit = 6

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
        active_deadline_seconds = 900

        container {
          name  = "bootstrap"
          image = "public.ecr.aws/amazonlinux/amazonlinux:2023"

          command = ["/bin/bash", "-c"]
          args    = [file("${path.module}/bootstrap.sh")]

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
              memory = "512Mi"
            }
            limits = {
              cpu    = "1000m"
              memory = "2Gi"
            }
          }
        }
      }
    }
  }

  # Gateway migrations require a successful IAM login, not just a submitted Job.
  wait_for_completion = true

  timeouts {
    create = "15m"
  }

  depends_on = [
    kubernetes_service_account.rds_bootstrap,
    aws_iam_role_policy.rds_bootstrap_secrets,
    data.kubernetes_namespace.gateway,
  ]
}
