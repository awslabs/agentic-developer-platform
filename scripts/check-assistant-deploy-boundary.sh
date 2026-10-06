#!/usr/bin/env bash
# Assistant rollout boundary (design: docs/architecture/adp-assistant-6929.md,
# inventory: docs/architecture/assistant-deploy-boundary.md).
#
# Runs inside a deployment job before any AWS credential or kubeconfig step and
# decides whether this candidate may roll protected assistant files onto the
# named deployment target.
#
# Inputs (environment):
#   ADP_ASSISTANT_DEPLOY_COMPONENT   gateway | chat-worker | agent-worker
#   ADP_ASSISTANT_DEPLOY_TARGET      exact protected deployment environment name
#   GITHUB_EVENT_NAME                push | workflow_dispatch (anything else refuses)
#   GITHUB_SHA                       candidate commit (override: ADP_ASSISTANT_DEPLOY_CANDIDATE)
#   ADP_ASSISTANT_APPROVED_TARGET    persistent approval: environment name it applies to
#   ADP_ASSISTANT_APPROVED_REVISION  persistent approval: approved 40-hex source commit
#   ADP_ASSISTANT_DISPATCH_APPROVED_REVISION
#                                    workflow_dispatch input `adp_approved_revision`;
#                                    one-off approval for this run only
#   ADP_DEPLOYED_BASELINE            actual source of the last successful deployment
#                                    of this workflow on the target (resolved by the
#                                    workflow via the GitHub API); consulted only
#                                    when no approval applies to this target
#
# Decision:
#   1. An approval for this target (persistent or dispatch input) must be an
#      available ancestor of the candidate; the protected files of candidate and
#      approval must then be identical. Otherwise the candidate carries held
#      assistant changes and is refused.
#   2. With no approval for this target, the candidate may continue only when
#      it does not change any protected file relative to the last revision this
#      workflow actually deployed (ADP_DEPLOYED_BASELINE). A refused earlier push
#      therefore cannot be carried by a later unrelated push. An unknown baseline
#      refuses; there is no fallback to the push's previous revision.
set -euo pipefail

refuse() {
  echo "::error::$1"
  exit 1
}

case "${ADP_ASSISTANT_DEPLOY_COMPONENT:-}" in
  gateway)
    protected_paths=(
      modules/gateway/src/agentauth
      'modules/gateway/src/orchestration/chat_*'
      'modules/gateway/src/orchestration/intake_*'
      modules/gateway/src/chat_data
      modules/gateway/src/app.py
      modules/gateway/src/main.py
    )
    ;;
  chat-worker)
    protected_paths=(
      modules/agent-factory/agent/src/complex-task-chat
      'modules/agent-factory/agent/k8s/chat-*'
      modules/agent-factory/agent/k8s/deploy-chat-scaledjob.sh
    )
    ;;
  agent-worker)
    # The shared adp-agent-runtime image ships the assistant/chat surfaces of
    # the TypeScript worker; its rollout onto agent-scaledjob is gated on them.
    protected_paths=(
      modules/agent-factory/agent/src/complex-task-chat
      'modules/agent-factory/agent/k8s/chat-*'
    )
    ;;
  *)
    refuse 'Unknown assistant deployment component; deployment refused.'
    ;;
esac

target="${ADP_ASSISTANT_DEPLOY_TARGET:-}"
if [[ -z "$target" ]]; then
  refuse 'Missing protected deployment target; deployment refused.'
fi

event="${GITHUB_EVENT_NAME:-}"
case "$event" in
  push | workflow_dispatch) ;;
  *)
    refuse 'Unsupported deployment event; deployment refused.'
    ;;
esac

is_sha() { [[ "$1" =~ ^[a-f0-9]{40}$ ]]; }
commit_available() { git cat-file -e "$1^{commit}" 2>/dev/null; }

candidate="${ADP_ASSISTANT_DEPLOY_CANDIDATE:-${GITHUB_SHA:-}}"
if ! is_sha "$candidate"; then
  refuse 'An immutable 40-character candidate revision is required; deployment refused.'
