#!/usr/bin/env bash
# Sourced by deploy-all.sh after identity validation, before cloud mutations.
deploy_checkpoint() {
  python3 "$SCRIPT_DIR/deploy-checkpoints.py" "$DEPLOY_CHECKPOINT_FILE" "$@"
}
deploy_checkpoint_init() {
  command -v flock >/dev/null || fail "Deployment checkpoints require flock (util-linux)"
  mkdir -p "$ROOT_DIR/.adp-deploy-checkpoints"
  DEPLOY_CHECKPOINT_FILE="$ROOT_DIR/.adp-deploy-checkpoints/${ACCOUNT_ID}-${AWS_REGION}-${ENVIRONMENT}.json"
  exec {DEPLOY_LOCK_FD}>"$DEPLOY_CHECKPOINT_FILE.lock"
  flock -n "$DEPLOY_LOCK_FD" || fail "Another deployment holds this target's checkpoint lock"
  # Materialize account placeholders before fingerprinting. The same idempotent
  # preparation runs under the checkpoint lock on both first runs and resumes.
  python3 "$SCRIPT_DIR/prepare-backends.py" "$ROOT_DIR/environments/$ENVIRONMENT" "$ACCOUNT_ID" \
    || fail "Cannot prepare environment backend configuration"
  local flags=()
  [ "$RESUME" = false ] || flags+=(--resume)
  [ -z "$FROM_PHASE" ] || flags+=(--from "$FROM_PHASE")
  # Confirmation and pricing recovery flags may change when retrying a failure.
  local options="$UPDATE_MODE:$LOCAL_MODE:$GATEWAY_ONLY:$AGENT_FACTORY_ONLY:$AGENT_CONTEXT_ONLY:$SKIP_AGENT_CONTEXT:$AGENT_CONTEXT_ENABLED:$SKIP_FRONTEND:$SKIP_BROKER:$SKIP_ADMIN_BOOTSTRAP:$SKIP_WEBHOOK_INGRESS"
  deploy_checkpoint init --root "$ROOT_DIR" --account "$ACCOUNT_ID" --region "$AWS_REGION" \
    --environment "$ENVIRONMENT" --source "$(git -C "$ROOT_DIR" rev-parse HEAD)" \
    --options "$options" "${flags[@]}" || fail "Cannot initialize deployment checkpoints"
  UPGRADE_REUSE_CONTEXT=false
  if [ "$UPDATE_MODE" = true ]; then
    if [ "$RESUME" = true ] && [ -f "$DEPLOY_CHECKPOINT_FILE.upgrade-path" ]; then
      UPGRADE_RUN_DIR=$(cat "$DEPLOY_CHECKPOINT_FILE.upgrade-path")
      [ -f "$UPGRADE_RUN_DIR/context.env" ] && [ -f "$UPGRADE_RUN_DIR/integration-before.json" ] \
        || fail "Original upgrade evidence is missing; cannot safely resume"
      export UPGRADE_RUN_DIR
      UPGRADE_REUSE_CONTEXT=true
    elif [ "$RESUME" = false ]; then
      rm -f "$DEPLOY_CHECKPOINT_FILE.upgrade-path"
    fi
  fi
  DEPLOY_ACTIVE_PHASE=""
  trap 'deploy_checkpoint_exit $?' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
}
deploy_checkpoint_exit() {
  local result="$1"
  if [ -n "${DEPLOY_ACTIVE_PHASE:-}" ]; then
    deploy_checkpoint failed "$DEPLOY_ACTIVE_PHASE" || true
  fi
  return "$result"
}
deploy_phase_begin() {
  local status
  status=$(deploy_checkpoint status "$1") || exit 1
  if [ "$status" = complete ]; then
    echo "Resuming: $1 already complete"
    return 1
  fi
  DEPLOY_ACTIVE_PHASE="$1"
  deploy_checkpoint running "$1" || exit 1
}
deploy_phase_complete() {
  deploy_checkpoint complete "$DEPLOY_ACTIVE_PHASE"
  DEPLOY_ACTIVE_PHASE=""
}
