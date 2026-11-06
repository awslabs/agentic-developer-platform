#!/usr/bin/env bash
set -euo pipefail

module_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
usage() {
  echo 'Usage: deploy.sh [--infra|--emitter|--distiller|--verify|--bootstrap-ci] [--plan] --env ENV --region REGION [--image ECR_IMAGE@sha256:DIGEST] [--account ACCOUNT_ID for --bootstrap-ci]' >&2
  exit 2
}

stage=infra
plan=false
environment=
region=
image=
bootstrap_account=
while (($#)); do
  case "$1" in
    --infra|--emitter|--distiller|--verify|--bootstrap-ci) stage="${1#--}" ;;
    --plan) plan=true ;;
    --env|--region|--image|--account)
      (($# >= 2)) || usage
      case "$1" in
        --env) environment="$2" ;;
        --region) region="$2" ;;
        --image) image="$2" ;;
        --account) bootstrap_account="$2" ;;
      esac
      shift ;;
    *) usage ;;
  esac
  shift
done
[[ "$environment" =~ ^[a-z][a-z0-9-]*$ && "$region" =~ ^[a-z]{2}-[a-z]+-[0-9]+$ ]] || usage
[[ "$environment" == dev || "$environment" == prod ]] || usage

if [[ "$stage" == bootstrap-ci ]]; then
  [[ "$plan" == false && -z "$image" && "$bootstrap_account" =~ ^[0-9]{12}$ ]] || usage
  cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect":"Allow","Action":["s3:ListBucket","s3:GetBucketLocation","s3:GetBucketVersioning","s3:GetBucketEncryption","s3:GetBucketPublicAccessBlock","s3:GetBucketOwnershipControls","s3:GetBucketTagging","s3:GetBucketPolicy","s3:PutBucketPolicy","s3:PutBucketVersioning","s3:PutEncryptionConfiguration","s3:PutBucketPublicAccessBlock","s3:PutBucketOwnershipControls","s3:PutBucketTagging","s3:DeleteBucket","s3:CreateBucket"],"Resource":"arn:aws:s3:::adp-${environment}-insights-evidence"},
    {"Effect":"Allow","Action":"s3:ListBucket","Resource":"arn:aws:s3:::adp-terraform-state-${bootstrap_account}"},
    {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject"],"Resource":"arn:aws:s3:::adp-terraform-state-${bootstrap_account}/${environment}/modules/insights/terraform.tfstate"},
    {"Effect":"Allow","Action":"s3:GetObject","Resource":"arn:aws:s3:::adp-terraform-state-${bootstrap_account}/${environment}/platform/terraform.tfstate"},
    {"Effect":"Allow","Action":["iam:GetRole","iam:GetRolePolicy","iam:ListRolePolicies","iam:ListAttachedRolePolicies","iam:ListRoleTags","iam:ListInstanceProfilesForRole"],"Resource":["arn:aws:iam::${bootstrap_account}:role/adp-${environment}-insights-emitter","arn:aws:iam::${bootstrap_account}:role/adp-${environment}-insights-distiller"]},
    {"Effect":"Allow","Action":["ecr:CreateRepository","ecr:DeleteRepository","ecr:DescribeRepositories","ecr:DescribeImages","ecr:PutImageTagMutability","ecr:PutLifecyclePolicy","ecr:GetLifecyclePolicy","ecr:TagResource","ecr:ListTagsForResource"],"Resource":"arn:aws:ecr:${region}:${bootstrap_account}:repository/adp-${environment}-insights-*"},
    {"Effect":"Allow","Action":["kms:CreateKey","kms:ListAliases"],"Resource":"*","Condition":{"StringEquals":{"aws:RequestedRegion":"${region}"}}},
    {"Effect":"Allow","Action":["kms:DescribeKey","kms:EnableKeyRotation","kms:GetKeyRotationStatus","kms:PutKeyPolicy","kms:GetKeyPolicy","kms:TagResource","kms:ListResourceTags"],"Resource":"arn:aws:kms:${region}:${bootstrap_account}:key/*"},
    {"Effect":"Allow","Action":["kms:CreateAlias","kms:DeleteAlias"],"Resource":"arn:aws:kms:${region}:${bootstrap_account}:alias/adp-${environment}-insights-*"},
    {"Effect":"Allow","Action":"ssm:GetParameter","Resource":["arn:aws:ssm:${region}:${bootstrap_account}:parameter/adp/${environment}/platform/account-id","arn:aws:ssm:${region}:${bootstrap_account}:parameter/adp/${environment}/insights/l5-consumer-verification-keys","arn:aws:ssm:${region}:${bootstrap_account}:parameter/adp/${environment}/insights/l5-authority-gateway-url"]},
    {"Effect":"Allow","Action":"eks:DescribeCluster","Resource":"arn:aws:eks:${region}:${bootstrap_account}:cluster/adp-${environment}-eks-cluster"},
    {"Effect":"Allow","Action":["ec2:DescribePrefixLists","ec2:DescribeVpcEndpoints","ec2:DescribeNetworkInterfaces"],"Resource":"*","Condition":{"StringEquals":{"aws:RequestedRegion":"${region}"}}},
    {"Effect":"Allow","Action":"sts:GetCallerIdentity","Resource":"*"}
  ]
}
POLICY
  exit
