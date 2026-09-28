#!/usr/bin/env bash
# Deploy S3 Files storage infrastructure (Mountpoint for Amazon S3)
# Runs Terraform to create AWS resources + creates K8s PV/PVC
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
echo "Mountpoint S3 Storage Deployment"
echo "============================================"

# ─── Step 1: Run Terraform ────────────────────────────────────────────────

run_terraform() {
  echo ""
  echo "--- Terraform ---"

  if [ ! -d "${TF_DIR}" ]; then
    echo "ERROR: Terraform directory not found at ${TF_DIR}"
    exit 1
  fi

  cd "${TF_DIR}"

  echo "  Running terraform init..."
  terraform init -input=false

  echo "  Running terraform plan..."
  terraform plan -out=tfplan -input=false

  echo "  Running terraform apply..."
  terraform apply -input=false tfplan

  # Capture outputs
  BUCKET_NAME=$(terraform output -raw bucket_name)
  echo ""
  echo "  Bucket: ${BUCKET_NAME}"
  echo "  S3 CSI Role: $(terraform output -raw s3_csi_role_arn)"

  cd "${ROOT_DIR}"
  export BUCKET_NAME
}

# ─── Step 2: Apply K8s Manifests ──────────────────────────────────────────

apply_k8s_manifests() {
  echo ""
  echo "--- K8s Storage Manifests ---"

  # Get bucket name (from Terraform output or env)
  if [ -z "${BUCKET_NAME:-}" ]; then
    if [ -f "${TF_DIR}/terraform.tfstate" ] || [ -d "${TF_DIR}/.terraform" ]; then
      cd "${TF_DIR}"
      BUCKET_NAME=$(terraform output -raw bucket_name 2>/dev/null || echo "")
      cd "${ROOT_DIR}"
    fi
  fi

  if [ -z "${BUCKET_NAME:-}" ]; then
    echo "ERROR: Bucket name not available."
    echo "Run with Terraform first, or set BUCKET_NAME environment variable."
    exit 1
  fi

  echo "  Using bucket: ${BUCKET_NAME}"

  # Check if Mountpoint S3 CSI driver is running
  echo "  Checking Mountpoint S3 CSI driver..."
  if kubectl get daemonset s3-csi-node -n kube-system &>/dev/null; then
    echo "  Mountpoint S3 CSI driver: installed"
  else
    echo "  WARNING: Mountpoint S3 CSI driver not detected in kube-system."
    echo "  The CSI driver should be installed by Terraform (EKS add-on)."
    echo "  Continuing anyway — the PV/PVC will be created but may not bind until the driver is ready."
  fi

  # Template the manifest with the bucket name and apply
  echo "  Applying PV + PVC..."
  sed "s/<BUCKET_NAME>/${BUCKET_NAME}/g" "${MANIFESTS_DIR}/s3-files-storage.yaml" | kubectl apply -f -

  # Wait for PVC to bind
  echo "  Waiting for PVC to bind..."
  if kubectl wait --for=jsonpath='{.status.phase}'=Bound pvc/platform-data -n "${NAMESPACE}" --timeout=120s 2>/dev/null; then
    echo "  PVC platform-data: Bound"
  else
    PVC_STATUS=$(kubectl get pvc platform-data -n "${NAMESPACE}" -o jsonpath='{.status.phase}' 2>/dev/null || echo "NotFound")
    echo "  WARNING: PVC platform-data status: ${PVC_STATUS}"
    echo "  The PVC may take additional time to bind after the Mountpoint S3 CSI driver starts."
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
echo "Mountpoint S3 storage deployment complete!"
echo "============================================"
echo ""
echo "Storage:"
echo "  PVC:         platform-data (ReadWriteMany, backed by S3 via Mountpoint)"
echo "  Bucket:      ${BUCKET_NAME:-<from terraform output>}"
echo "  Mount in pods via:"
echo "    volumes:"
echo "      - name: platform-data"
echo "        persistentVolumeClaim:"
echo "          claimName: platform-data"
echo "    volumeMounts:"
echo "      - name: platform-data"
echo "        mountPath: /platform-data"
echo ""
echo "IMPORTANT: Writers must produce complete artifacts (no in-place edits)."
echo "Git clones and build scratch belong on emptyDir, not this mount."
echo ""
