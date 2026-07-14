#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# remove.sh — Complete ADP teardown and account cleanup
# =============================================================================
# Destroys ALL ADP resources in the target account, including:
#   - Webhook-ingress stack (API GW, Lambda, SQS, KEDA, DynamoDB)
#   - Gateway infra (RDS, Redis, Cognito, CloudFront, API GW, Lambdas)
#   - Platform infra (VPC, EKS, ECR, IAM, CodeBuild)
#   - Orphaned resources (Secrets Manager, SSM params, CloudWatch logs, IAM roles)
#   - Terraform state backend ONLY with --delete-state (kept by default)
#
# Usage:
#   export AWS_PROFILE=<profile> AWS_REGION=<region>
#   ./remove.sh [options]
#
# Options:
#   --env ENV         Environment (default: dev)
#   --yes             Skip confirmation prompt
#   --delete-state    Also delete the Terraform state backend (S3 + lock table).
#                     KEPT by default — state-backend destruction is a separate
#                     deliberate step (see platform/scripts/bootstrap-destroy.sh).
#
# Safety: Prompts for confirmation before destructive operations (unless --yes).
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$SCRIPT_DIR"

ENVIRONMENT="${ENVIRONMENT:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
AUTO_CONFIRM=false
KEEP_STATE=true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)          ENVIRONMENT="$2"; shift 2 ;;
    --region)       AWS_REGION="$2"; shift 2 ;;
    --yes)          AUTO_CONFIRM=true; shift ;;
    --keep-state)   KEEP_STATE=true; shift ;;
    --delete-state) KEEP_STATE=false; shift ;;
    -h|--help)    sed -n '4,25p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

export AWS_REGION ENVIRONMENT
export AWS_PAGER=""  # Disable AWS CLI interactive pager

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
step()  { echo -e "\n${BLUE}════════════════════════════════════════════════════${NC}"; echo -e "${BLUE}  $1${NC}"; echo -e "${BLUE}════════════════════════════════════════════════════${NC}\n"; }
ok()    { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}⚠${NC} $1"; }
fail()  { echo -e "${RED}✗${NC} $1"; exit 1; }

command -v aws >/dev/null 2>&1       || fail "aws CLI not found"
command -v terraform >/dev/null 2>&1 || fail "terraform not found"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text) || fail "AWS credentials not configured"
STATE_BUCKET="adp-terraform-state-${ACCOUNT_ID}"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  ADP DESTROY — Complete Teardown                            ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Account:     $ACCOUNT_ID                          ║"
echo "║  Region:      $AWS_REGION                                 ║"
echo "║  Environment: $ENVIRONMENT                                   ║"
echo "║  State:       $([ "$KEEP_STATE" = true ] && echo 'KEEP' || echo 'DELETE')                                      ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

if [ "$AUTO_CONFIRM" = false ]; then
  echo -e "${RED}This will PERMANENTLY destroy all ADP resources in account $ACCOUNT_ID.${NC}"
  echo -n "Type the account ID to confirm: "
  read -r CONFIRM
  [ "$CONFIRM" = "$ACCOUNT_ID" ] || fail "Account ID mismatch — aborting."
  echo ""
fi

# =============================================================================
# Phase 1: Terraform destroy — webhook-ingress
# =============================================================================
step "Destroying: Webhook-ingress stack"

WH_INFRA="$ROOT_DIR/modules/agent-factory/webhook-ingress/infra"
WH_BACKEND="$ROOT_DIR/environments/${ENVIRONMENT}/modules/webhook-ingress-backend.tfvars"

if [ -f "$WH_BACKEND" ] && aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
  # Empty S3 buckets that terraform can't delete when non-empty
  for bucket in $(aws s3 ls 2>/dev/null | awk '{print $3}' | grep "adp-.*-${ACCOUNT_ID}"); do
    echo "Emptying bucket: $bucket"
    aws s3 rm "s3://$bucket" --recursive --region "$AWS_REGION" 2>/dev/null || true
  done

  ( cd "$WH_INFRA" \
    && terraform init -backend-config="$WH_BACKEND" -input=false -reconfigure 2>/dev/null \
    && terraform destroy -var="environment=${ENVIRONMENT}" -var="aws_region=${AWS_REGION}" \
         -input=false -auto-approve 2>&1 | tail -5 ) \
  && ok "Webhook-ingress destroyed" \
  || warn "Webhook-ingress destroy failed or already empty"
else
  warn "Webhook-ingress backend not found or state bucket missing — skipping"
fi

# =============================================================================
# Phase 2: Terraform destroy — gateway infra
# =============================================================================
step "Destroying: Gateway infra (RDS, Redis, Cognito, CloudFront)"

GW_INFRA="$ROOT_DIR/modules/gateway/infra"
GW_BACKEND="$ROOT_DIR/environments/${ENVIRONMENT}/modules/gateway-backend.tfvars"
GW_VARS="$ROOT_DIR/environments/${ENVIRONMENT}/modules/gateway.tfvars"

if [ -f "$GW_BACKEND" ] && aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
  # Force unlock if needed (previous interrupted destroy)
  LOCK_ID=$(cd "$GW_INFRA" && terraform init -backend-config="$GW_BACKEND" -input=false -reconfigure 2>/dev/null && \
    terraform force-unlock -force "dummy" 2>&1 | grep -o '[a-f0-9-]\{36\}' | head -1 || echo "")
  [ -n "$LOCK_ID" ] && ( cd "$GW_INFRA" && terraform force-unlock -force "$LOCK_ID" 2>/dev/null ) || true

  ( cd "$GW_INFRA" \
    && terraform init -backend-config="$GW_BACKEND" -input=false -reconfigure 2>/dev/null \
    && terraform destroy -var-file="$GW_VARS" -input=false -auto-approve 2>&1 | tail -5 ) \
  && ok "Gateway infra destroyed" \
  || warn "Gateway destroy incomplete — some resources may need manual cleanup"
else
  warn "Gateway backend not found or state bucket missing — skipping"
fi

# =============================================================================
# Phase 3: Delete EKS namespaces + kubectl resources (before platform destroy)
# =============================================================================
step "Cleaning up Kubernetes resources"

# Update kubeconfig (may fail if cluster already gone)
aws eks update-kubeconfig --name "adp-${ENVIRONMENT}-eks-cluster" --region "$AWS_REGION" 2>/dev/null || true

for ns in adp-agents adp-gateway keda; do
  if kubectl get ns "$ns" 2>/dev/null; then
    kubectl delete ns "$ns" --timeout=60s 2>/dev/null || warn "Could not delete namespace $ns"
  fi
done
ok "Kubernetes namespaces cleaned (or cluster already gone)"

