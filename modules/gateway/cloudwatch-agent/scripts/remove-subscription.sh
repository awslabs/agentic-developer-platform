#!/bin/bash
set -euo pipefail

# Remove CloudWatch subscription filter from a log group
# Usage: ./remove-subscription.sh <log-group-name>

if [ $# -lt 1 ]; then
    echo "Usage: $0 <log-group-name>"
    exit 1
fi

LOG_GROUP=$1
FILTER_NAME="cloudwatch-agent-$(echo "$LOG_GROUP" | tr '/' '-' | sed 's/^-//')"

echo "Removing subscription filter: $FILTER_NAME"

aws logs delete-subscription-filter \
    --log-group-name "$LOG_GROUP" \
    --filter-name "$FILTER_NAME" 2>/dev/null || echo "Filter not found or already removed"

echo "✅ Subscription removed"
