#!/usr/bin/env bash
set -euo pipefail
# Package installation needs more than 512 MiB with current AL2023 metadata.
dnf install -y postgresql15 jq awscli-2
export PGSSLMODE=require PGCONNECT_TIMEOUT=10
verify_iam() {
  local token
  token=$(aws rds generate-db-auth-token --hostname "$DB_HOST" --port 5432 --username "$DB_USER" --region "$AWS_REGION") || return 1
  PGPASSWORD="$token" psql -X -v ON_ERROR_STOP=1 -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -p 5432 -c 'SELECT 1' >/dev/null
}
# Once rds_iam is granted, PostgreSQL requires IAM rather than the master password.
if verify_iam; then
  echo 'RDS IAM login already works; bootstrap complete.'
  exit 0
fi
MASTER_JSON=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ID" --region "$AWS_REGION" --query SecretString --output text)
MASTER_USER=$(printf '%s' "$MASTER_JSON" | jq -er .username)
MASTER_PASS=$(printf '%s' "$MASTER_JSON" | jq -er .password)
PGPASSWORD="$MASTER_PASS" psql -X -v ON_ERROR_STOP=1 -v "db_user=$DB_USER" -h "$DB_HOST" -U "$MASTER_USER" -d "$DB_NAME" -p 5432 <<'SQL'
GRANT rds_iam TO :"db_user";
SQL
unset MASTER_JSON MASTER_USER MASTER_PASS
verify_iam
echo 'RDS IAM login verified; bootstrap complete.'
