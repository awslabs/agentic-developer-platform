#!/usr/bin/env bash
set -euo pipefail
# No API deployment/stage command belongs here; the shared API owner publishes it.
mode=${1:?Usage: deploy.sh plan BACKEND_CONFIG TFVARS SAVED_PLAN | deploy.sh apply BACKEND_CONFIG SAVED_PLAN}
backend_config=$(realpath "${2:?Supply isolated S3 backend configuration}")
expected_account=${EXPECTED_AWS_ACCOUNT_ID:?Set the account confirmed under the canonical deployment guide}
[[ "$expected_account" =~ ^[0-9]{12}$ ]] || exit 2
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
    terraform -chdir="$infra_dir" plan -input=false -var-file="$vars_file" -out="$plan_file"
    ;;
  apply)
    [[ $# -eq 3 && -f "$3" ]] || exit 2
    terraform -chdir="$infra_dir" init -input=false -backend-config="$backend_config"
    plan_file=$(realpath "$3")
    terraform -chdir="$infra_dir" apply -input=false "$plan_file"
    terraform -chdir="$infra_dir" output
    echo 'Infrastructure applied. Shared API owner must deploy the reviewed API configuration before this route is live.'
    ;;
  *) exit 2 ;;
esac