fi
if [[ "$(git rev-parse HEAD)" != "$candidate" ]]; then
  refuse 'Checkout does not match the deployment candidate; deployment refused.'
fi

# protected_diff BASE -> prints "same" | "changed" | "error"
protected_diff() {
  local status=0
  git diff --quiet "$1" "$candidate" -- "${protected_paths[@]}" || status=$?
  case "$status" in
    0) echo same ;;
    1) echo changed ;;
    *) echo error ;;
  esac
}

# ── 1. Resolve the approval that applies to this target ─────────────────────
approved=""
approval_source=""
dispatch_revision="${ADP_ASSISTANT_DISPATCH_APPROVED_REVISION:-}"
persistent_target="${ADP_ASSISTANT_APPROVED_TARGET:-}"
persistent_revision="${ADP_ASSISTANT_APPROVED_REVISION:-}"

if [[ "$event" == workflow_dispatch && -n "$dispatch_revision" ]]; then
  if ! is_sha "$dispatch_revision"; then
    refuse 'Dispatch input adp_approved_revision must be a full 40-character commit; deployment refused.'
  fi
  approved="$dispatch_revision"
  approval_source="workflow_dispatch input adp_approved_revision"
elif [[ -n "$persistent_revision" && "$persistent_target" == "$target" ]]; then
  if ! is_sha "$persistent_revision"; then
    refuse "ADP_ASSISTANT_APPROVED_REVISION for target '$target' is not a full 40-character commit; deployment refused."
  fi
  approved="$persistent_revision"
  approval_source="ADP_ASSISTANT_APPROVED_REVISION for target '$target'"
elif [[ -n "$persistent_revision" && -n "$persistent_target" ]]; then
  echo "Assistant approval is recorded for target '$persistent_target', not '$target'; it does not apply here."
fi

# ── 2. Approval present: candidate must match the approved assistant source ──
if [[ -n "$approved" ]]; then
  if ! commit_available "$approved" || ! git merge-base --is-ancestor "$approved" "$candidate"; then
    refuse "Approved revision ($approval_source) must be an available ancestor of the candidate; deployment refused."
  fi
  case "$(protected_diff "$approved")" in
    same)
      echo "Candidate assistant files match the approved revision $approved ($approval_source); unrelated deployment may continue."
      exit 0
      ;;
    changed)
      refuse "Candidate contains held assistant changes relative to approved revision $approved ($approval_source); record a target-specific approval or dispatch with adp_approved_revision for this candidate."
      ;;
    *)
      refuse 'Cannot compare candidate with approved assistant source; deployment refused.'
      ;;
  esac
fi

# ── 3. No approval for this target: compare with what this workflow last deployed ──
missing="No assistant approval is recorded for target '$target' (set ADP_ASSISTANT_APPROVED_TARGET='$target' and ADP_ASSISTANT_APPROVED_REVISION on that environment, or dispatch with the adp_approved_revision input)"

baseline="${ADP_DEPLOYED_BASELINE:-}"
if [[ -z "$baseline" ]]; then
  refuse "$missing and the last successfully deployed revision of this workflow is unknown (ADP_DEPLOYED_BASELINE is empty; a brand-new workflow has none); deployment refused."
fi
if ! is_sha "$baseline"; then
  refuse "$missing and the deployed baseline '$baseline' is not a full 40-character commit; deployment refused."
fi
if ! commit_available "$baseline" || ! git merge-base --is-ancestor "$baseline" "$candidate"; then
  refuse "$missing and the deployed baseline $baseline is not an available ancestor of the candidate; deployment refused."
fi

case "$(protected_diff "$baseline")" in
  same)
    echo "No assistant changes in this candidate relative to the last deployed revision $baseline; no approval is recorded for target '$target' but none is needed; unrelated deployment continues."
    exit 0
    ;;
  changed)
    refuse "Candidate changes protected assistant paths relative to the last deployed revision $baseline and $missing; deployment refused."
    ;;
  *)
    refuse 'Cannot compare candidate with the last deployed revision; deployment refused.'
    ;;
esac
