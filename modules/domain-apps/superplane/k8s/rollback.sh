#!/usr/bin/env bash
#
# Roll back or tear down the Superplane SkyPilot rollout — Issue #5042 (U3), EPIC #4910.
#
# WHY THIS IS NOT `kubectl rollout undo`
#
# `kubectl rollout undo` reverts to "the previous ReplicaSet", whatever that happens to be. Two
# problems with that here:
#
#   1. It names no digest, so afterwards nobody can say what is running — which is the exact
#      property the release lock exists to provide (R2). A rollback that loses provenance
#      trades one unknown state for another.
#   2. The previous ReplicaSet may not be a state anyone reviewed. It can be a half-finished
#      earlier attempt, or a revision whose config it no longer matches.
#
# So this script requires the target digest EXPLICITLY, verifies that digest actually appears in
# the cluster's own rollout history before acting, and refuses otherwise. An operator who cannot
# name the digest they want does not yet know what they are rolling back to.
#
# WHAT A ROLLBACK HERE DOES NOT UNDO
#
# SkyPilot's cluster and job state lives in Postgres (U2 pinned `state_backend: postgres`
# precisely so it survives pod replacement). Rolling the API server image back does NOT roll
# that state back, and **it does not shut down any GPU cluster SkyPilot has launched.** A
# running cluster outlives the API server that created it. If clusters are running, they keep
# costing money after this script finishes — enumerate and stop them deliberately (`sky status`,
# `sky down`) from an authorized client. U19 owns the resource/state handover.
#
# The script therefore refuses to be quiet about that: it warns before every teardown, and does
# not pretend to have reclaimed anything.
#
# NOTHING PLATFORM-OWNED IS TOUCHED. This acts on objects in the domain's own namespace only.
# It never applies platform infra, never mutates a shared NodeClass or ConfigMap, and never
# enables a cluster-wide controller.

set -euo pipefail

ENVIRONMENT="dev"
AWS_REGION="${AWS_REGION:-us-east-1}"
TO_DIGEST=""
TEARDOWN="false"
DRY_RUN="false"
DEPLOYMENT="skypilot-api"

usage() {
  cat <<'EOF'
Usage:
  rollback.sh --environment <env> --to-digest sha256:<64-hex>   Revert to a known digest
  rollback.sh --environment <env> --teardown                    Delete the domain's objects
  rollback.sh ... --dry-run                                     Show what would happen

Options:
  --environment <env>   Environment whose SSM parameters and cluster to use (default: dev)
  --to-digest <digest>  Target image digest. Required for a revert; must appear in the
                        Deployment's own rollout history.
  --teardown            Delete the rollout's objects in reverse dependency order.
  --dry-run             Print the actions without performing them.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --environment) ENVIRONMENT="${2:?--environment needs a value}"; shift 2 ;;
    --to-digest)   TO_DIGEST="${2:?--to-digest needs a value}"; shift 2 ;;
    --teardown)    TEARDOWN="true"; shift ;;
    --dry-run)     DRY_RUN="true"; shift ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "ERROR: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
  esac
done

# Exactly one mode. Neither is a safe default: with no mode selected the script would have to
# guess whether the operator wants a revert or a deletion.
if [ "$TEARDOWN" = "true" ] && [ -n "$TO_DIGEST" ]; then
  echo "ERROR: --teardown and --to-digest are mutually exclusive." >&2
  exit 2
fi
if [ "$TEARDOWN" != "true" ] && [ -z "$TO_DIGEST" ]; then
  echo "ERROR: specify either --to-digest <digest> or --teardown." >&2
  echo "       There is no default: 'the previous revision' is not necessarily a state" >&2
  echo "       anyone reviewed, and a rollback that cannot name its target loses the" >&2
  echo "       provenance the release lock exists to provide." >&2
  exit 2
fi

if [ -n "$TO_DIGEST" ] && ! printf '%s' "$TO_DIGEST" | grep -qE '^sha256:[0-9a-f]{64}$'; then
  echo "ERROR: --to-digest must be 'sha256:' followed by 64 hex characters (got '$TO_DIGEST')." >&2
  exit 2
