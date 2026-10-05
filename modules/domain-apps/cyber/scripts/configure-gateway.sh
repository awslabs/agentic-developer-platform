#!/usr/bin/env bash
# Publish Cyber's broker settings only after the Cyber sandbox owns its queues,
# table and gateway IAM grant. The basic gateway ConfigMap has no Cyber values.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="$(cd "$SCRIPT_DIR/../infra" && pwd)"
ENVIRONMENT="${ENVIRONMENT:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
TEMP_DIR="$(mktemp -d)"
PATCH_FILE="$TEMP_DIR/cyber-gateway.json"
KUBECONFIG_FILE="$TEMP_DIR/kubeconfig"
trap 'rm -rf "$TEMP_DIR"' EXIT

terraform -chdir="$TF_DIR" init -input=false -reconfigure \
  "-backend-config=bucket=adp-terraform-state-${ACCOUNT_ID}" \
  "-backend-config=key=${ENVIRONMENT}/modules/cyber-sandbox/terraform.tfstate" \
  "-backend-config=region=${AWS_REGION}" \
  "-backend-config=encrypt=true" \
  "-backend-config=dynamodb_table=adp-terraform-locks" >/dev/null
terraform -chdir="$TF_DIR" output -json cyber_gateway_config |
  python3 -c 'import json,sys; data=json.load(sys.stdin); expected={"CYBER_SAMPLE_BUCKET","CYBER_TRIAGE_QUEUE","CYBER_STATIC_QUEUE","CYBER_RESULTS_TABLE"}; assert set(data)==expected and all(isinstance(value,str) and value for value in data.values()), "Incomplete Cyber gateway configuration"; json.dump({"data":data},sys.stdout)' > "$PATCH_FILE"

aws eks update-kubeconfig --name "adp-${ENVIRONMENT}-eks-cluster" \
  --region "$AWS_REGION" --kubeconfig "$KUBECONFIG_FILE" >/dev/null
kubectl --kubeconfig "$KUBECONFIG_FILE" -n adp-gateway get configmap bedrockgateway-config >/dev/null
kubectl --kubeconfig "$KUBECONFIG_FILE" -n adp-gateway patch configmap bedrockgateway-config \
  --type merge --patch-file "$PATCH_FILE"
kubectl --kubeconfig "$KUBECONFIG_FILE" -n adp-gateway rollout restart deployment/bedrockgateway
kubectl --kubeconfig "$KUBECONFIG_FILE" -n adp-gateway rollout status deployment/bedrockgateway --timeout=300s
