#!/bin/bash
set -euo pipefail

# =============================================================================
# force-delete-secrets.sh — Force-delete Secrets Manager secrets by prefix
# =============================================================================
# Finds secrets matching the given prefixes and force-deletes them (no 7-day
# recovery window). This prevents re-deploy name collisions.
#
# SAFETY: Explicitly NEVER touches:
#   - adp/gh-app-*       (GitHub App credentials — survive by design)
#   - adp/*/gh-app-*     (GitHub App credentials — org-scoped pattern)
#   - adp/*/github-app/* (GitHub App credentials — webhook-ingress module)
#   - adp-terraform-*    (Terraform state backend)
#   - rds!*              (AWS-managed RDS secrets — let AWS handle lifecycle)
#
# Usage:
#   ./force-delete-secrets.sh "bedrockgw-dev-" "adp/dev/gateway/test-"
#   ./force-delete-secrets.sh --dry-run "bedrockgw-dev-"
#
# Idempotent: no-op if no matching secrets found.
# =============================================================================

DRY_RUN=false
PREFIXES=()

for arg in "$@"; do
  case $arg in
    --dry-run) DRY_RUN=true ;;
    --help)
      echo "Usage: $0 [--dry-run] <prefix> [prefix ...]"
      echo "Force-deletes Secrets Manager secrets matching the given prefixes."
      echo "Protected prefixes (never deleted): adp/gh-app-*, adp/*/gh-app-*, adp/*/github-app/*, adp-terraform-*, rds!*"
      exit 0
      ;;
    *) PREFIXES+=("$arg") ;;
  esac
done

if [ ${#PREFIXES[@]} -eq 0 ]; then
  echo "Usage: $0 [--dry-run] <prefix> [prefix ...]"
  exit 1
fi

AWS_REGION="${AWS_REGION:-us-east-1}"
DELETED=0
SKIPPED=0
PROTECTED=0

# Protected patterns — these are NEVER deleted regardless of prefix match
is_protected() {
  local name="$1"
  # GitHub App credentials (legacy org-scoped pattern)
  if [[ "$name" == adp/gh-app-* ]] || [[ "$name" == adp/*/gh-app-* ]]; then
    return 0
  fi
  # GitHub App credentials (webhook-ingress module pattern)
  if [[ "$name" == adp/*/github-app/* ]]; then
    return 0
  fi
  # Terraform state backend
  if [[ "$name" == adp-terraform-* ]]; then
    return 0
  fi
  # AWS-managed RDS secrets
  if [[ "$name" == rds!* ]]; then
    return 0
  fi
  return 1
}

echo "=== Force-delete Secrets Manager secrets ==="
echo "Prefixes: ${PREFIXES[*]}"
echo "Region: $AWS_REGION"
[ "$DRY_RUN" = true ] && echo "MODE: DRY RUN (no deletions)"
echo ""

for PREFIX in "${PREFIXES[@]}"; do
  echo "--- Searching for secrets matching prefix: '$PREFIX' ---"

  # List secrets matching this prefix
  SECRETS=$(aws secretsmanager list-secrets \
    --filter "Key=name,Values=$PREFIX" \
    --query 'SecretList[].Name' \
    --output text \
    --region "$AWS_REGION" 2>/dev/null || echo "")

  if [ -z "$SECRETS" ] || [ "$SECRETS" = "None" ]; then
    echo "  No secrets found matching '$PREFIX'."
    continue
  fi

  for SECRET_NAME in $SECRETS; do
    # Safety check: skip protected secrets
    if is_protected "$SECRET_NAME"; then
      echo "  PROTECTED (skipping): $SECRET_NAME"
      PROTECTED=$((PROTECTED + 1))
      continue
    fi

    if [ "$DRY_RUN" = true ]; then
      echo "  WOULD DELETE: $SECRET_NAME"
      SKIPPED=$((SKIPPED + 1))
    else
      echo "  Deleting: $SECRET_NAME"
      if aws secretsmanager delete-secret \
          --secret-id "$SECRET_NAME" \
          --force-delete-without-recovery \
          --region "$AWS_REGION" > /dev/null 2>&1; then
        DELETED=$((DELETED + 1))
      else
        echo "  WARNING: Failed to delete '$SECRET_NAME'. It may already be deleted."
        SKIPPED=$((SKIPPED + 1))
      fi
    fi
  done
done

echo ""
echo "=== Summary ==="
echo "Deleted:   $DELETED"
echo "Protected: $PROTECTED"
echo "Skipped:   $SKIPPED"
