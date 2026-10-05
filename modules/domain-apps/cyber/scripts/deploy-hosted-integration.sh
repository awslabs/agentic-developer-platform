#!/usr/bin/env bash
# Install the optional Cyber integration after platform and webhook ingress.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOT_DIR="$(cd "$MODULE_DIR/../../.." && pwd)"
TF_DIR="$MODULE_DIR/infra/hosted-integration"
WEBHOOK_TF_DIR="$ROOT_DIR/modules/agent-factory/webhook-ingress/infra"
ENVIRONMENT="${ENVIRONMENT:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
MODE="${1:---plan}"
TFVARS="${2:-}"

if [[ "$MODE" != "--plan" && "$MODE" != "--apply" ]]; then
  echo "Usage: $0 [--plan|--apply] [settings.tfvars]" >&2
  exit 2
fi
if [[ "$MODE" == "--apply" && -z "$TFVARS" ]]; then
  echo "--apply requires reviewed Cyber settings and image digests in a tfvars file" >&2
  exit 2
fi
if [[ -n "$TFVARS" ]]; then
  TFVARS="$(realpath "$TFVARS")"
  [[ -f "$TFVARS" ]] || { echo "Missing tfvars: $TFVARS" >&2; exit 2; }
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"
BACKEND_ARGS=(
  "-backend-config=bucket=${STATE_BUCKET}"
  "-backend-config=region=${AWS_REGION}"
  "-backend-config=encrypt=true"
  "-backend-config=dynamodb_table=adp-terraform-locks"
)
printf 'Cyber hosted integration: account=%s region=%s env=%s mode=%s\n' "$ACCOUNT_ID" "$AWS_REGION" "$ENVIRONMENT" "$MODE"

# Do not let a second state adopt resources still owned by webhook Terraform.
terraform -chdir="$WEBHOOK_TF_DIR" init -input=false -reconfigure \
  "${BACKEND_ARGS[@]}" \
  "-backend-config=key=${ENVIRONMENT}/modules/webhook-ingress/terraform.tfstate" >/dev/null
if terraform -chdir="$WEBHOOK_TF_DIR" state list | grep -Eq '^module\.cyber(\[|\.)'; then
  echo "Legacy Cyber resources remain in webhook state. Migrate their state before installing this root." >&2
  exit 1
fi

terraform -chdir="$TF_DIR" init -input=false -reconfigure \
  "${BACKEND_ARGS[@]}" \
  "-backend-config=key=${ENVIRONMENT}/modules/cyber-hosted-integration/terraform.tfstate"
VARS=("-var=account_id=${ACCOUNT_ID}" "-var=aws_region=${AWS_REGION}" "-var=environment=${ENVIRONMENT}")
[[ -z "$TFVARS" ]] || VARS+=("-var-file=${TFVARS}")
if [[ "$MODE" == "--plan" ]]; then
  terraform -chdir="$TF_DIR" plan "${VARS[@]}"
else
  terraform -chdir="$TF_DIR" apply "${VARS[@]}" -auto-approve
fi
