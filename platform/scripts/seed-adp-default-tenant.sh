#!/usr/bin/env bash
# seed-adp-default-tenant.sh — Idempotent seed for the adp-default free-tier tenant.
#
# Issue #466: Creates the well-known org row, budget config, and rate-limit config
# for the shared free-tier tenant. Safe to run multiple times.
#
# Usage:
#   ./platform/scripts/seed-adp-default-tenant.sh [environment]
#
# Arguments:
#   environment   dev | staging | prod (default: dev)
#
# Prerequisites:
#   - AWS CLI configured with access to the target RDS instance
#   - psql available (or falls back to kubectl exec into the gateway pod)
#   - BG_DATABASE_URL set, OR script discovers RDS endpoint from SSM

set -euo pipefail

ENVIRONMENT="${1:-dev}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Well-known constants (must match src/shared/config.py BG_ADP_DEFAULT_ORG_ID)
ADP_DEFAULT_ORG_ID="00000000-0000-4000-a000-000000000001"
ADP_DEFAULT_ORG_NAME="Free tier"
ADP_DEFAULT_SLUG="adp-default"

# Free-tier budget defaults
FREE_TIER_MONTHLY_BUDGET_USD=5
FREE_TIER_DAILY_RUNS=10
FREE_TIER_CONCURRENT_PODS=2

echo "=== Seeding adp-default tenant (env: ${ENVIRONMENT}) ==="

# ---------------------------------------------------------------------------
# Discover database connection
# ---------------------------------------------------------------------------

if [ -z "${BG_DATABASE_URL:-}" ]; then
    echo "BG_DATABASE_URL not set — attempting to discover from SSM..."
    RDS_HOST=$(aws ssm get-parameter \
        --name "/adp/${ENVIRONMENT}/gateway/rds-host" \
        --query "Parameter.Value" --output text 2>/dev/null || echo "")

    if [ -z "$RDS_HOST" ]; then
        echo "ERROR: Cannot discover RDS host. Set BG_DATABASE_URL or ensure SSM param exists."
        exit 1
    fi

    RDS_PORT=$(aws ssm get-parameter \
        --name "/adp/${ENVIRONMENT}/gateway/rds-port" \
        --query "Parameter.Value" --output text 2>/dev/null || echo "5432")
    RDS_DBNAME=$(aws ssm get-parameter \
        --name "/adp/${ENVIRONMENT}/gateway/rds-dbname" \
        --query "Parameter.Value" --output text 2>/dev/null || echo "bedrockgateway")
    RDS_USER=$(aws ssm get-parameter \
        --name "/adp/${ENVIRONMENT}/gateway/rds-username" \
        --query "Parameter.Value" --output text 2>/dev/null || echo "bgadmin")

    # Generate IAM auth token
    RDS_TOKEN=$(aws rds generate-db-auth-token \
        --hostname "$RDS_HOST" \
        --port "$RDS_PORT" \
        --username "$RDS_USER" \
        --region "${AWS_REGION:-us-east-1}")

    export PGHOST="$RDS_HOST"
    export PGPORT="$RDS_PORT"
    export PGDATABASE="$RDS_DBNAME"
    export PGUSER="$RDS_USER"
    export PGPASSWORD="$RDS_TOKEN"
    export PGSSLMODE="require"
else
    echo "Using BG_DATABASE_URL from environment."
    # Parse connection string for psql
    export DATABASE_URL="$BG_DATABASE_URL"
fi

# ---------------------------------------------------------------------------
# SQL statements (idempotent via ON CONFLICT DO NOTHING)
# ---------------------------------------------------------------------------

SQL=$(cat <<EOF
-- Seed adp-default organization
INSERT INTO organizations (id, name, aws_accounts, role_mappings, settings, github_installation_ids, cognito_client_ids, created_at)
VALUES (
    '${ADP_DEFAULT_ORG_ID}',
    '${ADP_DEFAULT_ORG_NAME}',
    '[]'::jsonb,
    '{}'::jsonb,
    '{"tier": "free", "slug": "${ADP_DEFAULT_SLUG}", "monthly_budget_usd": ${FREE_TIER_MONTHLY_BUDGET_USD}, "daily_runs_per_user": ${FREE_TIER_DAILY_RUNS}, "concurrent_pods_per_user": ${FREE_TIER_CONCURRENT_PODS}}'::jsonb,
    '[]'::jsonb,
    '[]'::jsonb,
    NOW()
)
ON CONFLICT (id) DO NOTHING;

-- Verify
SELECT id, name, settings->>'tier' AS tier FROM organizations WHERE id = '${ADP_DEFAULT_ORG_ID}';
EOF
)

# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------

run_sql() {
    if command -v psql &>/dev/null && [ -n "${PGHOST:-}" ]; then
        echo "$SQL" | psql --no-psqlrc -q
    elif [ -n "${DATABASE_URL:-}" ]; then
        echo "$SQL" | psql --no-psqlrc -q "$DATABASE_URL"
    else
        # Fallback: kubectl exec into gateway pod
        echo "No direct DB access — using kubectl exec..."
        POD=$(kubectl get pod -n adp-gateway -l app=bedrockgateway -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
        if [ -z "$POD" ]; then
            echo "ERROR: Cannot find gateway pod for kubectl exec. Ensure kubectl is configured."
            exit 1
        fi
        echo "$SQL" | kubectl exec -n adp-gateway "$POD" -i -- \
            python3 -c "
import sys, asyncio
from src.shared.database import get_engine
from sqlalchemy import text

async def run():
    engine = get_engine()
    async with engine.begin() as conn:
        for stmt in sys.stdin.read().split(';'):
            stmt = stmt.strip()
            if stmt:
                result = await conn.execute(text(stmt))
                if result.returns_rows:
                    for row in result:
                        print(dict(row._mapping))

asyncio.run(run())
"
    fi
}

run_sql

echo ""
echo "=== adp-default tenant seeded successfully ==="
echo "  Org ID:   ${ADP_DEFAULT_ORG_ID}"
echo "  Name:     ${ADP_DEFAULT_ORG_NAME}"
echo "  Tier:     free"
echo "  Budget:   \$${FREE_TIER_MONTHLY_BUDGET_USD}/month per user"
echo "  Runs:     ${FREE_TIER_DAILY_RUNS}/day per user"
echo "  Pods:     ${FREE_TIER_CONCURRENT_PODS} concurrent per user"
