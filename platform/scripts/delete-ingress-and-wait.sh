#!/bin/bash
set -euo pipefail

# =============================================================================
# delete-ingress-and-wait.sh — Delete K8s Ingress and wait for ALB cleanup
# =============================================================================
# Deletes all Ingress resources in the adp-gateway namespace and waits for the
# EKS Ingress controller to fully remove the associated ALB. This MUST run
# BEFORE terraform destroy on the gateway module to avoid orphaned ALBs,
# target groups, and security groups.
#
# Usage:
#   ./delete-ingress-and-wait.sh
#
# Environment variables:
#   AWS_REGION  — AWS region (default: us-east-1)
#   NAMESPACE   — Kubernetes namespace (default: adp-gateway)
#   TIMEOUT     — Max wait time in seconds (default: 300)
#
# Idempotent: no-op if no Ingress resources or namespace doesn't exist.
# =============================================================================

AWS_REGION="${AWS_REGION:-us-east-1}"
NAMESPACE="${NAMESPACE:-adp-gateway}"
TIMEOUT="${TIMEOUT:-300}"

echo "=== Delete Ingress and wait for ALB cleanup ==="
echo "Namespace: $NAMESPACE | Region: $AWS_REGION | Timeout: ${TIMEOUT}s"

# Check if kubectl is available and cluster is reachable
if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl not found. Skipping Ingress cleanup."
  exit 0
fi

if ! kubectl cluster-info >/dev/null 2>&1; then
  echo "Cannot reach Kubernetes cluster. Skipping Ingress cleanup."
  exit 0
fi

# Check if namespace exists
if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
  echo "Namespace '$NAMESPACE' does not exist. Nothing to clean up."
  exit 0
fi

# Check for existing Ingress resources
INGRESS_COUNT=$(kubectl get ingress -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l || echo "0")
if [ "$INGRESS_COUNT" -eq 0 ]; then
  echo "No Ingress resources in namespace '$NAMESPACE'. Nothing to clean up."
  exit 0
fi

echo "Found $INGRESS_COUNT Ingress resource(s). Discovering ALBs before deletion..."

# Discover ALBs associated with the gateway before deleting Ingress
# Look for ALBs tagged with the ingress class or matching name pattern
PRE_DELETE_ALBS=$(aws elbv2 describe-load-balancers --region "$AWS_REGION" \
  --query 'LoadBalancers[?Scheme==`internal`].LoadBalancerArn' --output text 2>/dev/null || echo "")

# Also check by name pattern
NAMED_ALBS=$(aws elbv2 describe-load-balancers --region "$AWS_REGION" \
  --query 'LoadBalancers[?contains(LoadBalancerName,`bedrockgw`) || contains(LoadBalancerName,`k8s-bedrockgw`)].LoadBalancerArn' \
  --output text 2>/dev/null || echo "")

# Combine ALB ARNs to watch
WATCH_ALBS=""
for arn in $PRE_DELETE_ALBS $NAMED_ALBS; do
  [ -n "$arn" ] && [ "$arn" != "None" ] && WATCH_ALBS="$WATCH_ALBS $arn"
done
WATCH_ALBS=$(echo "$WATCH_ALBS" | tr ' ' '\n' | sort -u | tr '\n' ' ')

echo "ALBs to watch for removal: ${WATCH_ALBS:-none}"

# Delete all Ingress resources
echo "Deleting Ingress resources in namespace '$NAMESPACE'..."
kubectl delete ingress -n "$NAMESPACE" --all --wait=true --timeout="${TIMEOUT}s" 2>/dev/null || {
  echo "WARNING: kubectl delete ingress returned non-zero. Continuing with ALB wait..."
}

# Also delete any TargetGroupBindings (AWS Load Balancer Controller CRDs)
kubectl delete targetgroupbinding -n "$NAMESPACE" --all --wait=true --timeout="60s" 2>/dev/null || true

echo "Ingress resources deleted. Waiting for ALBs to be removed by the controller..."

if [ -z "$WATCH_ALBS" ]; then
  echo "No ALBs were tracked. Done."
  exit 0
fi

# Wait for ALBs to disappear
ELAPSED=0
INTERVAL=10
while [ "$ELAPSED" -lt "$TIMEOUT" ]; do
  REMAINING_ALBS=0
  for ALB_ARN in $WATCH_ALBS; do
    if aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" --region "$AWS_REGION" >/dev/null 2>&1; then
      REMAINING_ALBS=$((REMAINING_ALBS + 1))
    fi
  done

  if [ "$REMAINING_ALBS" -eq 0 ]; then
    echo "All ALBs removed successfully."
    break
  fi

  echo "  $REMAINING_ALBS ALB(s) still present. Waiting ${INTERVAL}s... (${ELAPSED}/${TIMEOUT}s)"
  sleep "$INTERVAL"
  ELAPSED=$((ELAPSED + INTERVAL))
done

if [ "$ELAPSED" -ge "$TIMEOUT" ]; then
  echo "WARNING: Timeout reached. Some ALBs may still exist. Proceeding anyway."
  echo "You may need to manually delete remaining ALBs in the AWS console."
fi

# Clean up any orphaned security groups referencing the ALB SGs
echo "Checking for orphaned ENIs from load balancers..."
for ALB_ARN in $WATCH_ALBS; do
  # Try to extract VPC ID from the ALB before it's fully gone
  VPC_ID=$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" --region "$AWS_REGION" \
    --query 'LoadBalancers[0].VpcId' --output text 2>/dev/null || echo "")
  if [ -n "$VPC_ID" ] && [ "$VPC_ID" != "None" ]; then
    # Look for ENIs with descriptions matching the ALB
    ORPHAN_ENIS=$(aws ec2 describe-network-interfaces --region "$AWS_REGION" \
      --filters "Name=vpc-id,Values=$VPC_ID" "Name=description,Values=*ELB*" \
      --query 'NetworkInterfaces[?Status==`available`].NetworkInterfaceId' --output text 2>/dev/null || echo "")
    for ENI in $ORPHAN_ENIS; do
      echo "  Deleting orphaned ENI: $ENI"
      aws ec2 delete-network-interface --network-interface-id "$ENI" --region "$AWS_REGION" 2>/dev/null || true
    done
  fi
done

# Clean up SSM cache entries for ALB wiring
echo "Cleaning up SSM ALB cache entries..."
for param in "/adp/${ENVIRONMENT:-dev}/gateway/internal-alb-arn" \
             "/adp/${ENVIRONMENT:-dev}/gateway/internal-alb-dns" \
             "/adp/${ENVIRONMENT:-dev}/gateway/internal-alb-security-group-ids"; do
  aws ssm delete-parameter --name "$param" --region "$AWS_REGION" 2>/dev/null || true
done

echo "=== Ingress and ALB cleanup complete ==="
