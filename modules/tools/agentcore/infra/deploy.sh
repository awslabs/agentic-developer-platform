#!/usr/bin/env bash
set -euo pipefail
# No API deployment/stage command belongs here; the shared API owner publishes it.
mode=${1:?Usage: deploy.sh plan BACKEND_CONFIG TFVARS SAVED_PLAN | deploy.sh apply BACKEND_CONFIG SAVED_PLAN}
backend_config=$(realpath "${2:?Supply isolated S3 backend configuration}")
expected_account=${EXPECTED_AWS_ACCOUNT_ID:?Set the account confirmed under the canonical deployment guide}
[[ "$expected_account" =~ ^[0-9]{12}$ ]] || exit 2
target_region=${AWS_REGION:?Set the confirmed deployment region}
[[ "$target_region" =~ ^[a-z0-9-]+$ ]] || exit 2
actual_account=$(aws sts get-caller-identity --query Account --output text)
[[ "$actual_account" == "$expected_account" ]] || { echo 'Active AWS identity differs from confirmed target' >&2; exit 2; }
infra_dir=$(cd "$(dirname "$0")" && pwd)
case "$mode" in
  plan)
    [[ $# -eq 4 ]] || exit 2
    terraform -chdir="$infra_dir" init -input=false -backend-config="$backend_config"
    terraform -chdir="$infra_dir" validate
    vars_file=$(realpath "$3")
    plan_file=$(realpath -m "$4")
    image_vars=("-var=aws_account_id=$expected_account" "-var=aws_region=$target_region")
    for variable in image_uri browser_service_image; do
      env_name="TF_VAR_$variable"
      if [[ -n "${!env_name:-}" ]]; then
        [[ "${!env_name}" =~ @sha256:[a-f0-9]{64}$ ]] || { echo 'Expected immutable image digest' >&2; exit 2; }
        image_vars+=("-var=$variable=${!env_name}")
      fi
    done
    terraform -chdir="$infra_dir" plan -input=false -var-file="$vars_file" "${image_vars[@]}" -out="$plan_file"
    terraform -chdir="$infra_dir" show -json "$plan_file" | jq -e '[.resource_changes[]? | select(.change.actions | index("delete"))] | length == 0' >/dev/null || { echo "Plan deletes or replaces resources; stop for a reviewed migration" >&2; exit 2; }
    ;;
  apply)
    [[ $# -eq 3 && -f "$3" ]] || exit 2
    terraform -chdir="$infra_dir" init -input=false -backend-config="$backend_config"
    plan_file=$(realpath "$3")
    terraform -chdir="$infra_dir" show -json "$plan_file" | jq -e \
      --arg account "$expected_account" --arg region "$target_region" \
      '.variables.aws_account_id.value == $account and .variables.aws_region.value == $region' >/dev/null || { echo 'Saved plan differs from confirmed account or region' >&2; exit 2; }
    terraform -chdir="$infra_dir" show -json "$plan_file" | jq -e '[.resource_changes[]? | select(.change.actions | index("delete"))] | length == 0' >/dev/null || { echo "Plan deletes or replaces resources; stop for a reviewed migration" >&2; exit 2; }
    terraform -chdir="$infra_dir" apply -input=false "$plan_file"
    terraform -chdir="$infra_dir" output
    echo 'Infrastructure applied. Shared API owner must deploy the reviewed API configuration before this route is live.'
    ;;
  *) exit 2 ;;
esac
