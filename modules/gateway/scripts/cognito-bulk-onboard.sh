#!/bin/bash
set -euo pipefail

# ============================================================================
# Cognito Bulk User Onboarding
#
# Reads a CSV file and creates users in Cognito with custom attributes.
# Users receive an email invitation with a temporary password.
#
# Usage:
#   ./scripts/cognito-bulk-onboard.sh <user-pool-id> <csv-file>
#   ./scripts/cognito-bulk-onboard.sh us-east-1_XXXXX users.csv
#   ./scripts/cognito-bulk-onboard.sh us-east-1_XXXXX users.csv --dry-run
#
# CSV format (header required):
#   email,name,github_username,org_id,department_id,team_id,role
#   jane@acme.com,Jane Smith,jane-acme,acme,engineering,platform,developer
#   bob@acme.com,Bob Jones,bob-jones,acme,engineering,data,admin
#
# Roles: admin, developer, team-lead, viewer
# ============================================================================

if [ $# -lt 2 ]; then
  echo "Usage: $0 <user-pool-id> <csv-file> [--dry-run]"
  echo ""
  echo "CSV columns: email,name,github_username,org_id,department_id,team_id,role"
  exit 1
fi

USER_POOL_ID="$1"
CSV_FILE="$2"
DRY_RUN="${3:-}"
AWS_REGION="${AWS_REGION:-us-east-1}"

if [ ! -f "$CSV_FILE" ]; then
  echo "❌ File not found: $CSV_FILE"
  exit 1
fi

# Validate user pool exists
echo "Validating user pool: $USER_POOL_ID"
POOL_NAME=$(aws cognito-idp describe-user-pool \
  --user-pool-id "$USER_POOL_ID" \
  --query "UserPool.Name" --output text \
  --region "$AWS_REGION" 2>/dev/null) || {
  echo "❌ User pool $USER_POOL_ID not found in $AWS_REGION"
  exit 1
}
echo "✅ User pool: $POOL_NAME ($USER_POOL_ID)"
echo ""

TOTAL=0
CREATED=0
SKIPPED=0
FAILED=0
ERRORS=""

# Read CSV, skip header
HEADER_SKIPPED=false
while IFS=',' read -r email name github_username org_id department_id team_id role; do
  # Skip header row
  if [ "$HEADER_SKIPPED" = false ]; then
    HEADER_SKIPPED=true
    # Validate header
    if [[ "$email" != "email" ]]; then
      echo "⚠️  CSV doesn't appear to have a header row. Expected: email,name,github_username,org_id,department_id,team_id,role"
      echo "   First row: $email,$name,$github_username,$org_id,$department_id,$team_id,$role"
      echo "   Treating first row as data..."
      HEADER_SKIPPED=true
    else
      continue
    fi
  fi

  # Trim whitespace
  email=$(echo "$email" | xargs)
  name=$(echo "$name" | xargs)
  github_username=$(echo "$github_username" | xargs)
  org_id=$(echo "$org_id" | xargs)
  department_id=$(echo "$department_id" | xargs)
  team_id=$(echo "$team_id" | xargs)
  role=$(echo "$role" | xargs)

  TOTAL=$((TOTAL + 1))

  # Validate required fields
  if [ -z "$email" ] || [ -z "$name" ]; then
    echo "⚠️  Row $TOTAL: Skipping — email and name are required"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  # Validate role
  case "$role" in
    admin|developer|team-lead|viewer) ;;
    "")
      role="viewer"
      echo "   Row $TOTAL: No role specified, defaulting to 'viewer'"
      ;;
    *)
      echo "⚠️  Row $TOTAL: Invalid role '$role' — must be admin|developer|team-lead|viewer. Defaulting to 'viewer'"
      role="viewer"
      ;;
  esac

  # Check if user already exists
  EXISTING=$(aws cognito-idp list-users \
    --user-pool-id "$USER_POOL_ID" \
    --filter "email = \"$email\"" \
    --query "Users[0].Username" --output text \
    --region "$AWS_REGION" 2>/dev/null) || EXISTING=""

  if [ -n "$EXISTING" ] && [ "$EXISTING" != "None" ]; then
    echo "⏭️  $email — already exists (username: $EXISTING)"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  # Build attributes
  ATTRS="Name=email,Value=$email Name=email_verified,Value=true Name=name,Value=\"$name\""
  [ -n "$github_username" ] && ATTRS="$ATTRS Name=custom:github_username,Value=$github_username"
  [ -n "$org_id" ]          && ATTRS="$ATTRS Name=custom:org_id,Value=$org_id"
  [ -n "$department_id" ]   && ATTRS="$ATTRS Name=custom:department_id,Value=$department_id"
  [ -n "$team_id" ]         && ATTRS="$ATTRS Name=custom:team_id,Value=$team_id"
  [ -n "$role" ]            && ATTRS="$ATTRS Name=custom:role,Value=$role"

  if [ "$DRY_RUN" = "--dry-run" ]; then
    echo "🔍 [DRY RUN] Would create: $email ($name) github=$github_username org=$org_id team=$team_id role=$role"
    CREATED=$((CREATED + 1))
    continue
  fi

  # Create user
  echo -n "➕ Creating $email ($name) github=$github_username org=$org_id team=$team_id role=$role ... "
  if eval aws cognito-idp admin-create-user \
    --user-pool-id "$USER_POOL_ID" \
    --username "$email" \
    --user-attributes $ATTRS \
    --desired-delivery-mediums EMAIL \
    --region "$AWS_REGION" \
    --output text --query "User.Username" 2>/tmp/cognito-error.txt; then
    echo "✅"
    CREATED=$((CREATED + 1))
  else
    ERROR_MSG=$(cat /tmp/cognito-error.txt)
    echo "❌"
    echo "   Error: $ERROR_MSG"
    FAILED=$((FAILED + 1))
    ERRORS="$ERRORS\n  $email: $ERROR_MSG"
  fi

done < "$CSV_FILE"

# Summary
echo ""
echo "============================================"
echo "  ONBOARDING SUMMARY"
echo "============================================"
echo "  Total rows:  $TOTAL"
echo "  Created:     $CREATED"
echo "  Skipped:     $SKIPPED (already exist or invalid)"
echo "  Failed:      $FAILED"
if [ -n "$ERRORS" ]; then
  echo ""
  echo "  Errors:"
  echo -e "$ERRORS"
fi
echo "============================================"

if [ "$DRY_RUN" = "--dry-run" ]; then
  echo ""
  echo "This was a dry run. No users were created."
  echo "Remove --dry-run to create users."
fi
