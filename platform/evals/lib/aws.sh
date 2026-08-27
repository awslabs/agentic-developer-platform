# shellcheck shell=bash
# =============================================================================
# lib/aws.sh — the credentialed harness helpers
# =============================================================================
# h_* helpers are the ONLY credentialed paths in an eval. They exist so a reader
# can grep for exactly which steps touch AWS, and so the boundary between
# "harness" (holds credentials) and "laptop" (holds none) is visible in the code
# rather than asserted in a doc. See lib/pod.sh for the other side of that line.
# =============================================================================

h_aws()     { aws --region "$AWS_REGION" "$@"; }
h_kubectl() { kubectl "$@"; }
h_psql()    { psql --no-psqlrc -q -t -A "$@"; }

# eval_ssm <parameter-name> — read an SSM parameter, empty string if absent.
# Non-fatal by design: the caller decides which parameters are required, and
# reports them together rather than dying on the first gap.
eval_ssm() {
  h_aws ssm get-parameter --name "$1" --query Parameter.Value --output text 2>/dev/null || echo ""
}

# -----------------------------------------------------------------------------
# Resolve the RDS connection + a fresh IAM auth token into the PG* environment.
# -----------------------------------------------------------------------------
# Self-contained (resolves RDS_HOST/RDS_DB from SSM if not already set) so it is
# callable from BOTH a full run and the --cleanup-only branch — the sweep deletes
# rows via psql, so it needs DB creds too. Without this the standalone sweep
# failed to connect and reported spurious "could not delete row" litter.
#
# bedrockgw-<env>-postgres has IAM database authentication enabled and its master
# user is granted rds_iam in-database — exactly how the gateway itself connects
# (BG_RDS_IAM_AUTH=true, no password in BG_DATABASE_URL). That grant *disables*
# password auth for that user, so pulling the managed master password from the
# rds!db-* secret and offering it yields "PAM authentication failed". We mint a
# short-lived IAM auth token instead and use it as PGPASSWORD (the harness
# runner's IRSA is authorized for rds-db:connect). The username still comes from
# the managed secret so we track the master user without hard-coding it. IAM auth
# mandates TLS (PGSSLMODE).
#
# Do not "simplify" this back to the master-password pattern: it cannot work.
resolve_db_creds() {
  local secret_arn secret
  [ -n "${RDS_HOST:-}" ] || RDS_HOST="$(eval_ssm "/adp/${ENVIRONMENT}/gateway/rds-host")"
  [ -n "${RDS_DB:-}" ]   || RDS_DB="$(eval_ssm "/adp/${ENVIRONMENT}/gateway/rds-database-name")"
  if [ -z "$RDS_HOST" ] || [ -z "$RDS_DB" ]; then
    die "Could not resolve RDS host/database for ${ENVIRONMENT}"
  fi

  secret_arn="$(h_aws secretsmanager list-secrets --filters Key=name,Values="rds!db-" \
    --query 'SecretList[0].ARN' --output text 2>/dev/null || echo "")"
  if [ -z "$secret_arn" ] || [ "$secret_arn" = "None" ]; then
    die "Could not find the rds!db-* secret"
  fi
  secret="$(h_aws secretsmanager get-secret-value --secret-id "$secret_arn" \
    --query SecretString --output text)"
  PGUSER="$(printf '%s' "$secret" | jq -r .username)"
  PGPASSWORD="$(h_aws rds generate-db-auth-token \
    --hostname "$RDS_HOST" --port 5432 --region "${AWS_REGION:-us-east-1}" \
    --username "$PGUSER" 2>/dev/null || echo "")"
  [ -n "$PGPASSWORD" ] || die "Failed to mint an RDS IAM auth token for $PGUSER@$RDS_HOST"
  mask "$PGPASSWORD"
  export PGHOST="$RDS_HOST" PGDATABASE="$RDS_DB" PGUSER PGPASSWORD PGSSLMODE=require
}
