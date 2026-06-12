# =============================================================================
# Agent-Context RDS Bootstrap Module
# =============================================================================
# One-shot Kubernetes Job that:
#   1. Creates the `agent_context` database on the shared gateway RDS instance
#   2. Creates the `agent_context_svc` role with rds_iam grant
#   3. Grants ownership of the database to the new role
#
# This is the agent-context equivalent of modules/gateway/infra/modules/rds-bootstrap/.
# The gateway bootstrap grants rds_iam to bgadmin; this one creates a new DB + user.
#
# Idempotency:
# - CREATE DATABASE IF NOT EXISTS (via PL/pgSQL DO block)
# - CREATE USER IF NOT EXISTS (via DO block)
# - GRANT is idempotent in PostgreSQL
# =============================================================================

# ---------------------------------------------------------------------------
# IRSA: Service Account + IAM Role for the bootstrap Job
# ---------------------------------------------------------------------------

resource "aws_iam_role" "ac_rds_bootstrap" {
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
    Purpose = "agent-context-rds-bootstrap"
  })
}

resource "aws_iam_role_policy" "ac_rds_bootstrap_secrets" {
  name = "${var.name_prefix}-rds-bootstrap-secrets"
  role = aws_iam_role.ac_rds_bootstrap.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = var.master_user_secret_arn != "" ? var.master_user_secret_arn : "arn:aws:secretsmanager:*:*:secret:placeholder-not-used"
      }
    ]
  })
}

resource "kubernetes_service_account" "ac_rds_bootstrap" {
  metadata {
    name      = "${var.name_prefix}-rds-bootstrap"
    namespace = var.namespace
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.ac_rds_bootstrap.arn
    }
  }
}

# ---------------------------------------------------------------------------
# Bootstrap Job
# ---------------------------------------------------------------------------
# Creates the agent_context database and agent_context_svc user on the shared
# RDS instance. Connects as the master user (password from Secrets Manager).
#
# SQL operations (all idempotent):
#   1. CREATE DATABASE agent_context (if not exists)
#   2. CREATE USER agent_context_svc (if not exists)
#   3. GRANT rds_iam TO agent_context_svc
#   4. GRANT ALL PRIVILEGES ON DATABASE agent_context TO agent_context_svc
#   5. Connect to agent_context DB and set default privileges
# ---------------------------------------------------------------------------

resource "kubernetes_job" "ac_bootstrap" {
  metadata {
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
        service_account_name    = kubernetes_service_account.ac_rds_bootstrap.metadata[0].name
        restart_policy          = "OnFailure"
        active_deadline_seconds = 600

        container {
          name  = "bootstrap"
          image = "public.ecr.aws/amazonlinux/amazonlinux:2023"

          command = ["/bin/bash", "-c"]
          args = [<<-EOT
            set -euo pipefail
            echo "=== Agent-Context RDS Bootstrap ==="
            echo "Creating database: $AC_DB_NAME"
            echo "Creating user: $AC_DB_USER"

            dnf install -y postgresql15 jq awscli-2

            # Fetch master credentials from Secrets Manager
            MASTER_JSON=$(aws secretsmanager get-secret-value \
              --secret-id "$SECRET_ID" \
              --region "$AWS_REGION" \
              --query SecretString --output text)
            MASTER_USER=$(echo "$MASTER_JSON" | jq -r .username)
            MASTER_PASS=$(echo "$MASTER_JSON" | jq -r .password)

            # Step 1: Create the agent_context database (idempotent)
            PGPASSWORD="$MASTER_PASS" psql \
              -h "$DB_HOST" -U "$MASTER_USER" -d "$GATEWAY_DB_NAME" -p 5432 \
              -c "SELECT 'CREATE DATABASE $AC_DB_NAME' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$AC_DB_NAME')\gexec"

            # Step 2: Create the agent_context_svc user (idempotent)
            PGPASSWORD="$MASTER_PASS" psql \
              -h "$DB_HOST" -U "$MASTER_USER" -d "$GATEWAY_DB_NAME" -p 5432 \
              -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='$AC_DB_USER') THEN CREATE USER $AC_DB_USER; RAISE NOTICE 'Created user $AC_DB_USER'; ELSE RAISE NOTICE 'User $AC_DB_USER already exists'; END IF; END \$\$;"

            # Step 3: Grant rds_iam to the new user (for IAM token auth)
            PGPASSWORD="$MASTER_PASS" psql \
              -h "$DB_HOST" -U "$MASTER_USER" -d "$GATEWAY_DB_NAME" -p 5432 \
              -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_auth_members WHERE roleid = (SELECT oid FROM pg_roles WHERE rolname='rds_iam') AND member = (SELECT oid FROM pg_roles WHERE rolname='$AC_DB_USER')) THEN EXECUTE 'GRANT rds_iam TO $AC_DB_USER'; RAISE NOTICE 'Granted rds_iam to $AC_DB_USER'; ELSE RAISE NOTICE 'rds_iam already granted to $AC_DB_USER'; END IF; END \$\$;"

            # Step 4: Grant all privileges on agent_context DB to the new user
            PGPASSWORD="$MASTER_PASS" psql \
              -h "$DB_HOST" -U "$MASTER_USER" -d "$GATEWAY_DB_NAME" -p 5432 \
              -c "GRANT ALL PRIVILEGES ON DATABASE $AC_DB_NAME TO $AC_DB_USER;"

            # Step 5: Connect to the agent_context DB and set default privileges
            PGPASSWORD="$MASTER_PASS" psql \
              -h "$DB_HOST" -U "$MASTER_USER" -d "$AC_DB_NAME" -p 5432 \
              -c "GRANT ALL ON SCHEMA public TO $AC_DB_USER; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO $AC_DB_USER; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO $AC_DB_USER;"

            echo "=== Agent-Context RDS Bootstrap complete ==="
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
            name  = "GATEWAY_DB_NAME"
            value = var.gateway_db_name
          }
          env {
            name  = "AC_DB_NAME"
            value = var.ac_db_name
          }
          env {
            name  = "AC_DB_USER"
            value = var.ac_db_username
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

  # Async execution — same pattern as gateway bootstrap (issue #769)
  wait_for_completion = false

  timeouts {
    create = "15m"
  }

  depends_on = [
    kubernetes_service_account.ac_rds_bootstrap,
  ]
}
