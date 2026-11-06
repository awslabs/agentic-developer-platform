#!/bin/bash
# Portable launcher: no Bash 4 arrays or flock dependency.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Select credentials before load-deploy-config.sh can resolve the AWS account.
# Match deploy.sh: an explicit profile overrides inherited credentials.
ARGS=()
SHOW_HELP=false
ENTRYPOINT="$SCRIPT_DIR/teardown.py"
APP_OPERATION=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --aws-profile)
      [ "$#" -ge 2 ] && [ -n "$2" ] && [[ "$2" != -* ]] || { echo "--aws-profile requires a profile name" >&2; exit 2; }
      export AWS_PROFILE="$2" AWS_DEFAULT_PROFILE="$2"
      unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN
      unset AWS_ROLE_ARN AWS_WEB_IDENTITY_TOKEN_FILE AWS_ROLE_SESSION_NAME
      shift 2 ;;
    --superplane-quiesce|--superplane-infrastructure|--superplane-workspace-prepare|--superplane-workspace-apply|--superplane-storage|--evaluator-fixtures)
      [ -z "$APP_OPERATION" ] || { echo "Choose one Superplane teardown operation" >&2; exit 2; }
      APP_OPERATION="$1"
      case "$1" in
        --superplane-quiesce) ENTRYPOINT="$SCRIPT_DIR/../../modules/domain-apps/superplane/scripts/quiesce_for_teardown.py" ;;
        --superplane-infrastructure) ENTRYPOINT="$SCRIPT_DIR/../../modules/domain-apps/superplane/scripts/teardown_infrastructure.py" ;;
        --superplane-workspace-prepare) ENTRYPOINT="$SCRIPT_DIR/../../modules/domain-apps/superplane/infra/workspaces/scripts/prepare_workspace_plan.py" ;;
        --superplane-workspace-apply) ENTRYPOINT="$SCRIPT_DIR/../../modules/domain-apps/superplane/infra/workspaces/scripts/apply_workspace_plan.py" ;;
        --superplane-storage) ENTRYPOINT="$SCRIPT_DIR/../../modules/domain-apps/superplane/scripts/teardown_storage.py" ;;
        --evaluator-fixtures) ENTRYPOINT="$SCRIPT_DIR/teardown_evaluator_fixtures.py" ;;
      esac
      shift ;;
    --help|-h) SHOW_HELP=true; ARGS+=("$1"); shift ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
case "$APP_OPERATION" in
  --superplane-workspace-prepare) ARGS+=("--destroy") ;;
  --superplane-workspace-apply) ARGS+=("--destroy-only") ;;
esac
# Help must work without credentials/configuration.
if [[ "$SHOW_HELP" == true ]]; then
  echo "Launcher option: --aws-profile PROFILE (same as deploy.sh; overrides inherited credentials)"
  echo "App-owned cleanup: --superplane-quiesce, --superplane-infrastructure, --superplane-workspace-prepare, --superplane-workspace-apply (add --help for selected operation)"
  echo "Retained storage/fixture cleanup: --superplane-storage, --evaluator-fixtures (add --help)"
  exec python3 "$ENTRYPOINT" ${ARGS[@]+"${ARGS[@]}"}
fi
source "$SCRIPT_DIR/load-deploy-config.sh"
exec python3 "$ENTRYPOINT" ${ARGS[@]+"${ARGS[@]}"}