# =============================================================================
# Phase 3b: Delete EKS nodegroups (must be gone before cluster can be deleted)
# =============================================================================
EKS_CLUSTER="adp-${ENVIRONMENT}-eks-cluster"
if aws eks describe-cluster --name "$EKS_CLUSTER" --region "$AWS_REGION" >/dev/null 2>&1; then
  echo "Deleting EKS nodegroups (required before cluster deletion)..."
  NODEGROUPS=$(aws eks list-nodegroups --cluster-name "$EKS_CLUSTER" --region "$AWS_REGION" \
    --query 'nodegroups' --output text 2>/dev/null || echo "")
  for ng in $NODEGROUPS; do
    echo "  Deleting nodegroup: $ng"
    aws eks delete-nodegroup --cluster-name "$EKS_CLUSTER" --nodegroup-name "$ng" --region "$AWS_REGION" >/dev/null 2>&1 || true
  done
  # Wait for all nodegroups to be deleted (up to 10 min)
  if [ -n "$NODEGROUPS" ]; then
    echo "  Waiting for nodegroups to delete (up to 10 min)..."
    for i in $(seq 1 40); do
      REMAINING=$(aws eks list-nodegroups --cluster-name "$EKS_CLUSTER" --region "$AWS_REGION" \
        --query 'nodegroups' --output text 2>/dev/null || echo "")
      [ -z "$REMAINING" ] && break
      sleep 15
    done
    [ -z "$REMAINING" ] && ok "All nodegroups deleted" || warn "Nodegroups still deleting — terraform destroy may retry"
  else
    ok "No nodegroups to delete"
  fi
fi

# =============================================================================
# Phase 4: Terraform destroy — platform infra (VPC, EKS, ECR, IAM)
# =============================================================================
step "Destroying: Platform infra (VPC, EKS, ECR, IAM)"

PLAT_INFRA="$ROOT_DIR/platform/infra"
PLAT_BACKEND="$ROOT_DIR/environments/${ENVIRONMENT}/backend.tfvars"
PLAT_VARS="$ROOT_DIR/environments/${ENVIRONMENT}/platform.tfvars"

if [ -f "$PLAT_BACKEND" ] && aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
  MY_IP=$(curl -fsS https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || echo "0.0.0.0")
  export TF_VAR_eks_public_access_cidrs="[\"${MY_IP}/32\"]"

  # Force unlock if needed
  LOCK_ID=$(cd "$PLAT_INFRA" && terraform init -backend-config="$PLAT_BACKEND" -input=false -reconfigure 2>/dev/null && \
    terraform force-unlock -force "dummy" 2>&1 | grep -o '[a-f0-9-]\{36\}' | head -1 || echo "")
  [ -n "$LOCK_ID" ] && ( cd "$PLAT_INFRA" && terraform force-unlock -force "$LOCK_ID" 2>/dev/null ) || true

  ( cd "$PLAT_INFRA" \
    && terraform init -backend-config="$PLAT_BACKEND" -input=false -reconfigure 2>/dev/null \
    && terraform destroy -var-file="$PLAT_VARS" -input=false -auto-approve 2>&1 | tail -5 ) \
  && ok "Platform infra destroyed" \
  || warn "Platform destroy incomplete — cleaning up remaining resources manually"
fi

# =============================================================================
# Phase 5: Force-delete remaining AWS resources
# =============================================================================
step "Cleaning up orphaned resources"

# --- EKS cluster (force-delete if terraform couldn't) ---
echo "EKS clusters..."
if aws eks describe-cluster --name "$EKS_CLUSTER" --region "$AWS_REGION" >/dev/null 2>&1; then
  echo "  Force-deleting EKS cluster $EKS_CLUSTER..."
  aws eks delete-cluster --name "$EKS_CLUSTER" --region "$AWS_REGION" >/dev/null 2>&1 || true
  echo "  Waiting for cluster deletion (up to 10 min)..."
  for i in $(seq 1 40); do
    aws eks describe-cluster --name "$EKS_CLUSTER" --region "$AWS_REGION" >/dev/null 2>&1 || break
    sleep 15
  done
  ok "EKS cluster deleted"
fi

# --- ECR repos (force-delete with images) ---
echo "ECR repositories..."
for repo in $(aws ecr describe-repositories --region "$AWS_REGION" --query 'repositories[?contains(repositoryName,`adp`)].repositoryName' --output text 2>/dev/null); do
  aws ecr delete-repository --repository-name "$repo" --force --region "$AWS_REGION" >/dev/null 2>&1
  ok "Deleted ECR: $repo"
done

# --- CodeBuild projects ---
echo "CodeBuild projects..."
for proj in $(aws codebuild list-projects --region "$AWS_REGION" --query 'projects' --output text 2>/dev/null | tr '\t' '\n' | grep adp); do
  aws codebuild delete-project --name "$proj" --region "$AWS_REGION" >/dev/null 2>&1
  ok "Deleted CodeBuild: $proj"
done

# --- Lambda functions ---
echo "Lambda functions..."
for fn in $(aws lambda list-functions --region "$AWS_REGION" \
  --query 'Functions[?contains(FunctionName,`bedrockgw-`) || contains(FunctionName,`adp-`) || contains(FunctionName,`adp_`)].FunctionName' --output text 2>/dev/null || echo ""); do
  aws lambda delete-function --function-name "$fn" --region "$AWS_REGION" >/dev/null 2>&1
  ok "Deleted Lambda: $fn"
done
# Wait for Lambda ENI cleanup (Hyperplane ENIs go to "available" state after function deletion)
# This is critical — Lambda VPC ENIs can take 5-10 minutes to detach after function deletion
echo "  Waiting for Lambda Hyperplane ENIs to release (checking every 15s, up to 5 min)..."
for i in $(seq 1 20); do
  LAMBDA_ENIS=$(aws ec2 describe-network-interfaces --region "$AWS_REGION" \
    --filters "Name=description,Values=AWS Lambda VPC ENI*" "Name=status,Values=in-use" \
    --query 'NetworkInterfaces[].NetworkInterfaceId' --output text 2>/dev/null || echo "")
  [ -z "$LAMBDA_ENIS" ] && break
  sleep 15
done
ok "Lambda ENIs released (or timed out — will force-delete in VPC cleanup)"

