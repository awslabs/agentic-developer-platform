#!/usr/bin/env bash
# Decide whether this run may enable protected agent authority, and prove the
# worker images it would trust actually exist before Terraform trusts them.
#
# Issue #5365. Protected dispatch (`agent_authority_enabled`) admits a pod only
# when its image digest is on an approved list. Terraform already refuses to
# enable the feature with an empty list, and already refuses a value that is not
# `sha256:<64 hex>`. Neither of those checks can tell whether a well-formed
# digest is a real image in this account's registry: a typo'd-but-valid digest
# passes both and produces an authority that admits nothing, which surfaces as
# every agent run failing to bootstrap rather than as a deploy error.
#
# So this guard adds the one fact only the registry can answer, and makes
# enabling deliberate:
#
#   * Turning the feature ON requires an explicit request. A push that merely
#     touches this module never flips it — the committed tfvars value stays
#     authoritative and is echoed back, not overridden.
#   * Every digest must be `sha256:<64 lowercase hex>`. Tags are refused: a tag
#     is mutable, so admitting one would let a later push change what is
#     trusted without a deploy.
#   * Every digest must resolve to an existing image in the worker repository,
#     in this account and region.
#
# Emits terraform `-var` arguments on stdout (empty when it has nothing to
# override) and, on GitHub, a decision line to the step summary. Refusals exit
# non-zero with the reason; there is no path that enables the feature on
# unverified input.
set -euo pipefail

REQUESTED="${ENABLE_AGENT_AUTHORITY:-}"
DIGESTS_INPUT="${AGENT_AUTHORITY_WORKER_IMAGE_DIGESTS:-}"
REPOSITORY="${AGENT_AUTHORITY_WORKER_REPOSITORY:-adp-agent-runtime}"
TFVARS="${AGENT_AUTHORITY_TFVARS:-modules/agent-factory/webhook-ingress/infra/terraform.tfvars}"
REGION="${AWS_REGION:-}"

fail() {
  echo "::error::$1" >&2
  exit 1
}

note() {
  [ -n "${GITHUB_STEP_SUMMARY:-}" ] && echo "$1" >> "${GITHUB_STEP_SUMMARY}"
  echo "$1" >&2
}

# The committed value, so a run with no explicit request neither enables nor
# disables anything. `false` when the variable is absent, matching the variable's
# own Terraform default.
committed=false
if [ -f "$TFVARS" ] && grep -qE '^[[:space:]]*agent_authority_enabled[[:space:]]*=[[:space:]]*true' "$TFVARS"; then
  committed=true
fi

case "$REQUESTED" in
  true)  intended=true ;;
  false) intended=false ;;
  "")    intended="$committed" ;;
  *)     fail "enable_agent_authority must be true or false, got '${REQUESTED}'." ;;
esac

if [ "$intended" != true ]; then
  # Nothing to verify and nothing to override. An explicit `false` is passed
  # through so a dispatch can deliberately roll the feature back.
  if [ "$REQUESTED" = false ] && [ "$committed" = true ]; then
    note "Agent authority: explicitly disabled for this run (committed value is enabled)."
    echo '-var=agent_authority_enabled=false'
  else
    note "Agent authority: remains disabled; no rollout requested."
  fi
  exit 0
fi

[ -n "$DIGESTS_INPUT" ] || fail \
  "Enabling agent authority requires approved worker image digests. Build the worker with agent-worker-image.yml, then pass its sha256 digest as agent_authority_worker_image_digests. This guard will not invent one."

digests=()
IFS=',' read -r -a raw <<< "$DIGESTS_INPUT"
for entry in "${raw[@]}"; do
  digest="${entry//[[:space:]]/}"
  [ -n "$digest" ] || continue
  if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    fail "Worker image '${digest}' is not sha256:<64 lowercase hex digits>. A mutable tag must never be an admission identity."
  fi
  digests+=("$digest")
done

[ "${#digests[@]}" -gt 0 ] || fail "agent_authority_worker_image_digests contained no digest."
[ -n "$REGION" ] || fail "AWS_REGION is required to verify worker image digests against the registry."

for digest in "${digests[@]}"; do
  # Ask the registry directly. A well-formed digest that is not a real image
  # would otherwise deploy an authority that admits no pod at all.
  if ! found=$(aws ecr describe-images \
      --repository-name "$REPOSITORY" \
      --image-ids "imageDigest=${digest}" \
      --region "$REGION" \
      --query 'imageDetails[0].imageDigest' \
      --output text 2>/tmp/agent-authority-ecr.err); then
    cat /tmp/agent-authority-ecr.err >&2 || true
    fail "Could not verify ${digest} in ECR repository ${REPOSITORY}. Refusing to enable agent authority on an unverified image."
  fi
  [ "$found" = "$digest" ] || fail "ECR did not return ${digest} for repository ${REPOSITORY}; got '${found}'."
done

# Sorted and de-duplicated so the emitted value matches the `set(string)` the
# variable declares and does not churn the plan on input reordering.
approved=$(printf '%s\n' "${digests[@]}" | sort -u | paste -sd',' -)
note "Agent authority: enabling with $(printf '%s\n' "${digests[@]}" | sort -u | wc -l | tr -d ' ') verified worker image digest(s) in ${REPOSITORY}."

printf '%s\n' "-var=agent_authority_enabled=true"
printf '%s\n' "-var=agent_authority_worker_image_digests=[\"${approved//,/\",\"}\"]"
