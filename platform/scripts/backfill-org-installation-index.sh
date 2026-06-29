#!/usr/bin/env bash
# backfill-org-installation-index.sh — Issue #2336
#
# One-time backfill: writes reverse-lookup rows (org_installation → installation_id)
# to the identity-index DynamoDB table for existing tenants.
#
# These rows enable the EventBridge and agent-trigger handlers to resolve a real
# installation_id instead of hardcoding 0 (which crashes the worker).
#
# Usage:
#   ENVIRONMENT=dev ./platform/scripts/backfill-org-installation-index.sh
#
# The script is idempotent — safe to run multiple times.

set -euo pipefail

ENVIRONMENT="${ENVIRONMENT:-dev}"
TABLE_NAME="adp-${ENVIRONMENT}-identity-index"
REGION="${AWS_REGION:-us-east-1}"
NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

echo "=== Backfill org_installation reverse-lookup rows ==="
echo "Table: ${TABLE_NAME}"
echo "Region: ${REGION}"
echo "Environment: ${ENVIRONMENT}"
echo ""

# Discover existing installations from the identity-index table.
# Query all items with identity_type=github_installation_id to find
# the current org→installation mappings.
echo "Scanning for existing github_installation_id rows..."

ITEMS=$(aws dynamodb scan \
    --table-name "${TABLE_NAME}" \
    --region "${REGION}" \
    --filter-expression "identity_type = :it" \
    --expression-attribute-values '{":it": {"S": "github_installation_id"}}' \
    --projection-expression "identity_value, org_id" \
    --output json 2>/dev/null || echo '{"Items": []}')

COUNT=$(echo "${ITEMS}" | python3 -c "
import json, sys
data = json.load(sys.stdin)
items = data.get('Items', [])
print(len(items))
")

echo "Found ${COUNT} existing installation mapping(s)."
echo ""

if [ "${COUNT}" -eq 0 ]; then
    echo "No existing installations found. Nothing to backfill."
    echo "Reverse-lookup rows will be written automatically when the next"
    echo "GitHub webhook arrives (via _auto_register_installation)."
    exit 0
fi

# Write reverse-lookup rows for each discovered installation
echo "${ITEMS}" | python3 -c "
import json, sys

data = json.load(sys.stdin)
items = data.get('Items', [])

for item in items:
    installation_id = item.get('identity_value', {}).get('S', '')
    org_id = item.get('org_id', {}).get('S', '')
    if installation_id and org_id:
        print(f'{org_id}\t{installation_id}')
" | while IFS=$'\t' read -r ORG_ID INSTALLATION_ID; do
    echo "  Writing: org_installation/${ORG_ID} → installation_id=${INSTALLATION_ID}"
    aws dynamodb put-item \
        --table-name "${TABLE_NAME}" \
        --region "${REGION}" \
        --item "{
            \"identity_type\": {\"S\": \"org_installation\"},
            \"identity_value\": {\"S\": \"${ORG_ID}\"},
            \"installation_id\": {\"N\": \"${INSTALLATION_ID}\"},
            \"updated_at\": {\"S\": \"${NOW}\"},
            \"auto_registered\": {\"BOOL\": true}
        }" \
        --return-consumed-capacity NONE \
        --no-cli-pager
done

echo ""
echo "=== Backfill complete ==="
echo "Reverse-lookup rows written. EventBridge and agent-trigger dispatches"
echo "can now resolve installation_id for these tenants."
