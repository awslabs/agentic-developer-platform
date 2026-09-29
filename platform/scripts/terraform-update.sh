#!/usr/bin/env bash
# Shared saved-plan gate for platform and delegated webhook upgrades.
_UPDATE_PLAN_HELPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Environment tfvars can contain operator-activated settings for a different
# account. A saved plan is too late to catch cross-account references in every
# policy, URL and ConfigMap, so reject them before planning an update.
terraform_update_var_file() {
  local DEFAULT_FILE="$1" EXPLICIT_FILE="${2:-}" TARGET_ACCOUNT="$3"
  python3 - "${EXPLICIT_FILE:-$DEFAULT_FILE}" "$TARGET_ACCOUNT" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1]).expanduser().resolve()
if not path.is_file():
    raise SystemExit(f"Upgrade tfvars file does not exist: {path}")
foreign = sorted(set(re.findall(r"(?<![0-9])[0-9]{12}(?![0-9])", path.read_text())) - {sys.argv[2]})
if foreign:
    raise SystemExit(f"Upgrade tfvars {path} references a different AWS account; provide target-specific tfvars")
print(path)
PY
}

terraform_update_apply() {
  local MODULE_NAME="$1"
  local VAR_FILE="$2"
  shift 2
  local CONTEXT_MODULE="$MODULE_NAME"
  case "$MODULE_NAME" in
    gateway-alb-wire|gateway-final|gateway-worker-authority) CONTEXT_MODULE=gateway ;;
    agent-factory-intake-managed) CONTEXT_MODULE=agent-factory ;;
  esac
  local CONTEXT_ARGS=()
  if [ -n "${UPGRADE_RUN_DIR:-}" ] && [ -f "$UPGRADE_RUN_DIR/$CONTEXT_MODULE.tfvars.json" ]; then
    python3 - "$UPGRADE_RUN_DIR/integration-before.json" "${ACCOUNT_ID:-}" <<'PY' || fail "Upgrade context belongs to a different account"
import json, sys
with open(sys.argv[1]) as source:
    assert json.load(source)["account"] == sys.argv[2], "Upgrade account mismatch"
PY
    CONTEXT_ARGS+=(-var-file="$UPGRADE_RUN_DIR/$CONTEXT_MODULE.tfvars.json")
  fi

  # Repository defaults/overlays precede the observed live context. Explicit
  # -var inputs (such as a selected release image) remain the final overrides.
  # Handle both Terraform spellings without splitting paths or values.
  local OVERLAY_ARGS=() PLAN_ARGS=()
  while [ "$#" -gt 0 ]; do
    case "$1" in
      -var-file=*) OVERLAY_ARGS+=("$1"); shift ;;
      -var-file)
        [ "$#" -ge 2 ] || fail "Missing -var-file value"
        OVERLAY_ARGS+=("$1" "$2"); shift 2 ;;
      *) PLAN_ARGS+=("$1"); shift ;;
    esac
  done

  # 1. Plan to a file (captures the plan for inspection)
  local PLAN_DIR
  PLAN_DIR=$(mktemp -d "${UPGRADE_RUN_DIR:-${TMPDIR:-/tmp}}/adp-plan-${MODULE_NAME}.XXXXXX")
  local PLAN_FILE="$PLAN_DIR/plan.tfplan"
  local PLAN_OUTPUT="$PLAN_DIR/plan.txt"
  local PLAN_JSON="$PLAN_DIR/plan.json"

  # Capture detailed-exitcode without letting errexit skip the safety gate.
  local EXIT_CODE=0
  terraform plan -var-file="$VAR_FILE" ${OVERLAY_ARGS[@]+"${OVERLAY_ARGS[@]}"} \
    ${CONTEXT_ARGS[@]+"${CONTEXT_ARGS[@]}"} ${PLAN_ARGS[@]+"${PLAN_ARGS[@]}"} \
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
  if [ "${UPGRADE_CHECK_ONLY:-false}" = true ]; then
    fail "$MODULE_NAME has remaining drift; inspect $PLAN_OUTPUT (no changes applied)"
  fi

  # JSON actions include delete for BOTH replacement orders and plain destroys.
  # Never interpret an unreadable plan as permission to apply.
  terraform show -json "$PLAN_FILE" >"$PLAN_JSON" \
    || fail "$MODULE_NAME: cannot inspect saved plan ($PLAN_FILE)"
  local DESTROYS
  DESTROYS=$(python3 "$_UPDATE_PLAN_HELPER_DIR/upgrade-plan-policy.py" "$PLAN_JSON" "$MODULE_NAME" "${ACCOUNT_ID:-}") \
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
