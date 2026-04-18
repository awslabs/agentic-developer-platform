#!/usr/bin/env bash
# Deploy S3 Files storage infrastructure
# Runs Terraform to create AWS resources + creates K8s StorageClass/PV/PVC
#
# Usage: ./scripts/deploy-s3-files.sh [--terraform-only] [--k8s-only]
#
# This script is idempotent — safe to run multiple times.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TF_DIR="${ROOT_DIR}/terraform"
MANIFESTS_DIR="${ROOT_DIR}/manifests"

# Source configuration
source "${SCRIPT_DIR}/_common.sh"
load_config "${ROOT_DIR}"

TERRAFORM_ONLY=false
K8S_ONLY=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --terraform-only) TERRAFORM_ONLY=true; shift ;;
    --k8s-only)       K8S_ONLY=true; shift ;;
    --help)
      echo "Usage: $0 [--terraform-only] [--k8s-only]"
      echo ""
      echo "  --terraform-only  Only run Terraform (skip K8s manifest creation)"
      echo "  --k8s-only        Only apply K8s manifests (assumes Terraform already ran)"
      exit 0
      ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

echo "============================================"
echo "S3 Files Storage Deployment"
echo "============================================"

# ─── Step 1: Discover cluster networking (for Terraform) ──────────────────

discover_cluster_networking() {
  echo ""
  echo "Discovering cluster networking..."

  # Get VPC ID
  VPC_ID=$(aws eks describe-cluster \
    --name "${CLUSTER_NAME}" \
    --region "${AWS_REGION}" \
    --query 'cluster.resourcesVpcConfig.vpcId' \
    --output text 2>/dev/null || echo "")

  if [ -z "${VPC_ID}" ] || [ "${VPC_ID}" = "None" ]; then
    echo "ERROR: Could not determine VPC ID for cluster ${CLUSTER_NAME}"
    echo "Set vpc_id in terraform.tfvars manually."
    return 1
  fi
  echo "  VPC: ${VPC_ID}"

  # Get subnet IDs (same as EKS node subnets)
  SUBNET_IDS=$(aws eks describe-cluster \
    --name "${CLUSTER_NAME}" \
    --region "${AWS_REGION}" \
    --query 'cluster.resourcesVpcConfig.subnetIds' \
    --output json 2>/dev/null || echo "[]")
  echo "  Subnets: ${SUBNET_IDS}"

  # Get node security group
  NODE_SG=$(aws eks describe-cluster \
    --name "${CLUSTER_NAME}" \
    --region "${AWS_REGION}" \
    --query 'cluster.resourcesVpcConfig.clusterSecurityGroupId' \
    --output text 2>/dev/null || echo "")

  if [ -z "${NODE_SG}" ] || [ "${NODE_SG}" = "None" ]; then
    echo "ERROR: Could not determine node security group for cluster ${CLUSTER_NAME}"
    return 1
  fi
  echo "  Node SG: ${NODE_SG}"

  export VPC_ID SUBNET_IDS NODE_SG
}

# ─── Step 2: Run Terraform ────────────────────────────────────────────────

run_terraform() {
  echo ""
  echo "--- Terraform ---"

  if [ ! -d "${TF_DIR}" ]; then
    echo "ERROR: Terraform directory not found at ${TF_DIR}"
    exit 1
  fi

  cd "${TF_DIR}"

  # Auto-discover networking if terraform.tfvars doesn't exist
  if [ ! -f "terraform.tfvars" ]; then
    echo "No terraform.tfvars found — auto-discovering cluster networking..."
    discover_cluster_networking

    cat > terraform.tfvars <<EOF
cluster_name           = "${CLUSTER_NAME}"
aws_region             = "${AWS_REGION}"
bucket_name            = "agent-context-platform-data"
namespace              = "${NAMESPACE}"
vpc_id                 = "${VPC_ID}"
subnet_ids             = ${SUBNET_IDS}
node_security_group_id = "${NODE_SG}"

tags = {
  Environment = "production"
  Team        = "agent-context"
}
EOF
    echo "  Generated terraform.tfvars"
  fi

  echo "  Running terraform init..."
  terraform init -input=false

  echo "  Running terraform plan..."
  terraform plan -out=tfplan -input=false

  echo "  Running terraform apply..."
  terraform apply -input=false tfplan

  # Capture outputs
  FS_ID=$(terraform output -raw file_system_id)
  echo ""
  echo "  File System ID: ${FS_ID}"
  echo "  Bucket:         $(terraform output -raw bucket_name)"

  cd "${ROOT_DIR}"
  export FS_ID
}

# ─── Step 3: Apply K8s Manifests ──────────────────────────────────────────

apply_k8s_manifests() {
  echo ""
  echo "--- K8s Storage Manifests ---"

  # Get file system ID (from Terraform output or env)
  if [ -z "${FS_ID:-}" ]; then
    if [ -f "${TF_DIR}/terraform.tfstate" ] || [ -d "${TF_DIR}/.terraform" ]; then
      cd "${TF_DIR}"
      FS_ID=$(terraform output -raw file_system_id 2>/dev/null || echo "")
      cd "${ROOT_DIR}"
    fi
  fi

  if [ -z "${FS_ID:-}" ]; then
    echo "ERROR: File system ID not available."
    echo "Run with Terraform first, or set FS_ID environment variable."
    exit 1
  fi

  echo "  Using File System ID: ${FS_ID}"

  # Check if EFS CSI driver is running
  echo "  Checking EFS CSI driver..."
  if kubectl get daemonset efs-csi-node -n kube-system &>/dev/null; then
    echo "  EFS CSI driver: installed"
  else
    echo "  WARNING: EFS CSI driver not detected in kube-system."
    echo "  The CSI driver should be installed by Terraform (EKS add-on)."
    echo "  Continuing anyway — the PV/PVC will be created but may not bind until the driver is ready."
  fi

  # Template the manifest with the file system ID and apply
  echo "  Applying StorageClass + PV + PVC..."
  sed "s/<FILE_SYSTEM_ID>/${FS_ID}/g" "${MANIFESTS_DIR}/s3-files-storage.yaml" | kubectl apply -f -

  # Wait for PVC to bind
  echo "  Waiting for PVC to bind..."
  if kubectl wait --for=jsonpath='{.status.phase}'=Bound pvc/platform-data -n "${NAMESPACE}" --timeout=120s 2>/dev/null; then
    echo "  PVC platform-data: Bound"
  else
    PVC_STATUS=$(kubectl get pvc platform-data -n "${NAMESPACE}" -o jsonpath='{.status.phase}' 2>/dev/null || echo "NotFound")
    echo "  WARNING: PVC platform-data status: ${PVC_STATUS}"
    echo "  The PVC may take additional time to bind after the EFS CSI driver starts."
  fi
}

# ─── Execute ──────────────────────────────────────────────────────────────

if [ "${K8S_ONLY}" = "true" ]; then
  apply_k8s_manifests
elif [ "${TERRAFORM_ONLY}" = "true" ]; then
  run_terraform
else
  run_terraform
  apply_k8s_manifests
fi

echo ""
echo "============================================"
echo "S3 Files storage deployment complete!"
echo "============================================"
echo ""
echo "Storage:"
echo "  PVC:         platform-data (500Gi, ReadWriteMany)"
echo "  StorageClass: s3-files"
echo "  Mount in pods via:"
echo "    volumes:"
echo "      - name: platform-data"
echo "        persistentVolumeClaim:"
echo "          claimName: platform-data"
echo "    volumeMounts:"
echo "      - name: platform-data"
echo "        mountPath: /data"
echo "        subPath: <service-name>"
echo ""
