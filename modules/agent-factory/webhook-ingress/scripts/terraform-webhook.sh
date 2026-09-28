#!/usr/bin/env bash
# Shared Terraform inputs for CLI deploy, CI plan and CI apply. Environment
# overlays are optional; account, region and state keys never fall back to dev.
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../../../.." && pwd)"
deploy_env="${ADP_ENV:-${ENVIRONMENT:?ADP_ENV or ENVIRONMENT required}}"
deploy_region="${AWS_REGION:?AWS_REGION required}"
[[ "$deploy_env" =~ ^[a-z][a-z0-9-]*$ ]] || { echo "Invalid deployment environment" >&2; exit 2; }
action="${1:?Terraform action required}"
shift
cd "$script_dir/../infra"
args=()
case "$action" in
  init)
    args=(
      "-backend-config=bucket=${STATE_BUCKET:?STATE_BUCKET required}"
      "-backend-config=key=${deploy_env}/modules/webhook-ingress/terraform.tfstate"
      "-backend-config=region=${ADP_STATE_REGION:-$deploy_region}"
      "-backend-config=encrypt=true"
      "-backend-config=dynamodb_table=adp-terraform-locks"
    )
    ;;
  plan|apply|import)
    args=("-var-file=terraform.tfvars")
    for suffix in tfvars tfvars.json; do
      overlay="$repo_root/environments/$deploy_env/modules/webhook-ingress.$suffix"
      if [[ -f "$overlay" ]]; then args+=("-var-file=$overlay"); fi
    done
    args+=("-var=environment=$deploy_env" "-var=aws_region=$deploy_region")
    ;;
  *) echo "Unsupported Terraform action: $action" >&2; exit 2 ;;
esac
exec terraform "$action" "${args[@]}" "$@"