# --- S3 buckets (adp-* and bedrockgw-*) ---
echo "S3 buckets..."
for bucket in $(aws s3 ls 2>/dev/null | awk '{print $3}' | grep -E "^(adp-|bedrockgw-)"); do
  echo "  Emptying and deleting bucket: $bucket"
  # Remove all objects
  aws s3 rm "s3://$bucket" --recursive --region "$AWS_REGION" 2>/dev/null || true
  # Remove versioned objects (required for versioned buckets)
  aws s3api list-object-versions --bucket "$bucket" \
    --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' --output json 2>/dev/null > /tmp/s3-versions.json
  python3 -c "import json; d=json.load(open('/tmp/s3-versions.json')); exit(0 if d.get('Objects') else 1)" 2>/dev/null \
    && aws s3api delete-objects --bucket "$bucket" --delete file:///tmp/s3-versions.json >/dev/null 2>&1 || true
  # Remove delete markers
  aws s3api list-object-versions --bucket "$bucket" \
    --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' --output json 2>/dev/null > /tmp/s3-markers.json
  python3 -c "import json; d=json.load(open('/tmp/s3-markers.json')); exit(0 if d.get('Objects') else 1)" 2>/dev/null \
    && aws s3api delete-objects --bucket "$bucket" --delete file:///tmp/s3-markers.json >/dev/null 2>&1 || true
  # Delete the bucket
  aws s3api delete-bucket --bucket "$bucket" --region "$AWS_REGION" 2>/dev/null \
    && ok "Deleted bucket: $bucket" \
    || warn "Could not delete bucket $bucket (may have remaining versions)"
done
rm -f /tmp/s3-versions.json /tmp/s3-markers.json 2>/dev/null || true

# --- DynamoDB tables (adp-* and bedrockgw-*) ---
echo "DynamoDB tables..."
for table in $(aws dynamodb list-tables --region "$AWS_REGION" \
  --query 'TableNames[?contains(@,`adp-`) || contains(@,`bedrockgw-`)]' --output text 2>/dev/null || echo ""); do
  aws dynamodb delete-table --table-name "$table" --region "$AWS_REGION" >/dev/null 2>&1
  ok "Deleted DynamoDB table: $table"
done

# --- SQS queues (adp-* and bedrockgw-*) ---
echo "SQS queues..."
for queue_url in $(aws sqs list-queues --region "$AWS_REGION" \
  --query 'QueueUrls' --output text 2>/dev/null | tr '\t' '\n' | grep -E "(adp-|bedrockgw-)" || echo ""); do
  aws sqs delete-queue --queue-url "$queue_url" --region "$AWS_REGION" 2>/dev/null
  ok "Deleted SQS queue: $queue_url"
done

# --- Cognito user pools (adp-* and bedrockgw-*) ---
echo "Cognito user pools..."
for pool_id in $(aws cognito-idp list-user-pools --max-results 60 --region "$AWS_REGION" \
  --query 'UserPools[?contains(Name,`adp`) || contains(Name,`bedrockgw`)].Id' --output text 2>/dev/null || echo ""); do
  # Delete domain first (blocks pool deletion)
  DOMAIN=$(aws cognito-idp describe-user-pool --user-pool-id "$pool_id" --region "$AWS_REGION" \
    --query 'UserPool.Domain' --output text 2>/dev/null || echo "")
  [ -n "$DOMAIN" ] && [ "$DOMAIN" != "None" ] && \
    aws cognito-idp delete-user-pool-domain --user-pool-id "$pool_id" --domain "$DOMAIN" --region "$AWS_REGION" 2>/dev/null
  aws cognito-idp delete-user-pool --user-pool-id "$pool_id" --region "$AWS_REGION" 2>/dev/null
  ok "Deleted Cognito pool: $pool_id"
done

# --- CloudFront distributions ---
echo "CloudFront distributions..."
for dist_id in $(aws cloudfront list-distributions \
  --query 'DistributionList.Items[?contains(Comment,`bedrockgw`) || contains(Comment,`adp`)].Id' --output text 2>/dev/null || echo ""); do
  ETAG=$(aws cloudfront get-distribution-config --id "$dist_id" --query 'ETag' --output text 2>/dev/null || echo "")
  if [ -n "$ETAG" ]; then
    # Disable first
    aws cloudfront get-distribution-config --id "$dist_id" --query 'DistributionConfig' --output json 2>/dev/null | \
      python3 -c "import sys,json; d=json.load(sys.stdin); d['Enabled']=False; json.dump(d,open('/tmp/cf-dis.json','w'))" 2>/dev/null
    aws cloudfront update-distribution --id "$dist_id" --if-match "$ETAG" --distribution-config file:///tmp/cf-dis.json >/dev/null 2>&1
    echo "  Disabled $dist_id — waiting for deploy..."
    for i in $(seq 1 20); do
      [ "$(aws cloudfront get-distribution --id "$dist_id" --query 'Distribution.Status' --output text 2>/dev/null)" = "Deployed" ] && break
      sleep 15
    done
    ETAG=$(aws cloudfront get-distribution-config --id "$dist_id" --query 'ETag' --output text 2>/dev/null || echo "")
    aws cloudfront delete-distribution --id "$dist_id" --if-match "$ETAG" >/dev/null 2>&1
    ok "Deleted CloudFront: $dist_id"
  fi
done
# Delete VPC origins
for vo in $(aws cloudfront list-vpc-origins --query 'VpcOriginList.Items[].Id' --output text 2>/dev/null || echo ""); do
  ETAG=$(aws cloudfront get-vpc-origin --id "$vo" --query 'ETag' --output text 2>/dev/null || echo "")
  [ -n "$ETAG" ] && aws cloudfront delete-vpc-origin --id "$vo" --if-match "$ETAG" >/dev/null 2>&1
  ok "Deleted VPC origin: $vo"
done
# Delete Origin Access Controls (OAC)
for oac_id in $(aws cloudfront list-origin-access-controls \
  --query "OriginAccessControlList.Items[?contains(Name,'bedrockgw') || contains(Name,'adp')].Id" --output text 2>/dev/null || echo ""); do
  ETAG=$(aws cloudfront get-origin-access-control --id "$oac_id" --query 'ETag' --output text 2>/dev/null || echo "")
  [ -n "$ETAG" ] && aws cloudfront delete-origin-access-control --id "$oac_id" --if-match "$ETAG" >/dev/null 2>&1
  ok "Deleted OAC: $oac_id"
done
# Delete Response Headers Policies
for rhp_id in $(aws cloudfront list-response-headers-policies --type custom \
  --query "ResponseHeadersPolicyList.Items[?ResponseHeadersPolicy.ResponseHeadersPolicyConfig.Name && (contains(ResponseHeadersPolicy.ResponseHeadersPolicyConfig.Name,'bedrockgw') || contains(ResponseHeadersPolicy.ResponseHeadersPolicyConfig.Name,'adp'))].ResponseHeadersPolicy.Id" --output text 2>/dev/null || echo ""); do
  ETAG=$(aws cloudfront get-response-headers-policy --id "$rhp_id" --query 'ETag' --output text 2>/dev/null || echo "")
  [ -n "$ETAG" ] && aws cloudfront delete-response-headers-policy --id "$rhp_id" --if-match "$ETAG" >/dev/null 2>&1
  ok "Deleted Response Headers Policy: $rhp_id"
