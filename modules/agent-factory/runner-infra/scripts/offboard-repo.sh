#!/bin/bash
set -euo pipefail

# Remove a repository from the GitHub Actions Runner
# Cleans up: Helm release, Kubernetes namespace, IAM role and policy

if [ $# -lt 1 ]; then
    echo "Usage: $0 <repo-name>"
    echo "Example: $0 my-awesome-repo"
    exit 1
fi

REPO_NAME=$1
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
# Lowercase and replace underscores to match onboarding
REPO_NAME_LOWER=$(echo "$REPO_NAME" | tr '[:upper:]' '[:lower:]' | tr '_' '-')
NAMESPACE="arc-runners-${REPO_NAME_LOWER}"
ROLE_NAME="github-runner-${REPO_NAME_LOWER}"
POLICY_NAME="github-runner-${REPO_NAME_LOWER}-policy"

echo "=========================================="
echo "Offboarding repository: $REPO_NAME"
echo "=========================================="
echo "Namespace: $NAMESPACE"
echo "IAM Role: $ROLE_NAME"
echo ""

# Step 1: Uninstall Helm release
echo "Step 1: Uninstalling runner scale set..."
helm uninstall "arc-runner-${REPO_NAME_LOWER}" --namespace "$NAMESPACE" 2>/dev/null || echo "  Helm release not found or already removed"

# Step 2: Delete namespace (this removes all resources in it)
echo "Step 2: Deleting namespace..."
kubectl delete namespace "$NAMESPACE" --ignore-not-found

# Step 3: Delete IAM role policy
echo "Step 3: Deleting IAM role policy..."
aws iam delete-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name "$POLICY_NAME" 2>/dev/null || echo "  Policy not found or already removed"

# Step 4: Delete IAM role
echo "Step 4: Deleting IAM role..."
aws iam delete-role \
    --role-name "$ROLE_NAME" 2>/dev/null || echo "  Role not found or already removed"

echo ""
echo "=========================================="
echo "✅ Repository offboarded successfully!"
echo "=========================================="
echo ""
echo "Removed:"
echo "  - Helm release: arc-runner-${REPO_NAME_LOWER}"
echo "  - Namespace: $NAMESPACE"
echo "  - IAM Role: $ROLE_NAME"
echo "  - IAM Policy: $POLICY_NAME"
echo ""
