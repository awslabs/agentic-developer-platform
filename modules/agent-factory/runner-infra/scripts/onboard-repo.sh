#!/bin/bash
set -euo pipefail

# Onboard a new repository to the GitHub Actions Runner
# Creates a dedicated IAM role per repository for fine-grained permissions

if [ $# -lt 1 ]; then
    echo "Usage: $0 <repo-name>"
    echo "Example: $0 my-awesome-repo"
    exit 1
fi

REPO_NAME=$1
[[ "$REPO_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,45}$ ]] || {
    echo "Invalid repository name for runner identity" >&2
    exit 1
}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
# Lowercase and replace underscores for Kubernetes resources
REPO_NAME_LOWER=$(echo "$REPO_NAME" | tr '[:upper:]' '[:lower:]' | tr '_' '-')
NAMESPACE="arc-runners-${REPO_NAME_LOWER}"
ROLE_NAME="github-runner-${REPO_NAME_LOWER}"
POLICY_NAME="github-runner-${REPO_NAME_LOWER}-policy"

# Get Terraform outputs
cd "$ROOT_DIR/infrastructure"
AWS_REGION=$(terraform output -raw kubeconfig_command | sed -n 's/.*--region \([^ ]*\).*/\1/p')
OIDC_PROVIDER_ARN=$(terraform output -raw oidc_provider_arn)
OIDC_PROVIDER_URL=$(terraform output -raw oidc_provider_url)
BOUNDARY_ARN=$(terraform output -raw runner_boundary_arn)
AWS_ACCOUNT_ID=$(terraform output -raw aws_account_id)

# Get GitHub org from tfvars
GITHUB_ORG=$(grep 'github_org' terraform.tfvars | cut -d'"' -f2)

# Extract OIDC issuer (remove https://)
OIDC_ISSUER="${OIDC_PROVIDER_URL#https://}"

echo "=========================================="
echo "Onboarding repository: $REPO_NAME"
echo "=========================================="
echo "Namespace: $NAMESPACE"
echo "GitHub Org: $GITHUB_ORG"
echo "IAM Role: $ROLE_NAME"
echo "Permissions Boundary: $BOUNDARY_ARN"
echo ""

# Step 1: Create namespace
echo "Step 1: Creating namespace..."
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

# Step 2: Create IAM role for this repo
echo "Step 2: Creating IAM role..."

# Check if role already exists
if aws iam get-role --role-name "$ROLE_NAME" 2>/dev/null; then
    echo "  IAM role already exists, skipping creation..."
    ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE_NAME}"
else
    # Create trust policy document
    TRUST_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "${OIDC_PROVIDER_ARN}"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "${OIDC_ISSUER}:sub": "system:serviceaccount:${NAMESPACE}:github-runner-sa",
          "${OIDC_ISSUER}:aud": "sts.amazonaws.com"
        }
      }
    }
  ]
}
EOF
)

    # Create the role
    aws iam create-role \
        --role-name "$ROLE_NAME" \
        --assume-role-policy-document "$TRUST_POLICY" \
        --permissions-boundary "$BOUNDARY_ARN" \
        --description "IAM role for GitHub runner in repo ${REPO_NAME}" \
        --tags Key=Project,Value=github-runners Key=Repository,Value="${REPO_NAME}" \
        --output text --query 'Role.Arn'
    
    ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE_NAME}"
    echo "  Created role: $ROLE_ARN"
fi

# Step 3: Create/update IAM policy for this repo
echo "Step 3: Creating IAM policy..."

