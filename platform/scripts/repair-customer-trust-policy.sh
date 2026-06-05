#!/bin/bash
#
# repair-customer-trust-policy.sh
#
# Repairs a stale cross-account trust policy on a customer's ADP-Agent-* role.
#
# When the gateway IRSA role in the platform account is recreated (e.g., during
# a terraform replace), its internal AWS principal ID changes. Trust policies in
# customer accounts that reference the old role by ARN become "orphaned" — the
# ARN looks correct but AWS can't match the new role's ID to the stored reference.
#
# This script updates the trust policy on the customer role to use the
# account-root + condition pattern, which is resilient to role recreation.
# It must be run with credentials that have iam:GetRole + iam:UpdateAssumeRolePolicy
# in the CUSTOMER account.
#
# Usage:
#   # With direct creds for the customer account:
#   export AWS_PROFILE=customer-admin
#   ./platform/scripts/repair-customer-trust-policy.sh ADP-Agent-Dep-testing
#
#   # With specific gateway role ARN (if different from default):
#   GATEWAY_ROLE_ARN=arn:aws:iam::<platform-account-id>:role/adp-dev-role-gateway-service \
#     ./platform/scripts/repair-customer-trust-policy.sh ADP-Agent-Dep-testing
#
# Prerequisites:
#   - AWS CLI configured with credentials for the CUSTOMER account
#   - The caller must have iam:GetRole and iam:UpdateAssumeRolePolicy permissions
#   - The ExternalId and UserSessionTag values from the original vault credential

set -euo pipefail

ROLE_NAME="${1:-}"
if [ -z "$ROLE_NAME" ]; then
  echo "Usage: $0 <role-name> [--gateway-role-arn ARN] [--external-id ID] [--user-session-tag TAG]"
  echo ""
  echo "Example: $0 ADP-Agent-Dep-testing"
  echo ""
  echo "Environment variables:"
  echo "  GATEWAY_ROLE_ARN     — ARN of the platform's gateway IRSA role"
  echo "                         (default: auto-detect from current trust policy)"
  echo "  GATEWAY_ACCOUNT_ID   — AWS account ID hosting the ADP platform"
  echo "                         (default: extracted from GATEWAY_ROLE_ARN)"
  exit 1
fi

echo "=== ADP Trust Policy Repair ==="
echo "Target role: $ROLE_NAME"
echo ""

# Step 1: Fetch current trust policy
echo "Fetching current trust policy..."
CURRENT_POLICY=$(aws iam get-role --role-name "$ROLE_NAME" --query 'Role.AssumeRolePolicyDocument' --output json 2>&1) || {
  echo "ERROR: Cannot read role '$ROLE_NAME'. Ensure you have iam:GetRole permission in the customer account."
  echo "  $CURRENT_POLICY"
  exit 1
}

echo "Current trust policy:"
echo "$CURRENT_POLICY" | python3 -m json.tool
echo ""

