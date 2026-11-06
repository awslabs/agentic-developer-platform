#!/usr/bin/env bash
# =============================================================================
# refresh-controller.sh — bring the running dev-box-controller up to date
# =============================================================================
# The controller runs the gateway release image with its own code mounted from the
# ConfigMap dev-box-controller-code (k8s/controller.yaml). Two things make it drift
# from main in a live environment, and both were seen on dev 2026-10-18 (#8208 was
# merged and the gateway redeployed, but the controller kept running the old code
# and the old image for six hours):
#
#   1. a gateway deploy rolls deploy/bedrockgateway to a new image but not the
#      controller, which imports gateway code from the same image;
#   2. a merge that changes controller/*.py runs dev-box-ci.yml (tests only);
#      nothing re-applies the ConfigMap.
#
# This script is the one place both paths converge:
#   * gateway-deploy.yml calls it after the gateway rollout with the released image;
#   * dev-box-controller-deploy.yml calls it on a push that changes controller code;
#   * deploy-dev-box.sh --controller-only calls it for a self-managed operator.
#
# It is a no-op (exit 0) when the environment has no controller — Dev Box is an
# optional module — so the gateway deploy can call it unconditionally.
#
# Usage:
#   refresh-controller.sh [--namespace adp-gateway] [--image <gateway image ref>]
#                         [--source-revision <sha>] [--timeout 300s] [--dry-run]
#                         [--container controller] [--allow-downgrade]
#
# Code never rolls back by accident (#8223 cause 54): when the running controller's
# recorded source revision is a descendant of --source-revision in this checkout's
# history, the code ConfigMap is left alone and only the image is updated. A gateway
# deploy legitimately moves the image; controller code is deployed from main, and a
# deploy started from an older commit must not undo a controller fix merged since
# (dev 2026-10-22 07:03: a workflow_dispatch run from 06:20 replaced controller code
# merged at 06:55). --allow-downgrade is the deliberate rollback switch.
#
#   --image defaults to the image deploy/bedrockgateway runs right now, so the
#   controller never runs an image the gateway does not. --source-revision defaults
#   to the checkout's HEAD and is recorded on the pod template so the live
#   controller says which commit its code came from.
#
# Exit codes: 0 refreshed or nothing to refresh; 1 the rollout failed (the last 50
# controller log lines are printed); 2 bad inputs.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${MODULE_ROOT}/../../.." && pwd)"

NAMESPACE="adp-gateway"
IMAGE=""
SOURCE_REVISION=""
TIMEOUT="300s"
DRY_RUN=false
ALLOW_DOWNGRADE=false
CODE_CONFIGMAP="dev-box-controller-code"
DEPLOYMENT="dev-box-controller"
CONTAINER="controller"   # the container name in k8s/controller.yaml
STRAY_CONTAINER="dev-box-controller"  # added by the 2026-10-19 refresh bug; removed if present

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace)       NAMESPACE="${2:?--namespace requires a value}"; shift 2 ;;
    --image)           IMAGE="${2:?--image requires a gateway image reference}"; shift 2 ;;
    --source-revision) SOURCE_REVISION="${2:?--source-revision requires a commit}"; shift 2 ;;
    --timeout)         TIMEOUT="${2:?--timeout requires a kubectl duration}"; shift 2 ;;
    --container)       CONTAINER="${2:?--container requires the controller container name}"; shift 2 ;;
    --dry-run)         DRY_RUN=true; shift ;;
    --allow-downgrade) ALLOW_DOWNGRADE=true; shift ;;
    -h|--help)         sed -n '2,42p' "$0"; exit 0 ;;
    *) echo "refresh-controller.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done

