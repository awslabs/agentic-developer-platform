#!/bin/bash
# Shared phase entry points for managed workflows. The Python engine validates
# target/state and refuses to delete providers while an omitted consumer remains.
# Prefer undeploy.sh for a complete run: it validates all plans before mutation.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "Source this phase library or run platform/scripts/undeploy.sh" >&2
  exit 1
fi
_undeploy_phase() {
  local phase="$1"
  local root="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
  python3 "$root/platform/scripts/teardown.py" --root "$root" \
    --environment "${ENVIRONMENT:?ENVIRONMENT is required}" \
    --region "${AWS_REGION:?AWS_REGION is required}" --phase "$phase"
}
phase_superplane() { _undeploy_phase superplane; }
phase_agent_context() { _undeploy_phase agent_context; }
phase_agent_factory() { _undeploy_phase agent_factory; }
phase_webhook_ingress() { _undeploy_phase webhook_ingress; }
phase_gateway() { _undeploy_phase gateway; }
phase_platform() { _undeploy_phase platform; }