# Step 2: Extract the GatewayRolePrincipal from the existing trust policy
# Look for the AWS principal in the existing policy
EXISTING_PRINCIPAL=$(echo "$CURRENT_POLICY" | python3 -c "
import json, sys
policy = json.load(sys.stdin)
for stmt in policy.get('Statement', []):
    principal = stmt.get('Principal', {})
    aws_principal = principal.get('AWS', '')
    if isinstance(aws_principal, list):
        for p in aws_principal:
            if ':role/' in p:
                print(p)
                sys.exit(0)
    elif ':role/' in aws_principal:
        print(aws_principal)
        sys.exit(0)
print('')
" 2>/dev/null)

GATEWAY_ROLE_ARN="${GATEWAY_ROLE_ARN:-$EXISTING_PRINCIPAL}"
if [ -z "$GATEWAY_ROLE_ARN" ]; then
  echo "ERROR: Could not determine gateway role ARN."
  echo "  Set GATEWAY_ROLE_ARN environment variable explicitly."
  exit 1
fi

echo "Gateway role ARN: $GATEWAY_ROLE_ARN"

# Extract account ID from role ARN
GATEWAY_ACCOUNT_ID="${GATEWAY_ACCOUNT_ID:-}"
if [ -z "$GATEWAY_ACCOUNT_ID" ]; then
  GATEWAY_ACCOUNT_ID=$(echo "$GATEWAY_ROLE_ARN" | cut -d: -f5)
fi
echo "Gateway account ID: $GATEWAY_ACCOUNT_ID"

# Step 3: Extract ExternalId and UserSessionTag from existing conditions
EXTRACTED=$(echo "$CURRENT_POLICY" | python3 -c "
import json, sys
policy = json.load(sys.stdin)
external_id = ''
user_tag = ''
for stmt in policy.get('Statement', []):
    cond = stmt.get('Condition', {})
    se = cond.get('StringEquals', {})
    if 'sts:ExternalId' in se:
        external_id = se['sts:ExternalId']
    if 'aws:RequestTag/adp:user_id' in se:
        user_tag = se['aws:RequestTag/adp:user_id']
print(f'{external_id}|{user_tag}')
" 2>/dev/null)

EXTERNAL_ID=$(echo "$EXTRACTED" | cut -d'|' -f1)
USER_SESSION_TAG=$(echo "$EXTRACTED" | cut -d'|' -f2)

if [ -z "$EXTERNAL_ID" ]; then
  echo "WARNING: Could not extract ExternalId from existing trust policy."
  echo "  The repair will proceed but the ExternalId condition may be missing."
fi

if [ -z "$USER_SESSION_TAG" ]; then
  echo "WARNING: Could not extract UserSessionTag from existing trust policy."
  echo "  The repair will proceed but the user tag condition may be missing."
fi

echo "ExternalId: ${EXTERNAL_ID:-(not found)}"
echo "UserSessionTag: ${USER_SESSION_TAG:-(not found)}"
echo ""

# Step 4: Build the new trust policy using the resilient account-root + condition pattern
NEW_POLICY=$(python3 -c "
import json, sys

gateway_role_arn = '$GATEWAY_ROLE_ARN'
gateway_account_id = '$GATEWAY_ACCOUNT_ID'
external_id = '$EXTERNAL_ID'
user_session_tag = '$USER_SESSION_TAG'

# Build AssumeRole condition
assume_condition = {
    'StringEquals': {
        'aws:PrincipalArn': gateway_role_arn,
    }
}
if external_id:
    assume_condition['StringEquals']['sts:ExternalId'] = external_id
if user_session_tag:
    assume_condition['StringEquals']['aws:RequestTag/adp:user_id'] = user_session_tag

# Build TagSession condition
tag_condition = {
    'StringEquals': {
        'aws:PrincipalArn': gateway_role_arn,
    }
}
if user_session_tag:
    tag_condition['StringEquals']['aws:RequestTag/adp:user_id'] = user_session_tag

policy = {
    'Version': '2012-10-17',
    'Statement': [
        {
            'Effect': 'Allow',
            'Principal': {'AWS': f'arn:aws:iam::{gateway_account_id}:root'},
            'Action': 'sts:AssumeRole',
            'Condition': assume_condition,
        },
        {
            'Effect': 'Allow',
            'Principal': {'AWS': f'arn:aws:iam::{gateway_account_id}:root'},
            'Action': 'sts:TagSession',
            'Condition': tag_condition,
        },
    ]
}

print(json.dumps(policy))
")

echo "New trust policy (resilient pattern):"
echo "$NEW_POLICY" | python3 -m json.tool
echo ""

# Step 5: Confirm before applying
read -p "Apply this trust policy to role '$ROLE_NAME'? [y/N] " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
  echo "Aborted."
  exit 0
fi

# Step 6: Apply the new trust policy
echo "Updating trust policy..."
aws iam update-assume-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-document "$NEW_POLICY" || {
  echo "ERROR: Failed to update trust policy."
  exit 1
}

echo ""
echo "Trust policy updated successfully."
echo ""
echo "Verification: attempting to describe the role..."
aws iam get-role --role-name "$ROLE_NAME" --query 'Role.AssumeRolePolicyDocument' --output json | python3 -m json.tool
echo ""
echo "Done. The cross-account assume should now work even if the gateway role is recreated."
