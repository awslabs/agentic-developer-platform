#!/bin/bash
# Portable launcher: no Bash 4 arrays or flock dependency.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Select credentials before load-deploy-config.sh can resolve the AWS account.
# Match deploy.sh: an explicit profile overrides inherited credentials.
ARGS=()
SHOW_HELP=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --aws-profile)
      [ "$#" -ge 2 ] && [ -n "$2" ] && [[ "$2" != -* ]] || { echo "--aws-profile requires a profile name" >&2; exit 2; }
      export AWS_PROFILE="$2" AWS_DEFAULT_PROFILE="$2"
      unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN
      unset AWS_ROLE_ARN AWS_WEB_IDENTITY_TOKEN_FILE AWS_ROLE_SESSION_NAME
      shift 2 ;;
    --help|-h) SHOW_HELP=true; ARGS+=("$1"); shift ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
# Help must work without credentials/configuration.
if [[ "$SHOW_HELP" == true ]]; then
  echo "Launcher option: --aws-profile PROFILE (same as deploy.sh; overrides inherited credentials)"
  exec python3 "$SCRIPT_DIR/teardown.py" ${ARGS[@]+"${ARGS[@]}"}
fi
source "$SCRIPT_DIR/load-deploy-config.sh"
exec python3 "$SCRIPT_DIR/teardown.py" ${ARGS[@]+"${ARGS[@]}"}