done
# Delete CloudFront Functions
for fn_name in $(aws cloudfront list-functions --query "FunctionList.Items[?contains(Name,'bedrockgw') || contains(Name,'adp')].Name" --output text 2>/dev/null || echo ""); do
  ETAG=$(aws cloudfront describe-function --name "$fn_name" --query 'ETag' --output text 2>/dev/null || echo "")
  [ -n "$ETAG" ] && aws cloudfront delete-function --name "$fn_name" --if-match "$ETAG" >/dev/null 2>&1
  ok "Deleted CloudFront function: $fn_name"
done

# --- API Gateway (REST + HTTP APIs + VPC Links) ---
echo "API Gateway..."
for api in $(aws apigateway get-rest-apis --region "$AWS_REGION" \
  --query 'items[?contains(name,`bedrockgw`) || contains(name,`adp-dev`)].id' --output text 2>/dev/null || echo ""); do
  aws apigateway delete-rest-api --rest-api-id "$api" --region "$AWS_REGION" >/dev/null 2>&1
  ok "Deleted REST API: $api"
  sleep 30  # API GW rate limit: 1 delete per 30s
done
for vl in $(aws apigatewayv2 get-vpc-links --region "$AWS_REGION" --query 'Items[].VpcLinkId' --output text 2>/dev/null || echo ""); do
  aws apigatewayv2 delete-vpc-link --vpc-link-id "$vl" --region "$AWS_REGION" >/dev/null 2>&1
  ok "Deleted VPC Link: $vl"
done

# --- WAFv2 WebACLs (adp-* and bedrockgw-*) ---
echo "WAFv2 WebACLs..."
for waf_entry in $(aws wafv2 list-web-acls --scope REGIONAL --region "$AWS_REGION" \
  --query "WebACLs[?contains(Name,'adp') || contains(Name,'bedrockgw')].[Id,Name,LockToken]" --output text 2>/dev/null || echo ""); do
  :  # list-web-acls with multiple columns uses tabs; parse below
done
aws wafv2 list-web-acls --scope REGIONAL --region "$AWS_REGION" \
  --query "WebACLs[?contains(Name,'adp') || contains(Name,'bedrockgw')].[Id,Name,LockToken]" --output text 2>/dev/null | \
while IFS=$'\t' read -r WAF_ID WAF_NAME WAF_LOCK; do
  [ -z "$WAF_ID" ] && continue
  # Disassociate from any resources first
  ASSOC_RESOURCES=$(aws wafv2 list-resources-for-web-acl --web-acl-arn "arn:aws:wafv2:${AWS_REGION}:${ACCOUNT_ID}:regional/webacl/${WAF_NAME}/${WAF_ID}" \
    --resource-type API_GATEWAY --region "$AWS_REGION" --query 'ResourceArns' --output text 2>/dev/null || echo "")
  for res_arn in $ASSOC_RESOURCES; do
    aws wafv2 disassociate-web-acl --resource-arn "$res_arn" --region "$AWS_REGION" 2>/dev/null || true
  done
  aws wafv2 delete-web-acl --id "$WAF_ID" --name "$WAF_NAME" --scope REGIONAL --lock-token "$WAF_LOCK" --region "$AWS_REGION" >/dev/null 2>&1
  ok "Deleted WAFv2 WebACL: $WAF_NAME"
done

# --- Secrets Manager (restore scheduled-for-deletion, then force-delete all) ---
echo "Secrets Manager..."
# First restore any scheduled-for-deletion secrets (can't re-create until fully gone)
for secret in $(aws secretsmanager list-secrets --include-planned-deletion --region "$AWS_REGION" \
  --query 'SecretList[?DeletedDate!=null && (contains(Name,`adp/`) || contains(Name,`bedrockgw`))].Name' --output text 2>/dev/null || echo ""); do
  aws secretsmanager restore-secret --secret-id "$secret" --region "$AWS_REGION" >/dev/null 2>&1 || true
done
sleep 5
# Now force-delete all ADP secrets
for secret in $(aws secretsmanager list-secrets --region "$AWS_REGION" \
  --query 'SecretList[?contains(Name,`adp/`) || contains(Name,`bedrockgw`)].Name' --output text 2>/dev/null || echo ""); do
  aws secretsmanager delete-secret --secret-id "$secret" --force-delete-without-recovery --region "$AWS_REGION" >/dev/null 2>&1
  ok "Deleted secret: $secret"
done

# --- SSM Parameters ---
echo "SSM parameters..."
SSM_PARAMS=$(aws ssm describe-parameters --region "$AWS_REGION" \
  --query 'Parameters[?starts_with(Name,`/adp/`)].Name' --output text 2>/dev/null || echo "")
for param in $SSM_PARAMS; do
  aws ssm delete-parameter --name "$param" --region "$AWS_REGION" 2>/dev/null
done
[ -n "$SSM_PARAMS" ] && ok "Deleted SSM parameters" || ok "No SSM parameters to delete"

# --- CloudWatch log groups ---
echo "CloudWatch log groups..."
for prefix in "/aws/codebuild/adp" "/aws/lambda/adp" "/aws/lambda/bedrockgw" "/aws/eks/adp" "/aws/ecr/adp" "/aws/rds/instance/bedrockgw" "/aws/api-gateway/bedrockgw" "/aws/apigateway/adp" "/adp/"; do
  LOG_GROUPS=$(aws logs describe-log-groups --log-group-name-prefix "$prefix" --region "$AWS_REGION" \
    --query 'logGroups[].logGroupName' --output text 2>/dev/null || echo "")
  for lg in $LOG_GROUPS; do
    aws logs delete-log-group --log-group-name "$lg" --region "$AWS_REGION" 2>/dev/null
  done
done
ok "CloudWatch log groups cleaned"

# --- KMS aliases ---
echo "KMS aliases..."
for alias in $(aws kms list-aliases --region "$AWS_REGION" --query 'Aliases[?starts_with(AliasName,`alias/adp`)].AliasName' --output text 2>/dev/null || echo ""); do
  aws kms delete-alias --alias-name "$alias" --region "$AWS_REGION" 2>/dev/null
  ok "Deleted KMS alias: $alias"
done

