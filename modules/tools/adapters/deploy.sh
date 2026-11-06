#!/usr/bin/env bash
# Called by the native release workflow after its server-authorized job guard.
# Keep the adapter's renderer dependencies out of the gateway and host Python.
set -euo pipefail
[[ $# == 1 && "$1" =~ ^(publish|apply|verify|configure|receipt|foundation|identity|dispatch-context|prepare|rollback-plan|handoff-context|handoff-receipt|handoff|rollback)$ ]] || {
  echo 'Usage: modules/tools/adapters/deploy.sh publish|apply|verify|configure|receipt|foundation|identity|dispatch-context|prepare|rollback-plan|handoff-context|handoff-receipt|handoff|rollback' >&2
  exit 2
}
adapter_root=$(cd "$(dirname "$0")" && pwd)
umask 077
adapter_temp=$(mktemp -d "${RUNNER_TEMP:-/tmp}/adp-adapter-runtime.XXXXXX")
trap 'rm -rf "$adapter_temp"' EXIT
unset PYTHONPATH PYTHONHOME
python3 -m venv "$adapter_temp/bootstrap"
"$adapter_temp/bootstrap/bin/python" -m pip --quiet install 'uv==0.12.23'
export UV_PROJECT_ENVIRONMENT="$adapter_temp/runtime"
"$adapter_temp/bootstrap/bin/uv" sync --project "$adapter_root" --frozen --no-dev --python 3.12
case "$1" in
  rollback-plan)
    "$adapter_temp/runtime/bin/python" "$adapter_root/prepare_handoff.py" --rollback \
      --config "$ADP_TOOLS_ADAPTER_PREPARATION_FILE" --output "$ADP_TOOLS_ADAPTER_HANDOFF_FILE"
    ;;
  handoff-context)
    "$adapter_temp/runtime/bin/python" "$adapter_root/prepare_handoff.py" --dispatch-context \
      --config "$ADP_TOOLS_ADAPTER_PREPARATION_FILE" --output "$ADP_TOOLS_ADAPTER_HANDOFF_CONTEXT"
    ;;
  handoff-receipt)
    "$adapter_temp/runtime/bin/python" "$adapter_root/handoff_delivery.py" \
      --result "$ADP_TOOLS_ADAPTER_HANDOFF_RESULT" --output "$ADP_TOOLS_ADAPTER_RELEASE_DIR"
    ;;
  prepare)
    "$adapter_temp/runtime/bin/python" "$adapter_root/prepare_handoff.py" \
      --config "$ADP_TOOLS_ADAPTER_PREPARATION_FILE" --output "$ADP_TOOLS_ADAPTER_HANDOFF_FILE"
    ;;
  handoff|rollback)
    "$adapter_temp/runtime/bin/python" "$adapter_root/handoff_release.py" "$1"
    ;;
  foundation)
    "$adapter_temp/runtime/bin/python" "$adapter_root/foundation_release.py"
    ;;
  identity)
    "$adapter_temp/runtime/bin/python" "$adapter_root/identity_release.py"
    ;;
  dispatch-context)
    "$adapter_temp/runtime/bin/python" "$adapter_root/identity_dispatch_context.py" \
      --output "$ADP_TOOLS_ADAPTER_RELEASE_DIR.dispatch-context.json"
    ;;
  configure)
    "$adapter_temp/runtime/bin/python" "$adapter_root/stage_configuration.py" \
      --publication "$ADP_TOOLS_ADAPTER_PUBLICATION_FILE" --output "$ADP_TOOLS_ADAPTER_CONFIG_FILE"
    ;;
  receipt)
    "$adapter_temp/runtime/bin/python" "$adapter_root/release_evidence.py" --output "$ADP_TOOLS_ADAPTER_RELEASE_DIR"
    ;;
  *) "$adapter_temp/runtime/bin/python" "$adapter_root/deploy.py" "$1" ;;
esac
