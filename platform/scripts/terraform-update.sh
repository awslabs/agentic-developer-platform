#!/usr/bin/env bash
# Shared saved-plan gate for platform and delegated webhook upgrades.
_UPDATE_PLAN_HELPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

terraform_update_apply() {
  local MODULE_NAME="$1"
  local VAR_FILE="$2"
  shift 2

  # 1. Plan to a file (captures the plan for inspection)
  local PLAN_DIR
  PLAN_DIR=$(mktemp -d "${TMPDIR:-/tmp}/adp-plan-${MODULE_NAME}.XXXXXX")
  local PLAN_FILE="$PLAN_DIR/plan.tfplan"
  local PLAN_OUTPUT="$PLAN_DIR/plan.txt"
  local PLAN_JSON="$PLAN_DIR/plan.json"

  # Capture detailed-exitcode without letting errexit skip the safety gate.
  local EXIT_CODE=0
  terraform plan -var-file="$VAR_FILE" ${1+"$@"} \
    -out="$PLAN_FILE" -input=false -detailed-exitcode -no-color \
    >"$PLAN_OUTPUT" 2>&1 || EXIT_CODE=$?
  cat "$PLAN_OUTPUT"

  # Exit code: 0 = no changes, 1 = error, 2 = changes present
  if [ "$EXIT_CODE" -eq 0 ]; then
    ok "$MODULE_NAME: no changes"
    rm -f "$PLAN_FILE" "$PLAN_OUTPUT" "$PLAN_JSON"
    rmdir "$PLAN_DIR"
    return 0
  elif [ "$EXIT_CODE" -ne 2 ]; then
    fail "$MODULE_NAME: terraform plan failed (exit $EXIT_CODE; log: $PLAN_OUTPUT)"
  fi

  # JSON actions include delete for BOTH replacement orders and plain destroys.
  # Never interpret an unreadable plan as permission to apply.
  terraform show -json "$PLAN_FILE" >"$PLAN_JSON" \
    || fail "$MODULE_NAME: cannot inspect saved plan ($PLAN_FILE)"
  local DESTROYS
  DESTROYS=$(python3 "$_UPDATE_PLAN_HELPER_DIR/plan-delete-actions.py" "$PLAN_JSON") \
    || fail "$MODULE_NAME: invalid saved plan ($PLAN_FILE)"
  if [ -n "$DESTROYS" ]; then
    echo ""
    echo "DESTROY GATE: $MODULE_NAME includes deletes/replacements:"
    echo "$DESTROYS"
    if [ "${CONFIRM_DESTRUCTIVE:-false}" = true ]; then
      warn "Operator confirmed destructive apply (--confirm-destructive). Proceeding."
    else
      fail "Refusing destructive $MODULE_NAME plan. Inspect $PLAN_OUTPUT and $PLAN_FILE before confirming."
    fi
  fi

  # 3. Apply the saved plan (no -auto-approve needed — plan file is pre-approved)
  terraform apply "$PLAN_FILE"
  ok "$MODULE_NAME: applied successfully"

  # Cleanup
  rm -f "$PLAN_FILE" "$PLAN_OUTPUT" "$PLAN_JSON"
  rmdir "$PLAN_DIR"
}