# --- RDS leftovers (subnet groups, parameter groups) ---
echo "RDS leftovers..."
# Force-delete RDS instances that terraform missed
for db in $(aws rds describe-db-instances --region "$AWS_REGION" \
  --query 'DBInstances[?contains(DBInstanceIdentifier,`bedrockgw`)].DBInstanceIdentifier' --output text 2>/dev/null || echo ""); do
  echo "  Deleting RDS instance: $db"
  aws rds delete-db-instance --db-instance-identifier "$db" --skip-final-snapshot --delete-automated-backups --region "$AWS_REGION" 2>/dev/null || true
  # Wait for deletion (up to 10 min)
  for i in $(seq 1 40); do
    aws rds describe-db-instances --db-instance-identifier "$db" --region "$AWS_REGION" >/dev/null 2>&1 || break
    sleep 15
  done
  ok "Deleted RDS: $db"
done
for sg in $(aws rds describe-db-subnet-groups --region "$AWS_REGION" \
  --query 'DBSubnetGroups[?contains(DBSubnetGroupName,`bedrockgw`)].DBSubnetGroupName' --output text 2>/dev/null || echo ""); do
  aws rds delete-db-subnet-group --db-subnet-group-name "$sg" --region "$AWS_REGION" 2>/dev/null
  ok "Deleted RDS subnet group: $sg"
done
for pg in $(aws rds describe-db-parameter-groups --region "$AWS_REGION" \
  --query 'DBParameterGroups[?contains(DBParameterGroupName,`bedrockgw`)].DBParameterGroupName' --output text 2>/dev/null || echo ""); do
  aws rds delete-db-parameter-group --db-parameter-group-name "$pg" --region "$AWS_REGION" 2>/dev/null
  ok "Deleted RDS param group: $pg"
done

# --- ElastiCache leftovers ---
echo "ElastiCache..."
for cache in $(aws elasticache describe-replication-groups --region "$AWS_REGION" \
  --query 'ReplicationGroups[?contains(ReplicationGroupId,`bedrockgw`)].ReplicationGroupId' --output text 2>/dev/null || echo ""); do
  aws elasticache delete-replication-group --replication-group-id "$cache" --region "$AWS_REGION" 2>/dev/null
  ok "Deleting ElastiCache: $cache"
done
# Wait for ElastiCache to finish deleting before removing subnet groups
if [ -n "$(aws elasticache describe-replication-groups --region "$AWS_REGION" --query 'ReplicationGroups[?contains(ReplicationGroupId,`bedrockgw`)].ReplicationGroupId' --output text 2>/dev/null)" ]; then
  echo "  Waiting for ElastiCache deletion (up to 5 min)..."
  for i in $(seq 1 20); do
    REMAINING=$(aws elasticache describe-replication-groups --region "$AWS_REGION" --query 'ReplicationGroups[?contains(ReplicationGroupId,`bedrockgw`)].ReplicationGroupId' --output text 2>/dev/null || echo "")
    [ -z "$REMAINING" ] && break
    sleep 15
  done
fi
# Delete cache subnet groups
for csg in $(aws elasticache describe-cache-subnet-groups --region "$AWS_REGION" \
  --query 'CacheSubnetGroups[?contains(CacheSubnetGroupName,`bedrockgw`)].CacheSubnetGroupName' --output text 2>/dev/null || echo ""); do
  aws elasticache delete-cache-subnet-group --cache-subnet-group-name "$csg" --region "$AWS_REGION" 2>/dev/null
  ok "Deleted cache subnet group: $csg"
done
# Delete ElastiCache users (created by gateway terraform — survive replication group deletion)
for user_id in $(aws elasticache describe-users --region "$AWS_REGION" \
  --query "Users[?contains(UserId,'bedrockgw') || contains(UserId,'adp')].UserId" --output text 2>/dev/null || echo ""); do
  aws elasticache delete-user --user-id "$user_id" --region "$AWS_REGION" >/dev/null 2>&1
  ok "Deleted ElastiCache user: $user_id"
done
# Delete ElastiCache user groups (must be deleted before users on re-deploy)
for ug in $(aws elasticache describe-user-groups --region "$AWS_REGION" \
  --query "UserGroups[?contains(UserGroupId,'bedrockgw') || contains(UserGroupId,'adp')].UserGroupId" --output text 2>/dev/null || echo ""); do
  aws elasticache delete-user-group --user-group-id "$ug" --region "$AWS_REGION" >/dev/null 2>&1
  ok "Deleted ElastiCache user group: $ug"
done
# Delete ElastiCache parameter groups
for pg in $(aws elasticache describe-cache-parameter-groups --region "$AWS_REGION" \
  --query "CacheParameterGroups[?contains(CacheParameterGroupName,'bedrockgw') || contains(CacheParameterGroupName,'adp')].CacheParameterGroupName" --output text 2>/dev/null || echo ""); do
  aws elasticache delete-cache-parameter-group --cache-parameter-group-name "$pg" --region "$AWS_REGION" >/dev/null 2>&1
  ok "Deleted ElastiCache param group: $pg"
done

# --- IAM roles (adp-* and bedrockgw-*) ---
echo "IAM roles..."
for role in $(aws iam list-roles --query 'Roles[?starts_with(RoleName,`adp-`) || starts_with(RoleName,`bedrockgw-`)].RoleName' --output text 2>/dev/null || echo ""); do
  # Detach managed policies
  for pol in $(aws iam list-attached-role-policies --role-name "$role" --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null || echo ""); do
    aws iam detach-role-policy --role-name "$role" --policy-arn "$pol" 2>/dev/null
  done
  # Delete inline policies
  for pol in $(aws iam list-role-policies --role-name "$role" --query 'PolicyNames' --output text 2>/dev/null || echo ""); do
    aws iam delete-role-policy --role-name "$role" --policy-name "$pol" 2>/dev/null
  done
  # Remove from instance profiles and delete them
  for ip in $(aws iam list-instance-profiles-for-role --role-name "$role" --query 'InstanceProfiles[].InstanceProfileName' --output text 2>/dev/null || echo ""); do
    aws iam remove-role-from-instance-profile --instance-profile-name "$ip" --role-name "$role" 2>/dev/null
    aws iam delete-instance-profile --instance-profile-name "$ip" 2>/dev/null
  done
  aws iam delete-role --role-name "$role" 2>/dev/null && ok "Deleted role: $role"
done

# --- IAM instance profiles (orphaned) ---
for ip in $(aws iam list-instance-profiles --query 'InstanceProfiles[?starts_with(InstanceProfileName,`adp-`)].InstanceProfileName' --output text 2>/dev/null || echo ""); do
  aws iam delete-instance-profile --instance-profile-name "$ip" 2>/dev/null
  ok "Deleted instance profile: $ip"
done

