#!/usr/bin/env bash
# Smoke-test: verify identity projection is healthy post-deploy.
# Issue #537: Identity projection redesign.
#
# Usage: ./scripts/verify-identity-projection.sh [env]
# Default env: dev

set -euo pipefail

ENV="${1:-dev}"
TABLE_NAME="adp-${ENV}-user-identity-index"
REGION="${AWS_REGION:-us-east-1}"

echo "=== Identity Projection Verification (env=${ENV}) ==="
echo ""

# 1. Assert new table exists and is ACTIVE
echo "1. Checking DDB table ${TABLE_NAME} status..."
STATUS=$(aws dynamodb describe-table --table-name "${TABLE_NAME}" --region "${REGION}" \
    --query 'Table.TableStatus' --output text 2>/dev/null || echo "NOT_FOUND")

if [ "${STATUS}" != "ACTIVE" ]; then
    echo "   FAIL: Table status is '${STATUS}' (expected ACTIVE)"
    exit 1
fi
echo "   OK: Table is ACTIVE"
echo ""

# 2. Assert row count matches Postgres (requires DATABASE_URL)
echo "2. Checking row counts..."
DDB_COUNT=$(aws dynamodb scan --table-name "${TABLE_NAME}" --region "${REGION}" \
    --select COUNT --query 'Count' --output text)
echo "   DDB table rows: ${DDB_COUNT}"

if [ -n "${DATABASE_URL:-}" ]; then
    PG_COUNT=$(psql "${DATABASE_URL}" -t -c "SELECT count(*) FROM user_identities;" | tr -d ' ')
    echo "   Postgres rows:  ${PG_COUNT}"
    if [ "${DDB_COUNT}" -lt "${PG_COUNT}" ]; then
        echo "   WARN: DDB count (${DDB_COUNT}) < Postgres count (${PG_COUNT})"
        echo "   Some rows may not have been backfilled yet."
    else
        echo "   OK: Counts match (or DDB has more due to TTL headroom)"
    fi
else
    echo "   SKIP: DATABASE_URL not set, cannot compare with Postgres"
fi
echo ""

# 3. Verify CloudWatch metric namespace exists
echo "3. Checking CloudWatch metric namespace..."
METRICS=$(aws cloudwatch list-metrics --namespace "ADP/IdentityResolver" --region "${REGION}" \
    --query 'Metrics[?MetricName==`CrossTenantMismatch`]' --output text 2>/dev/null || echo "")
if [ -n "${METRICS}" ]; then
    echo "   OK: CrossTenantMismatch metric exists in ADP/IdentityResolver namespace"
else
    echo "   INFO: CrossTenantMismatch metric not yet emitted (expected if no mismatches)"
fi
echo ""

# 4. Quick item sample (if table has items)
if [ "${DDB_COUNT}" -gt "0" ]; then
    echo "4. Sample item from DDB:"
    aws dynamodb scan --table-name "${TABLE_NAME}" --region "${REGION}" \
        --max-items 1 --query 'Items[0]' --output json
else
    echo "4. SKIP: Table is empty (backfill may not have run yet)"
fi
echo ""

echo "=== Verification complete ==="