fi

[[ -z "$bootstrap_account" ]] || usage
[[ -z "$image" || "$stage" == emitter || "$stage" == distiller ]] || usage
[[ "$stage" != verify || "$plan" == false ]] || usage
if [[ "$stage" == emitter || "$stage" == distiller ]]; then
  [[ "$image" =~ ^[a-zA-Z0-9._:/-]+@sha256:[a-f0-9]{64}$ ]] || usage
fi

account_parameter="${ADP_INSIGHTS_ACCOUNT_PARAMETER:-/adp/${environment}/platform/account-id}"
account=$(aws ssm get-parameter --name "$account_parameter" --region "$region" --query Parameter.Value --output text)
[[ "$account" =~ ^[0-9]{12}$ ]] || { echo 'Account parameter must contain a 12-digit account ID' >&2; exit 2; }
active_account=$(aws sts get-caller-identity --query Account --output text)
[[ "$account" == "$active_account" ]] || { echo 'Active identity differs from the SSM target account' >&2; exit 2; }

backend=("-backend-config=bucket=adp-terraform-state-${account}" "-backend-config=key=${environment}/modules/insights/terraform.tfstate" "-backend-config=region=${region}" "-backend-config=encrypt=true" "-backend-config=dynamodb_table=adp-terraform-locks")
terraform -chdir="$module_dir/platform" init -input=false -reconfigure "${backend[@]}"

if [[ "$stage" == verify ]]; then
  terraform -chdir="$module_dir/platform" output -json
  verify_directory=$(mktemp -d)
  trap 'rm -rf "$verify_directory"' EXIT
  kubeconfig="$verify_directory/kubeconfig"
  cluster_name="adp-${environment}-eks-cluster"
  cluster_context="arn:aws:eks:${region}:${account}:cluster/${cluster_name}"
  aws eks update-kubeconfig --name "$cluster_name" --region "$region" --kubeconfig "$kubeconfig" --alias "$cluster_context" >/dev/null
  kubectl --kubeconfig "$kubeconfig" --context "$cluster_context" -n adp-insights get deployment insights-emitter insights-distiller
  exit
fi

terraform -chdir="$module_dir/platform" validate
outputs=$(terraform -chdir="$module_dir/platform" output -json)
saved_images=$(python3 -c '
import json
import re
import sys

outputs = json.load(sys.stdin)
if not isinstance(outputs, dict):
    raise ValueError("Terraform outputs must be an object")
images = []
for name in ("emitter_image", "distiller_image"):
    value = outputs[name]["value"] if name in outputs else ""
    if not isinstance(value, str) or (value and not re.fullmatch(r"[a-zA-Z0-9._:/-]+@sha256:[a-f0-9]{64}", value)):
        raise ValueError(f"Invalid saved {name}")
    images.append(value)
print("|".join(images))
' <<< "$outputs")
IFS='|' read -r emitter_image distiller_image <<< "$saved_images"
if [[ "$stage" == emitter ]]; then emitter_image="$image"; fi
if [[ "$stage" == distiller ]]; then distiller_image="$image"; fi
vars=("-var=environment=${environment}" "-var=aws_region=${region}" "-var=aws_account_id=${account}"
      "-var=emitter_image=${emitter_image}" "-var=distiller_image=${distiller_image}")
plan_directory=$(mktemp -d)
trap 'rm -rf "$plan_directory"' EXIT
# An installed emitter must retain operator-owned receiver configuration on
# every stage. Never turn an unreadable parameter into an empty key map/default.
if [[ -n "$emitter_image" ]]; then
  python3 "$module_dir/scripts/read-receiver-config.py" --environment "$environment" --region "$region" \
    --account "$account" --output "$plan_directory/receiver.tfvars.json"
  vars+=("-var-file=$plan_directory/receiver.tfvars.json")
fi
if [[ "$plan" == true ]]; then
  terraform -chdir="$module_dir/platform" plan -input=false "${vars[@]}"
else
  plan_file="$plan_directory/deploy.tfplan"
  terraform -chdir="$module_dir/platform" plan -input=false "${vars[@]}" -out="$plan_file"
  terraform -chdir="$module_dir/platform" apply -input=false "$plan_file"
fi
