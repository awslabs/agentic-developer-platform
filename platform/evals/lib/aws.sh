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
# The protected environment supplies the approved database username. IAM auth
# requires an exact rds-db:connect grant and TLS; no master secret is discovered.
resolve_db_creds() {
  [ -n "${RDS_HOST:-}" ] || RDS_HOST="$(eval_ssm "/adp/${ENVIRONMENT}/gateway/rds-host")"
  [ -n "${RDS_DB:-}" ]   || RDS_DB="$(eval_ssm "/adp/${ENVIRONMENT}/gateway/rds-database-name")"
  if [ -z "$RDS_HOST" ] || [ -z "$RDS_DB" ]; then
    die "Could not resolve RDS host/database for ${ENVIRONMENT}"
  fi

  PGUSER="${ADP_DB_USER:-}"
  [ -n "$PGUSER" ] || die "Set ADP_DB_USER to the approved IAM database username"
  PGPASSWORD="$(h_aws rds generate-db-auth-token \
    --hostname "$RDS_HOST" --port 5432 --region "${AWS_REGION:-us-east-1}" \
    --username "$PGUSER" 2>/dev/null || echo "")"
  [ -n "$PGPASSWORD" ] || die "Failed to mint an RDS IAM auth token for $PGUSER@$RDS_HOST"
  mask "$PGPASSWORD"
  export PGHOST="$RDS_HOST" PGDATABASE="$RDS_DB" PGUSER PGPASSWORD PGSSLMODE=require
}