# Reuse the actual Terraform runtime inventory and the same boundary. Publishing
# builds and infrastructure operations use protected GitHub OIDC identities.
# Only transport secrets registered for this repository survive the filter.
RUNNER_POLICY=$(terraform output -json runner_runtime_policy | jq --arg repo "$REPO_NAME_LOWER" '
  .Statement |= map(
    if .Sid == "LegacyEngineTransport" then
      .Resource |= map(select(contains(":secret:github-runner/" + $repo + "/"))) |
      select(.Resource | length > 0)
    else . end
  )')

# Existing onboarded roles also need the reviewed boundary on an authorized
# rerun. Merely updating an inline policy would leave an old role unbounded.
aws iam put-role-permissions-boundary \
    --role-name "$ROLE_NAME" --permissions-boundary "$BOUNDARY_ARN"

# Put inline policy on the role
aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name "$POLICY_NAME" \
    --policy-document "$RUNNER_POLICY"

echo "  Attached policy: $POLICY_NAME"

# Step 4: Create service account with IRSA
echo "Step 4: Creating service account with IRSA..."
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ServiceAccount
metadata:
  name: github-runner-sa
  namespace: $NAMESPACE
  annotations:
    eks.amazonaws.com/role-arn: $ROLE_ARN
EOF

# Step 5: Create Kubernetes secret from Secrets Manager
echo "Step 5: Creating Kubernetes secret..."
PAT=$(aws secretsmanager get-secret-value \
    --secret-id github-ccsdk-agent/github-pat \
    --region "$AWS_REGION" \
    --query 'SecretString' \
    --output text | jq -r '.token')

if [ -z "$PAT" ] || [ "$PAT" == "null" ]; then
    echo "ERROR: Could not retrieve PAT from Secrets Manager."
    echo "Run: ./setup-secrets.sh <your-github-pat>"
    exit 1
fi

kubectl create secret generic github-arc-secret \
    --namespace "$NAMESPACE" \
    --from-literal=github_token="$PAT" \
    --dry-run=client -o yaml | kubectl apply -f -

# Step 6: Install runner scale set
echo "Step 6: Installing runner scale set..."
helm upgrade --install "arc-runner-${REPO_NAME_LOWER}" \
    --namespace "$NAMESPACE" \
    --set githubConfigUrl="https://github.com/${GITHUB_ORG}/${REPO_NAME}" \
    --set githubConfigSecret=github-arc-secret \
    --set minRunners=0 \
    --set maxRunners=5 \
    --set template.spec.serviceAccountName=github-runner-sa \
    --set 'template.metadata.annotations.karpenter\.sh/do-not-disrupt=true' \
    --wait \
    oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set

echo ""
echo "=========================================="
echo "✅ Repository onboarded successfully!"
echo "=========================================="
echo ""
echo "Runner: arc-runner-${REPO_NAME_LOWER}"
echo "IAM Role: $ROLE_ARN"
echo "Policy: $POLICY_NAME"
echo ""
echo "Update your workflow to use:"
echo "  runs-on: arc-runner-${REPO_NAME_LOWER}"
echo ""
echo "=========================================="
echo "📝 CUSTOMIZING PERMISSIONS"
echo "=========================================="
echo ""
echo "The role starts with a least-privilege policy (A18, #5674): Bedrock invoke,"
echo "and resources named for THIS repository only — no service wildcards, no"
echo "ability to create or assume roles."
echo ""
echo "1. View current policy:"
echo "   aws iam get-role-policy --role-name $ROLE_NAME --policy-name $POLICY_NAME"
echo ""
echo "2. Add the specific resources your jobs need (edit and apply):"
echo "   aws iam put-role-policy \\"
echo "     --role-name $ROLE_NAME \\"
echo "     --policy-name $POLICY_NAME \\"
echo "     --policy-document file://my-custom-policy.json"
echo ""
echo "3. Extend by naming concrete ARNs — a specific bucket, table or queue."
echo "   Do NOT add a service wildcard (s3:*, ec2:*) on Resource \"*\": in a"
echo "   shared account that reaches every other repository's data."
echo ""
echo "4. The permissions boundary will refuse these regardless of what you put"
echo "   in the policy above, so do not spend time on them:"
echo "   - Creating or attaching roles/policies, PutRolePolicy, PassRole"
echo "     (a runner able to do this can grant itself administrator access)"
echo "   - sts:AssumeRole (becoming another identity, e.g. one that can read"
echo "     another tenant's secrets)"
echo "   - Reading secrets outside this repository's own paths"
echo "   - Creating IAM users, or modifying billing/organizations"
echo ""
echo "If a job genuinely needs one of the above, that is a review conversation,"
echo "not a policy edit — the boundary is the control that makes one"
echo "compromised repository stay one compromised repository."
echo ""