# --- IAM policies (adp-* and bedrockgw-*) ---
echo "IAM policies..."
for pol in $(aws iam list-policies --scope Local \
  --query 'Policies[?starts_with(PolicyName,`adp-`) || starts_with(PolicyName,`bedrockgw-`)].Arn' --output text 2>/dev/null || echo ""); do
  for v in $(aws iam list-policy-versions --policy-arn "$pol" --query 'Versions[?!IsDefaultVersion].VersionId' --output text 2>/dev/null || echo ""); do
    aws iam delete-policy-version --policy-arn "$pol" --version-id "$v" 2>/dev/null
  done
  aws iam delete-policy --policy-arn "$pol" 2>/dev/null
done
ok "IAM policies cleaned"

# --- OIDC providers (from EKS) ---
echo "OIDC providers..."
for arn in $(aws iam list-open-id-connect-providers --query 'OpenIDConnectProviderList[].Arn' --output text 2>/dev/null || echo ""); do
  if echo "$arn" | grep -q "eks"; then
    aws iam delete-open-id-connect-provider --open-id-connect-provider-arn "$arn" 2>/dev/null
    ok "Deleted OIDC: $arn"
  fi
done

# --- VPCs (non-default) — thorough cleanup ---
echo "VPCs..."
VPC_IDS=$(aws ec2 describe-vpcs --region "$AWS_REGION" \
  --filters "Name=isDefault,Values=false" \
  --query 'Vpcs[].VpcId' --output text 2>/dev/null || echo "")
for vpc in $VPC_IDS; do
  echo "  Cleaning VPC $vpc..."
  # Delete VPC endpoints first (they hold ENIs that block SG/subnet deletion)
  VPCES=$(aws ec2 describe-vpc-endpoints --region "$AWS_REGION" --filters "Name=vpc-id,Values=$vpc" \
    --query 'VpcEndpoints[].VpcEndpointId' --output text 2>/dev/null || echo "")
  [ -n "$VPCES" ] && aws ec2 delete-vpc-endpoints --vpc-endpoint-ids $VPCES --region "$AWS_REGION" >/dev/null 2>&1
  # Delete NAT gateways
  for nat in $(aws ec2 describe-nat-gateways --region "$AWS_REGION" --filter "Name=vpc-id,Values=$vpc" \
    --query 'NatGateways[?State!=`deleted`].NatGatewayId' --output text 2>/dev/null || echo ""); do
    aws ec2 delete-nat-gateway --nat-gateway-id "$nat" --region "$AWS_REGION" >/dev/null 2>&1
  done
  # Delete ELBs (ALB/NLB) in the VPC — they hold ENIs that block subnet/SG deletion
  echo "  Deleting load balancers in VPC $vpc..."
  LB_ARNS=$(aws elbv2 describe-load-balancers --region "$AWS_REGION" \
    --query "LoadBalancers[?VpcId=='$vpc'].LoadBalancerArn" --output text 2>/dev/null || echo "")
  for lb in $LB_ARNS; do
    aws elbv2 delete-load-balancer --load-balancer-arn "$lb" --region "$AWS_REGION" >/dev/null 2>&1
    ok "Deleted load balancer: $lb"
  done
  # Delete target groups in the VPC
  for tg in $(aws elbv2 describe-target-groups --region "$AWS_REGION" \
    --query "TargetGroups[?VpcId=='$vpc'].TargetGroupArn" --output text 2>/dev/null || echo ""); do
    aws elbv2 delete-target-group --target-group-arn "$tg" --region "$AWS_REGION" >/dev/null 2>&1
  done
done

# Wait for NAT gateways and load balancers to fully delete before proceeding
if [ -n "$VPC_IDS" ]; then
  echo "  Waiting for NAT gateways and load balancers to delete (up to 3 min)..."
  for i in $(seq 1 12); do
    ALL_GONE=true
    for vpc in $VPC_IDS; do
      NATS=$(aws ec2 describe-nat-gateways --region "$AWS_REGION" --filter "Name=vpc-id,Values=$vpc" \
        --query 'NatGateways[?State!=`deleted`].NatGatewayId' --output text 2>/dev/null || echo "")
      LBS=$(aws elbv2 describe-load-balancers --region "$AWS_REGION" \
        --query "LoadBalancers[?VpcId=='$vpc'].LoadBalancerArn" --output text 2>/dev/null || echo "")
      [ -n "$NATS" ] || [ -n "$LBS" ] && ALL_GONE=false && break
    done
    [ "$ALL_GONE" = true ] && break
    sleep 15
  done
fi

# Force-delete orphaned Lambda Hyperplane ENIs and any other lingering ENIs
for vpc in $VPC_IDS; do
  echo "  Deleting orphaned ENIs in VPC $vpc..."
  ENIS=$(aws ec2 describe-network-interfaces --region "$AWS_REGION" \
    --filters "Name=vpc-id,Values=$vpc" \
    --query 'NetworkInterfaces[].{Id:NetworkInterfaceId,Status:Status,AttachId:Attachment.AttachmentId}' \
    --output json 2>/dev/null || echo "[]")
  echo "$ENIS" | python3 -c "
import json, sys, subprocess
enis = json.load(sys.stdin)
for eni in enis:
    eni_id = eni['Id']
    status = eni['Status']
    attach_id = eni.get('AttachId')
    # Detach if still attached (force for Lambda/EKS ENIs)
    if status == 'in-use' and attach_id:
        subprocess.run([
            'aws', 'ec2', 'detach-network-interface',
            '--attachment-id', attach_id, '--force',
            '--region', '$AWS_REGION'
        ], capture_output=True)
        # Brief wait for detach to take effect
        import time; time.sleep(5)
    # Delete the ENI
    result = subprocess.run([
        'aws', 'ec2', 'delete-network-interface',
        '--network-interface-id', eni_id,
        '--region', '$AWS_REGION'
    ], capture_output=True, text=True)
    if result.returncode == 0:
        print(f'    Deleted ENI: {eni_id}')
    else:
        print(f'    Could not delete ENI {eni_id}: {result.stderr.strip()}')
" 2>/dev/null || true
done

# Brief wait for any final ENI cleanup
if [ -n "$VPC_IDS" ]; then
  echo "  Waiting 30s for ENI state propagation..."
  sleep 30
fi