fi

run() {
  if [ "$DRY_RUN" = "true" ]; then
    echo "DRY RUN: $*"
  else
    "$@"
  fi
}

for tool in aws kubectl; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: $tool is required." >&2; exit 1; }
done

# The namespace comes from SSM, not from a literal here. A literal that drifted from
# `var.skypilot_namespace` would make this script silently act on the wrong namespace — or on
# nothing, and report success.
SSM_PREFIX="/adp/${ENVIRONMENT}/superplane"
if ! NAMESPACE=$(aws ssm get-parameter \
      --name "${SSM_PREFIX}/skypilot-namespace" \
      --region "$AWS_REGION" \
      --query 'Parameter.Value' --output text 2>/dev/null); then
  echo "ERROR: ${SSM_PREFIX}/skypilot-namespace not found." >&2
  echo "       This script reads what Terraform published rather than assuming a namespace." >&2
  echo "       A missing parameter means superplane-infra-apply.yml has not run for" >&2
  echo "       environment '${ENVIRONMENT}', so there is nothing deployed to roll back." >&2
  exit 1
fi

if [ -z "$NAMESPACE" ] || [ "$NAMESPACE" = "None" ]; then
  echo "ERROR: the published namespace is empty. Refusing to act on an unknown namespace." >&2
  exit 1
fi

echo "Environment: ${ENVIRONMENT}"
echo "Namespace:   ${NAMESPACE}"

if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
  echo "ERROR: namespace '${NAMESPACE}' does not exist in the current cluster context." >&2
  echo "       Confirm kubeconfig points at the ${ENVIRONMENT} cluster before retrying." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------
if [ "$TEARDOWN" = "true" ]; then
  cat <<EOF

WARNING — read before continuing.

Deleting the SkyPilot API server does NOT shut down any GPU cluster it has launched. Those
clusters outlive the API server, and they keep costing money. Their handles live in the
Postgres state backend, which this teardown does not touch either.

Enumerate and stop them deliberately from an authorized client BEFORE tearing this down:

    sky status        # what is running
    sky down <name>   # stop it

This script cannot do that for you: it has no SkyPilot client and no authorization to
terminate compute (U19 owns the resource/state handover).

EOF

  # Reverse dependency order: workloads before the identities and policies they use, so no pod
  # is left running without its NetworkPolicy or its ServiceAccount.
  for object in \
      "deployment/${DEPLOYMENT}" \
      "service/skypilot-api" \
      "networkpolicy/skypilot-api-egress" \
      "networkpolicy/skypilot-api-ingress" \
      "networkpolicy/default-deny" \
      "configmap/skypilot-config" \
      "rolebinding/skypilot-api" \
      "role/skypilot-api" \
      "serviceaccount/skypilot-api"; do
    echo "Deleting ${object} in ${NAMESPACE}"
    run kubectl delete "$object" -n "$NAMESPACE" --ignore-not-found
  done

  cat <<EOF

Teardown complete for the objects this lane owns.

DELIBERATELY NOT DELETED:
  - The namespace itself. Deleting it would take the out-of-band 'skypilot-api-db' Secret
    with it, and this script did not create that Secret. Delete the namespace by hand once
    you have confirmed nothing else in it is needed.
  - Everything AWS-side (IAM roles, ECR repositories, SSM parameters). Those are Terraform's:
    use superplane-infra-destroy.yml, which validates per-instance ownership from a saved
    plan before deleting anything.
  - Any running SkyPilot cluster. See the warning above.
EOF
  exit 0
fi

# ---------------------------------------------------------------------------
# Revert to a named digest
# ---------------------------------------------------------------------------
if ! CURRENT_IMAGE=$(kubectl get "deployment/${DEPLOYMENT}" -n "$NAMESPACE" \
      -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null); then
  echo "ERROR: deployment/${DEPLOYMENT} not found in namespace '${NAMESPACE}'." >&2
  echo "       There is nothing to roll back." >&2
  exit 1
fi