[[ "$NAMESPACE" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || { echo "refresh-controller.sh: invalid namespace" >&2; exit 2; }
command -v kubectl >/dev/null || { echo "refresh-controller.sh: kubectl is required" >&2; exit 2; }
[ -f "$MODULE_ROOT/controller/worker.py" ] || { echo "refresh-controller.sh: $MODULE_ROOT/controller is not a controller checkout" >&2; exit 2; }

if ! kubectl -n "$NAMESPACE" get "deploy/$DEPLOYMENT" >/dev/null 2>&1; then
  echo "refresh-controller.sh: no deploy/$DEPLOYMENT in $NAMESPACE; Dev Box is not deployed here, nothing to refresh"
  exit 0
fi

if [ -z "$IMAGE" ]; then
  IMAGE=$(kubectl -n "$NAMESPACE" get deploy bedrockgateway -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
  [ -n "$IMAGE" ] || { echo "refresh-controller.sh: --image not given and deploy/bedrockgateway has no image in $NAMESPACE" >&2; exit 2; }
fi
# The controller must run an immutable gateway release, never a floating tag.
[[ "$IMAGE" =~ /adp-gateway@sha256:[0-9a-f]{64}$ ]] || { echo "refresh-controller.sh: --image must be an adp-gateway image pinned by digest (got $IMAGE)" >&2; exit 2; }
[ -n "$SOURCE_REVISION" ] || SOURCE_REVISION=$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)

CONTAINERS=$(kubectl -n "$NAMESPACE" get "deploy/$DEPLOYMENT" -o jsonpath='{range .spec.template.spec.containers[*]}{.name}{"\n"}{end}')
grep -qx "$CONTAINER" <<<"$CONTAINERS" || { echo "refresh-controller.sh: deploy/$DEPLOYMENT has no container named $CONTAINER (has: $(tr '\n' ' ' <<<"$CONTAINERS"))" >&2; exit 2; }
CURRENT_IMAGE=$(kubectl -n "$NAMESPACE" get "deploy/$DEPLOYMENT" -o jsonpath="{.spec.template.spec.containers[?(@.name==\"$CONTAINER\")].image}")
RUNNING_REVISION=$(kubectl -n "$NAMESPACE" get "deploy/$DEPLOYMENT" -o jsonpath='{.spec.template.metadata.annotations.adp\.aws-e\.io/source-revision}' 2>/dev/null || true)
echo "refresh-controller.sh: $NAMESPACE/$DEPLOYMENT container $CONTAINER"
echo "  code   : $MODULE_ROOT/controller ($(ls "$MODULE_ROOT"/controller/*.py | wc -l | tr -d ' ') files) @ $SOURCE_REVISION"
echo "  running: ${RUNNING_REVISION:-unknown}"
echo "  image  : $CURRENT_IMAGE"
echo "       -> : $IMAGE"

# Never roll controller code back by accident (#8223 cause 54). The running revision
# is compared against the candidate in this checkout's history; a candidate that is an
# ancestor of what already runs is older code, so the ConfigMap is left alone and only
# the image follows the gateway. Unknown or unresolvable revisions (no annotation yet,
# shallow clone) keep the historical behaviour, and say so.
APPLY_CODE=true
if [ "$ALLOW_DOWNGRADE" != true ] && [[ "$RUNNING_REVISION" =~ ^[0-9a-f]{40}$ ]] \
   && [[ "$SOURCE_REVISION" =~ ^[0-9a-f]{40}$ ]] && [ "$RUNNING_REVISION" != "$SOURCE_REVISION" ]; then
  if git -C "$REPO_ROOT" cat-file -e "${RUNNING_REVISION}^{commit}" 2>/dev/null \
     && git -C "$REPO_ROOT" cat-file -e "${SOURCE_REVISION}^{commit}" 2>/dev/null; then
    if git -C "$REPO_ROOT" merge-base --is-ancestor "$SOURCE_REVISION" "$RUNNING_REVISION"; then
      APPLY_CODE=false
      echo "refresh-controller.sh: refusing to roll the controller back from $RUNNING_REVISION to $SOURCE_REVISION; refresh from a newer checkout (or pass --allow-downgrade for a deliberate rollback). Only the image is updated."
    fi
  else
    echo "refresh-controller.sh: running revision ${RUNNING_REVISION} or candidate ${SOURCE_REVISION} is not in this checkout's history; cannot compare, applying the candidate code"
  fi
fi

if [ "$DRY_RUN" = true ]; then
  if [ "$APPLY_CODE" = true ]; then
    echo "dry-run: would apply ConfigMap $CODE_CONFIGMAP, set the $CONTAINER image and wait for the rollout"
  else
    echo "dry-run: would keep the running controller code and only set the $CONTAINER image"
  fi
  exit 0
fi

if [ "$APPLY_CODE" != true ] && [ "$CURRENT_IMAGE" = "$IMAGE" ]; then
  echo "refresh-controller.sh: $DEPLOYMENT already runs $IMAGE with newer controller code ($RUNNING_REVISION); nothing to refresh"
  exit 0
fi

# Same construction deploy-dev-box.sh uses, so a refresh and a full stage produce
# an identical ConfigMap for the same checkout.
if [ "$APPLY_CODE" = true ]; then
  kubectl create configmap "$CODE_CONFIGMAP" -n "$NAMESPACE" --from-file="$MODULE_ROOT/controller" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
fi

# A stray container named after the Deployment was added by an earlier version of
# this script (strategic patch by a wrong container name, dev 2026-10-19); it ran the
# gateway's default entrypoint and failed on the missing token secret. Remove it so
# the pod is exactly the manifest's single controller again.
if grep -qx "$STRAY_CONTAINER" <<<"$CONTAINERS"; then
  STRAY_INDEX=$(grep -nx "$STRAY_CONTAINER" <<<"$CONTAINERS" | cut -d: -f1)
  echo "refresh-controller.sh: removing stray container $STRAY_CONTAINER (index $((STRAY_INDEX-1)))"
  kubectl -n "$NAMESPACE" patch "deploy/$DEPLOYMENT" --type=json \
    -p "[{\"op\":\"remove\",\"path\":\"/spec/template/spec/containers/$((STRAY_INDEX-1))\"}]" >/dev/null
fi

# `set image` refuses an unknown container name instead of adding one, which is the
# property the earlier strategic patch lacked.
kubectl -n "$NAMESPACE" set image "deploy/$DEPLOYMENT" "$CONTAINER=$IMAGE" >/dev/null
# The annotation makes a code-only refresh visible as a new template generation
# (a ConfigMap change alone does not restart pods) and records provenance. When the
# code was kept, the recorded revision stays the running one.
if [ "$APPLY_CODE" = true ]; then
  kubectl -n "$NAMESPACE" patch "deploy/$DEPLOYMENT" --type=merge \
    -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"adp.aws-e.io/source-revision\":\"$SOURCE_REVISION\",\"adp.aws-e.io/controller-refreshed-from\":\"refresh-controller.sh\"}}}}}" >/dev/null
fi

if ! kubectl -n "$NAMESPACE" rollout status "deploy/$DEPLOYMENT" --timeout="$TIMEOUT"; then
  kubectl -n "$NAMESPACE" logs "deploy/$DEPLOYMENT" --tail=50 || true
  echo "refresh-controller.sh: $DEPLOYMENT did not become ready; the log above names the refused release check or missing grant" >&2
  exit 1
fi
if [ "$APPLY_CODE" = true ]; then
  echo "refresh-controller.sh: $DEPLOYMENT running $IMAGE with controller code from $SOURCE_REVISION"
else
  echo "refresh-controller.sh: $DEPLOYMENT running $IMAGE with controller code kept at $RUNNING_REVISION"
fi