for vpc in $VPC_IDS; do
  # Release EIPs associated with this VPC (via ENI or unattached in the VPC subnets)
  for eip in $(aws ec2 describe-addresses --region "$AWS_REGION" --filters "Name=domain,Values=vpc" \
    --query 'Addresses[?AssociationId==null].AllocationId' --output text 2>/dev/null || echo ""); do
    aws ec2 release-address --allocation-id "$eip" --region "$AWS_REGION" 2>/dev/null
  done
  # Detach and delete internet gateways
  for igw in $(aws ec2 describe-internet-gateways --region "$AWS_REGION" --filters "Name=attachment.vpc-id,Values=$vpc" \
    --query 'InternetGateways[].InternetGatewayId' --output text 2>/dev/null || echo ""); do
    aws ec2 detach-internet-gateway --internet-gateway-id "$igw" --vpc-id "$vpc" --region "$AWS_REGION" 2>/dev/null
    aws ec2 delete-internet-gateway --internet-gateway-id "$igw" --region "$AWS_REGION" 2>/dev/null
  done
  # Delete subnets (retry once after ENI cleanup)
  for sub in $(aws ec2 describe-subnets --region "$AWS_REGION" --filters "Name=vpc-id,Values=$vpc" \
    --query 'Subnets[].SubnetId' --output text 2>/dev/null || echo ""); do
    aws ec2 delete-subnet --subnet-id "$sub" --region "$AWS_REGION" 2>/dev/null || true
  done
  # Delete non-main route tables
  for rt in $(aws ec2 describe-route-tables --region "$AWS_REGION" --filters "Name=vpc-id,Values=$vpc" \
    --query 'RouteTables[?Associations[0].Main!=`true`].RouteTableId' --output text 2>/dev/null || echo ""); do
    # Disassociate explicit associations first
    for assoc in $(aws ec2 describe-route-tables --region "$AWS_REGION" --route-table-ids "$rt" \
      --query 'RouteTables[0].Associations[?!Main].RouteTableAssociationId' --output text 2>/dev/null || echo ""); do
      aws ec2 disassociate-route-table --association-id "$assoc" --region "$AWS_REGION" 2>/dev/null
    done
    aws ec2 delete-route-table --route-table-id "$rt" --region "$AWS_REGION" 2>/dev/null
  done
  # Delete security groups (revoke ALL rules first to break circular refs)
  SG_IDS=$(aws ec2 describe-security-groups --region "$AWS_REGION" --filters "Name=vpc-id,Values=$vpc" \
    --query 'SecurityGroups[?GroupName!=`default`].GroupId' --output text 2>/dev/null || echo "")
  # First pass: revoke all ingress AND egress rules on every SG (breaks circular dependencies)
  for sg in $SG_IDS; do
    # Revoke ingress rules
    INGRESS_RULES=$(aws ec2 describe-security-group-rules --region "$AWS_REGION" --filters "Name=group-id,Values=$sg" \
      --query 'SecurityGroupRules[?!IsEgress].SecurityGroupRuleId' --output text 2>/dev/null || echo "")
    for rule in $INGRESS_RULES; do
      aws ec2 revoke-security-group-ingress --group-id "$sg" --security-group-rule-ids "$rule" --region "$AWS_REGION" 2>/dev/null || true
    done
    # Revoke egress rules
    EGRESS_RULES=$(aws ec2 describe-security-group-rules --region "$AWS_REGION" --filters "Name=group-id,Values=$sg" \
      --query 'SecurityGroupRules[?IsEgress].SecurityGroupRuleId' --output text 2>/dev/null || echo "")
    for rule in $EGRESS_RULES; do
      aws ec2 revoke-security-group-egress --group-id "$sg" --security-group-rule-ids "$rule" --region "$AWS_REGION" 2>/dev/null || true
    done
  done
  # Second pass: delete the security groups
  for sg in $SG_IDS; do
    aws ec2 delete-security-group --group-id "$sg" --region "$AWS_REGION" 2>/dev/null || true
  done
  # Retry subnet deletion (in case SG/ENI dependencies just cleared)
  for sub in $(aws ec2 describe-subnets --region "$AWS_REGION" --filters "Name=vpc-id,Values=$vpc" \
    --query 'Subnets[].SubnetId' --output text 2>/dev/null || echo ""); do
    aws ec2 delete-subnet --subnet-id "$sub" --region "$AWS_REGION" 2>/dev/null || warn "Subnet $sub still blocked"
  done
  # Delete VPC
  aws ec2 delete-vpc --vpc-id "$vpc" --region "$AWS_REGION" 2>/dev/null && ok "Deleted VPC: $vpc" || warn "VPC $vpc still has dependencies — see retry below"
done

# Retry pass: if VPCs are still present (edge case: ENI detach propagation delay)
sleep 10
for vpc in $VPC_IDS; do
  if aws ec2 describe-vpcs --region "$AWS_REGION" --vpc-ids "$vpc" >/dev/null 2>&1; then
    echo "  Retrying cleanup for VPC $vpc (ENI propagation delay)..."
    # Final attempt to delete any remaining ENIs
    for eni in $(aws ec2 describe-network-interfaces --region "$AWS_REGION" \
      --filters "Name=vpc-id,Values=$vpc" --query 'NetworkInterfaces[].NetworkInterfaceId' --output text 2>/dev/null || echo ""); do
      aws ec2 delete-network-interface --network-interface-id "$eni" --region "$AWS_REGION" 2>/dev/null || true
    done
    sleep 5
    # Final attempt at subnets and SGs
    for sub in $(aws ec2 describe-subnets --region "$AWS_REGION" --filters "Name=vpc-id,Values=$vpc" \
      --query 'Subnets[].SubnetId' --output text 2>/dev/null || echo ""); do
      aws ec2 delete-subnet --subnet-id "$sub" --region "$AWS_REGION" 2>/dev/null || true
    done
    for sg in $(aws ec2 describe-security-groups --region "$AWS_REGION" --filters "Name=vpc-id,Values=$vpc" \
      --query 'SecurityGroups[?GroupName!=`default`].GroupId' --output text 2>/dev/null || echo ""); do
      aws ec2 delete-security-group --group-id "$sg" --region "$AWS_REGION" 2>/dev/null || true
    done
    aws ec2 delete-vpc --vpc-id "$vpc" --region "$AWS_REGION" 2>/dev/null && ok "Deleted VPC: $vpc (retry succeeded)" || warn "VPC $vpc requires manual cleanup"
  fi
done

# =============================================================================
# Phase 6: Check us-east-1 for CFN stacks from "Connect AWS Account" flow
# =============================================================================
if [ "$AWS_REGION" != "us-east-1" ]; then
  echo ""
  echo "Checking us-east-1 for ADP CloudFormation stacks..."
  CFN_STACKS=$(aws cloudformation list-stacks --region us-east-1 \
    --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
    --query 'StackSummaries[?starts_with(StackName,`ADP-`)].StackName' --output text 2>/dev/null || echo "")
  for stack in $CFN_STACKS; do
    aws cloudformation delete-stack --stack-name "$stack" --region us-east-1 2>/dev/null
    ok "Deleting CFN stack (us-east-1): $stack"
  done
fi

# Also check target region
CFN_STACKS=$(aws cloudformation list-stacks --region "$AWS_REGION" \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
  --query 'StackSummaries[?starts_with(StackName,`ADP-`)].StackName' --output text 2>/dev/null || echo "")
