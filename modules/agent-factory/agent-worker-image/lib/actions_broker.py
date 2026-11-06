"""Bounded worker-side bridge to the gateway's typed Actions endpoints."""

import json
import sys

from .mediated_github import MediatedError, _request

PATHS = {
    "actions_dispatch": "/internal/v1/agent/actions/dispatch",
    "actions_rerun": "/internal/v1/agent/actions/rerun",
    "actions_runs": "/internal/v1/agent/actions/runs",
    "actions_run": "/internal/v1/agent/actions/run",
    "actions_logs": "/internal/v1/agent/actions/logs",
}

# Only engine-owned prerequisites with a concrete recovery step are surfaced.
# This table is also the transport allowlist; no response text is echoed.
CONFLICT_ACTIONS = {
    "operations_repair_merge_required": (
        "Read repair_tasks for earlier attempts, then repair_status for each pending PR. "
        "Resolve failed checks or use repair_wait, and repair_merge the exact checked head before dispatching."
    ),
    "delivery_target_busy": "Wait for the existing deployment to finish, then retry the accepted workflow.",
    "delivery_dispatch_reconciliation_required": (
        "Read actions_runs and actions_run to observe the correlated workflow before attempting another dispatch."
    ),
    "workflow_definition_changed_on_dispatch_branch": (
        "Inspect the accepted workflow and repair history. Reconcile the workflow through the scoped repair path before dispatching."
    ),
    "correlated_workflow_inputs_are_unavailable": (
        "Inspect the accepted workflow's correlation inputs and repair the workflow through the scoped repair path."
    ),
    "accepted_delivery_inputs_are_unavailable": (
        "Inspect the accepted workflow input definitions and repair them through the scoped repair path."
    ),
}


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in PATHS:
        raise SystemExit("unsupported Actions operation")
    body = sys.stdin.read(8193)
    if len(body) > 8192:
        raise SystemExit("Actions request exceeds limit")
    try:
        result = _request(json.loads(body), path=PATHS[sys.argv[1]], conflict_codes=frozenset(CONFLICT_ACTIONS))
    except MediatedError as exc:
        # These exceptions already contain bounded transport categories, never
        # response bodies or credentials. Preserve them for agent recovery.
        result = {"error": str(exc), "error_code": type(exc).__name__}
        if str(exc) in CONFLICT_ACTIONS:
            result.update(reason_code=str(exc), next_action=CONFLICT_ACTIONS[str(exc)])
    print(json.dumps(result))


if __name__ == "__main__":
    main()
