#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
  definition)
    test "$GITHUB_REF" = refs/heads/main
    if [[ "${ADP_TOOLS_MODE:-release}" == drift ]]; then
      test "${ADP_TOOLS_ENVIRONMENT:-dev}" = dev
    fi
    if [ "$GITHUB_EVENT_NAME" = push ] || [ "$GITHUB_EVENT_NAME" = schedule ]; then
      exit 0
    fi
    test "$GITHUB_EVENT_NAME" = workflow_dispatch
    if [ "${ADP_TOOLS_MODE:-release}" = identity ] || {
      [ "${ADP_TOOLS_MODE:-release}" = release ] && [ "${ADP_TOOLS_STAGE:-}" = tool-adapters ] &&
      [[ "${ADP_TOOLS_ADAPTER_ACTION:-}" =~ ^(handoff|rollback)$ ]];
    }; then
      test "${ADP_TOOLS_ENVIRONMENT:-dev}" = dev
      test "${ADP_TOOLS_STAGE:-}" = tool-adapters
      [[ "${ADP_CONTRACT_DIGEST:-}" =~ ^[0-9a-f]{64}$ ]]
      test -z "${ADP_MANUAL_SOURCE:-}"
    else
      test -z "${ADP_CONTRACT_DIGEST:-}"
    fi
    if [ -n "${ADP_MANUAL_SOURCE:-}" ]; then
      [[ "$ADP_MANUAL_SOURCE" =~ ^[0-9a-f]{40}$ ]]
      test -z "${ADP_CORRELATION:-}${ADP_SOURCE:-}${ADP_DEFINITION:-}"
    else
      [[ "${ADP_CORRELATION:-}" =~ ^([0-9a-f]{64}|orch:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$ ]]
      [[ "${ADP_SOURCE:-}" =~ ^[0-9a-f]{40}$ ]]
      [[ "${ADP_DEFINITION:-}" =~ ^[0-9a-f]{40}$ ]]
      # The engine pins the reviewed definition revision; the dispatch runs the
      # workflow at main's head. Unrelated commits land on main continuously, so
      # compare the workflow definition by content (as the interceptor and
      # façade guards do), not by commit id: the pinned revision must be reviewed
      # main history and its workflow file must be byte-identical to the one
      # actually running.
      git cat-file -e "${ADP_DEFINITION}^{commit}"
      git merge-base --is-ancestor "$ADP_DEFINITION" refs/remotes/origin/main
      workflow_file="${ADP_WORKFLOW_FILE:-.github/workflows/tools-platform-deploy.yml}"
      [[ "$workflow_file" =~ ^\.github/workflows/[A-Za-z0-9._-]+\.ya?ml$ ]]
      git show "$ADP_DEFINITION:$workflow_file" | cmp -s - "$workflow_file"
    fi
    ;;
  source)
    if [ "$GITHUB_EVENT_NAME" = push ] || [ "$GITHUB_EVENT_NAME" = schedule ]; then
      candidate="$GITHUB_SHA"
    else
      candidate="${ADP_MANUAL_SOURCE:-${ADP_SOURCE:-}}"
    fi
    test -n "$candidate"
    git cat-file -e "${candidate}^{commit}"
    git merge-base --is-ancestor "$candidate" refs/remotes/origin/main
    ;;
  *)
    exit 2
    ;;
esac