for stack in $CFN_STACKS; do
  aws cloudformation delete-stack --stack-name "$stack" --region "$AWS_REGION" 2>/dev/null
  ok "Deleting CFN stack ($AWS_REGION): $stack"
done

# Clean secrets in us-east-1 too (Connect AWS flow creates them there)
if [ "$AWS_REGION" != "us-east-1" ]; then
  EAST_SECRETS=$(aws secretsmanager list-secrets --region us-east-1 \
    --query 'SecretList[?contains(Name,`adp/`)].Name' --output text 2>/dev/null || echo "")
  for secret in $EAST_SECRETS; do
    aws secretsmanager delete-secret --secret-id "$secret" --force-delete-without-recovery --region us-east-1 2>/dev/null
  done
  [ -n "$EAST_SECRETS" ] && ok "Cleaned secrets in us-east-1"
fi

# =============================================================================
# Phase 7: Delete Terraform state backend
# =============================================================================
if [ "$KEEP_STATE" = true ]; then
  warn "Keeping Terraform state backend (default; pass --delete-state or run bootstrap-destroy.sh to remove it)"
else
  step "Destroying: Terraform state backend"

  if aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
    echo "Emptying state bucket..."
    aws s3 rm "s3://$STATE_BUCKET" --recursive --region "$AWS_REGION" 2>/dev/null || true

    # Delete versioned objects
    aws s3api list-object-versions --bucket "$STATE_BUCKET" \
      --query '{Objects: Versions[].{Key:Key,VersionId:VersionId}}' --output json 2>/dev/null > /tmp/versions.json
    python3 -c "
import json
d = json.load(open('/tmp/versions.json'))
if d.get('Objects'): print(f'Deleting {len(d[\"Objects\"])} versioned objects...')
" 2>/dev/null || true
    python3 -c "import json; d=json.load(open('/tmp/versions.json')); exit(0 if d.get('Objects') else 1)" 2>/dev/null \
      && aws s3api delete-objects --bucket "$STATE_BUCKET" --delete file:///tmp/versions.json >/dev/null 2>&1 || true

    # Delete markers
    aws s3api list-object-versions --bucket "$STATE_BUCKET" \
      --query '{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}' --output json 2>/dev/null > /tmp/markers.json
    python3 -c "import json; d=json.load(open('/tmp/markers.json')); exit(0 if d.get('Objects') else 1)" 2>/dev/null \
      && aws s3api delete-objects --bucket "$STATE_BUCKET" --delete file:///tmp/markers.json >/dev/null 2>&1 || true

    aws s3api delete-bucket --bucket "$STATE_BUCKET" --region "$AWS_REGION" 2>/dev/null \
      && ok "State bucket deleted" \
      || warn "Could not delete state bucket (may have remaining versions)"
  else
    ok "State bucket already gone"
  fi

  # DynamoDB lock table
  if aws dynamodb describe-table --table-name adp-terraform-locks --region "$AWS_REGION" >/dev/null 2>&1; then
    aws dynamodb delete-table --table-name adp-terraform-locks --region "$AWS_REGION" >/dev/null 2>&1
    ok "DynamoDB lock table deleted"
  else
    ok "DynamoDB lock table already gone"
  fi
fi

# =============================================================================
# Final verification
# =============================================================================
step "Final Verification"

echo "Checking for remaining ADP resources..."
REMAINING=0

check() {
  local label="$1" result="$2"
  if [ -n "$result" ] && [ "$result" != "None" ] && [ "$result" != "[]" ] && [ "$result" != "null" ]; then
    warn "$label: $result"
    REMAINING=$((REMAINING+1))
  else
    ok "$label: clean"
  fi
}

check "EKS clusters" "$(aws eks list-clusters --region "$AWS_REGION" --query 'clusters[?contains(@,`adp`)]' --output text 2>/dev/null)"
check "ECR repos" "$(aws ecr describe-repositories --region "$AWS_REGION" --query 'repositories[?contains(repositoryName,`adp`)].repositoryName' --output text 2>/dev/null)"
check "RDS instances" "$(aws rds describe-db-instances --region "$AWS_REGION" --query 'DBInstances[?contains(DBInstanceIdentifier,`bedrockgw`)].DBInstanceIdentifier' --output text 2>/dev/null)"
check "DynamoDB tables" "$(aws dynamodb list-tables --region "$AWS_REGION" --query 'TableNames[?contains(@,`adp`) || contains(@,`bedrockgw`)]' --output text 2>/dev/null)"
check "S3 buckets" "$(aws s3 ls 2>/dev/null | awk '{print $3}' | grep -E '^(adp-|bedrockgw-)' | tr '\n' ' ')"
check "IAM roles" "$(aws iam list-roles --query 'Roles[?starts_with(RoleName,`adp-`) || starts_with(RoleName,`bedrockgw-`)].RoleName' --output text 2>/dev/null)"
check "KMS aliases" "$(aws kms list-aliases --region "$AWS_REGION" --query 'Aliases[?starts_with(AliasName,`alias/adp`)].AliasName' --output text 2>/dev/null)"
check "CodeBuild" "$(aws codebuild list-projects --region "$AWS_REGION" --query 'projects' --output text 2>/dev/null | tr '\t' '\n' | grep adp || true)"
check "VPCs (non-default)" "$(aws ec2 describe-vpcs --region "$AWS_REGION" --filters 'Name=isDefault,Values=false' --query 'Vpcs[].VpcId' --output text 2>/dev/null)"
check "ENIs (orphaned)" "$(aws ec2 describe-network-interfaces --region "$AWS_REGION" --filters 'Name=description,Values=AWS Lambda VPC ENI*' --query 'NetworkInterfaces[].NetworkInterfaceId' --output text 2>/dev/null)"
check "Lambda functions" "$(aws lambda list-functions --region "$AWS_REGION" --query 'Functions[?contains(FunctionName,`bedrockgw-`) || contains(FunctionName,`adp-`)].FunctionName' --output text 2>/dev/null)"
check "CloudFront" "$(aws cloudfront list-distributions --query 'DistributionList.Items[].Id' --output text 2>/dev/null)"

echo ""
if [ "$REMAINING" -eq 0 ]; then
  ok "Account cleanup complete — no ADP resources remain."
else
  warn "$REMAINING resource type(s) may need manual cleanup."
fi

# Cleanup temp files
rm -f /tmp/versions.json /tmp/markers.json 2>/dev/null || true

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Teardown complete."
echo ""
echo "  NOT deleted (manual cleanup if desired):"
echo "    - GitHub App: delete at https://github.com/settings/apps"
echo "    - GitHub App installations on repos"
echo "═══════════════════════════════════════════════════════════════"
