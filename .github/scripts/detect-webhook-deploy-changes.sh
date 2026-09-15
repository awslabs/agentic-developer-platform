#!/usr/bin/env bash
# Emit deployment eligibility, not merely whether files changed. Keep mixed
# code/infra releases together when the worker migration blocks infrastructure.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
code=false
infra=false
case "${GITHUB_EVENT_NAME:?}" in
  workflow_dispatch)
    code=true
    infra=true
    ;;
  push)
    changed=$(git diff --name-only HEAD^ HEAD)
    if grep -qE '^modules/agent-factory/webhook-ingress/(lambda|common|scripts)/|^modules/agent-factory/webhook-ingress/requirements\.txt$' <<< "$changed"; then
      code=true
    fi
    if grep -qE '^modules/agent-factory/webhook-ingress/infra/' <<< "$changed"; then
      infra=true
    fi
    ;;
  *)
    echo "::error::Unsupported webhook deployment event"
    exit 1
    ;;
esac

held=false
if [[ "$infra" == true && -e .github/deployment-holds/webhook-infra.md ]]; then
  held=true
  code=false
  infra=false
  echo "::notice::Webhook infrastructure deployment held for worker migration (#5195)."
  cat >> "${GITHUB_STEP_SUMMARY:?}" <<'SUMMARY'
## Webhook infrastructure deployment held

No artifacts, Terraform state, infrastructure, or Lambda code were changed by
this run. Infrastructure and mixed releases are held until the scoped worker
migration in #5195 is ready. Code-only pushes remain eligible for deployment.
See `.github/deployment-holds/webhook-infra.md` for release conditions.
SUMMARY
fi

{
  echo "code=$code"
  echo "infra=$infra"
  echo "infra_held=$held"
} >> "${GITHUB_OUTPUT:?}"
