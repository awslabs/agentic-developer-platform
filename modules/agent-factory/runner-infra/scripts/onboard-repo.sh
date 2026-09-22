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

# Per-repository runner policy (A18, #5674).
#
# What this policy deliberately does NOT contain, and why:
#
#   iam:CreateRole / CreatePolicy / AttachRolePolicy / PutRolePolicy / PassRole
#     A runner holding these does not need to be granted administrator access —
#     it can create itself a role, attach AdministratorAccess to it, and use it.
#     Workflow jobs execute instructions that originate in text written outside
#     the organisation, so this converted any prompt-injected or compromised job
#     into account takeover. The permissions boundary now DENIES this action set
#     outright (see infrastructure/iam.tf), so re-adding it here would not revive
#     the capability — but it is removed here too, so the intent is unambiguous.
#
#   sts:AssumeRole on "*"
#     Let a runner become any role in the account that trusts it, including the
#     shared runner role that could read the whole adp/ secret prefix. Removed.
#     A repository needing a specific role must have that single role ARN added
#     below, reviewed as a named exception.
#
#   s3:* / ec2:* / lambda:* / dynamodb:* / cloudformation:* / rds:* / ecs:* and
#   the rest of the service wildcards, all on Resource "*"
#     Whole services on every resource in a shared account: one repository's
#     runner could read every other tenant's buckets and tables. Replaced by the
#     concrete resources a repository's own jobs use, all carrying this
#     repository's name.
#
# Scope model: a per-repository runner reaches resources tagged or named for ITS
# repository, and nothing belonging to another. The repo-scoped prefix below is
# the mechanism — a runner for repo A cannot name repo B's resources.
#
# Extending this for a real workload is expected: add the specific ARNs that
# repository needs. Adding a service wildcard or an iam:/sts: action back is not
# an extension, it is a reintroduction of the finding this closed, and the
# boundary will refuse it.
RUNNER_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockModelInvoke",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/anthropic.*",
        "arn:aws:bedrock:*:${AWS_ACCOUNT_ID}:inference-profile/*"
      ]
    },
    {
      "Sid": "OwnRepositorySecrets",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret"
      ],
      "Resource": [
        "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:github-runner/${REPO_NAME_LOWER}/*",
        "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:adp/runner/${REPO_NAME_LOWER}/*"
      ]
    },
    {
      "Sid": "OwnRepositoryParameters",
      "Effect": "Allow",
      "Action": [
        "ssm:GetParameter",
        "ssm:GetParameters",
        "ssm:GetParametersByPath"
      ],
      "Resource": "arn:aws:ssm:${AWS_REGION}:${AWS_ACCOUNT_ID}:parameter/adp/runner/${REPO_NAME_LOWER}/*"
    },
    {
      "Sid": "OwnRepositoryArtifacts",
      "Effect": "Allow",
      "Action": [
        "s3:AbortMultipartUpload",
        "s3:DeleteObject",
        "s3:GetObject",
        "s3:GetObjectVersion",
        "s3:ListBucket",
        "s3:PutObject"
      ],
      "Resource": [
        "arn:aws:s3:::adp-runner-artifacts-${AWS_ACCOUNT_ID}",
        "arn:aws:s3:::adp-runner-artifacts-${AWS_ACCOUNT_ID}/${REPO_NAME_LOWER}/*"
      ]
    },
    {
      "Sid": "OwnRepositoryEcr",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:CompleteLayerUpload",
        "ecr:DescribeImages",
        "ecr:DescribeRepositories",
        "ecr:GetDownloadUrlForLayer",
        "ecr:InitiateLayerUpload",
        "ecr:ListImages",
        "ecr:PutImage",
        "ecr:UploadLayerPart"
      ],
      "Resource": "arn:aws:ecr:${AWS_REGION}:${AWS_ACCOUNT_ID}:repository/${REPO_NAME_LOWER}/*"
    },
    {
      "Sid": "EcrAuth",
      "Effect": "Allow",
      "Action": ["ecr:GetAuthorizationToken"],
      "Resource": "*"
    },
    {
      "Sid": "OwnRepositoryLogs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogStream",
        "logs:DescribeLogStreams",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:${AWS_REGION}:${AWS_ACCOUNT_ID}:log-group:/adp/runner/${REPO_NAME_LOWER}:*"
    },
    {
      "Sid": "CallerIdentity",
      "Effect": "Allow",
      "Action": ["sts:GetCallerIdentity"],
      "Resource": "*"
    }
  ]
}
EOF
)

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