echo "Currently deployed: ${CURRENT_IMAGE}"

if [ "$CURRENT_IMAGE" = "${CURRENT_IMAGE%@*}@${TO_DIGEST}" ]; then
  echo "The requested digest is already deployed. Nothing to do."
  exit 0
fi

# The registry/repository is taken from what is RUNNING, so a rollback cannot silently move to a
# different repository while claiming to be a digest revert.
REPOSITORY="${CURRENT_IMAGE%@*}"
if [ "$REPOSITORY" = "$CURRENT_IMAGE" ]; then
  echo "ERROR: the running image ('${CURRENT_IMAGE}') is not digest-addressed." >&2
  echo "       Refusing to roll back from an unpinned state: nothing can establish what is" >&2
  echo "       currently running, so 'rolled back' would be an unverifiable claim." >&2
  exit 1
fi
TARGET_IMAGE="${REPOSITORY}@${TO_DIGEST}"

# The digest must appear in the Deployment's OWN history. This is what makes the target a state
# this cluster actually ran, rather than an arbitrary digest an operator typed — including a
# digest from another environment, or one that was never deployed here at all.
echo "Checking the rollout history for ${TO_DIGEST}…"
HISTORY_IMAGES=$(kubectl get replicaset -n "$NAMESPACE" \
  -l "app.kubernetes.io/name=skypilot-api" \
  -o jsonpath='{range .items[*]}{.spec.template.spec.containers[0].image}{"\n"}{end}' 2>/dev/null || true)

if ! printf '%s\n' "$HISTORY_IMAGES" | grep -qF -- "$TO_DIGEST"; then
  echo "ERROR: ${TO_DIGEST} does not appear in this Deployment's rollout history." >&2
  echo "" >&2
  echo "Digests this cluster has actually run for skypilot-api:" >&2
  if [ -n "$HISTORY_IMAGES" ]; then
    printf '  %s\n' $HISTORY_IMAGES >&2
  else
    echo "  (none found — no ReplicaSet carries the app.kubernetes.io/name=skypilot-api label)" >&2
  fi
  echo "" >&2
  echo "Refusing to roll back to a digest this cluster never ran. If the digest is correct" >&2
  echo "and the history was pruned, deploy it forward through superplane-k8s-deploy.yml so" >&2
  echo "it passes the rendered-manifest validation, rather than setting the image directly." >&2
  exit 1
fi

echo "Rolling back ${DEPLOYMENT} to ${TARGET_IMAGE}"
run kubectl set image "deployment/${DEPLOYMENT}" \
  "skypilot-api=${TARGET_IMAGE}" -n "$NAMESPACE"

if [ "$DRY_RUN" = "true" ]; then
  echo "DRY RUN: would wait for the rollout to complete."
  exit 0
fi

# Waiting is not optional. `kubectl set image` returns as soon as the object is updated, so
# without this the script would report success while the new pod is in ImagePullBackOff — a
# rollback that reports success while the service is down is worse than one that fails loudly.
if ! kubectl rollout status "deployment/${DEPLOYMENT}" -n "$NAMESPACE" --timeout=300s; then
  echo "" >&2
  echo "ERROR: the rollback did not become ready within 300s. The service may be down." >&2
  echo "       Diagnose before retrying:" >&2
  echo "         kubectl describe deployment/${DEPLOYMENT} -n ${NAMESPACE}" >&2
  echo "         kubectl logs -n ${NAMESPACE} -l app.kubernetes.io/name=skypilot-api --tail=50" >&2
  exit 1
fi

RUNNING=$(kubectl get "deployment/${DEPLOYMENT}" -n "$NAMESPACE" \
  -o jsonpath='{.spec.template.spec.containers[0].image}')
echo ""
echo "Rollback complete. Running: ${RUNNING}"
cat <<EOF

NOT rolled back by this operation:
  - SkyPilot's cluster and job state in Postgres. Reverting the API server image does not
    revert its database.
  - Any running GPU cluster. Those outlive the API server and continue to cost money;
    enumerate with 'sky status' and stop deliberately.
EOF
