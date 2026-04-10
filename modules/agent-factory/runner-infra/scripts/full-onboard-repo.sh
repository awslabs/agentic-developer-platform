#!/bin/bash
set -euo pipefail

# Full onboarding script: EKS runner + agent workflow + label
# Usage: ./full-onboard-repo.sh <repo-name> [agent-label-name]

if [ $# -lt 1 ]; then
    echo "Usage: $0 <repo-name> [agent-label-name]"
    echo ""
    echo "Examples:"
    echo "  $0 my-new-repo                    # Label will be 'my-new-repo-agent'"
    echo "  $0 my-new-repo custom-agent       # Label will be 'custom-agent'"
    echo ""
    echo "This script will:"
    echo "  1. Onboard repo to EKS runner cluster (helm, IRSA, etc.)"
    echo "  2. Clone the repo"
    echo "  3. Copy .github-agent/ and .github/workflows/"
    echo "  4. Update workflow with correct runner and label"
    echo "  5. Create the agent label in GitHub"
    echo "  6. Push changes to repo"
    exit 1
fi

REPO_NAME=$1
REPO_NAME_LOWER=$(echo "$REPO_NAME" | tr '[:upper:]' '[:lower:]' | tr '_' '-')
AGENT_LABEL=${2:-"${REPO_NAME_LOWER}-agent"}
RUNNER_NAME="arc-runner-${REPO_NAME_LOWER}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"  # Go up to cc-sdk-agent root
TEMP_DIR="/tmp/${REPO_NAME}"

# Get GitHub org from tfvars
GITHUB_ORG=$(grep 'github_org' "$SCRIPT_DIR/../infrastructure/terraform.tfvars" | cut -d'"' -f2)

echo "=========================================="
echo "Full Onboarding: $REPO_NAME"
echo "=========================================="
echo "GitHub Org: $GITHUB_ORG"
echo "Runner Name: $RUNNER_NAME"
echo "Agent Label: $AGENT_LABEL"
echo "=========================================="
echo ""

# Step 1: Run EKS onboarding
echo "Step 1: Onboarding to EKS cluster..."
"$SCRIPT_DIR/onboard-repo.sh" "$REPO_NAME"
echo ""

# Step 2: Clone the repo
echo "Step 2: Cloning repository..."
rm -rf "$TEMP_DIR"
gh repo clone "${GITHUB_ORG}/${REPO_NAME}" "$TEMP_DIR"
echo ""

# Step 3: Copy agent files
echo "Step 3: Copying agent files..."
cp -r "$ROOT_DIR/.github-agent" "$TEMP_DIR/"
mkdir -p "$TEMP_DIR/.github/workflows"

# Remove node_modules if present
rm -rf "$TEMP_DIR/.github-agent/agent/node_modules"
echo ""

# Step 4: Create customized workflow
echo "Step 4: Creating workflow file..."
cat > "$TEMP_DIR/.github/workflows/agent-trigger.yml" << EOF
name: AI Agent Trigger

on:
  issues:
    types: [labeled]
  issue_comment:
    types: [created]

concurrency:
  group: agent-issue-\${{ github.event.issue.number }}
  cancel-in-progress: false

jobs:
  run-agent:
    if: |
      (github.event_name == 'issues' && github.event.label.name == '${AGENT_LABEL}') ||
      (github.event_name == 'issue_comment' &&
      contains(github.event.comment.body, '/retry') &&
      contains(github.event.issue.labels.*.name, '${AGENT_LABEL}'))
    runs-on: ${RUNNER_NAME}

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Configure Git
        run: |
          git config --global user.email "agent@${REPO_NAME_LOWER}.local"
          git config --global user.name "${REPO_NAME} Agent"

      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: '24'
          cache: 'npm'
          cache-dependency-path: .github-agent/agent/package-lock.json

      - name: Install dependencies
        working-directory: ./.github-agent/agent
        run: npm ci

      - name: Build agent
        working-directory: ./.github-agent/agent
        run: npm run build

      - name: Run agent
        working-directory: ./.github-agent/agent
        env:
          GITHUB_TOKEN: \${{ secrets.GITHUB_TOKEN }}
          ISSUE_NUMBER: \${{ github.event.issue.number }}
          REPO_OWNER: \${{ github.repository_owner }}
          REPO_NAME: \${{ github.event.repository.name }}
          CLAUDE_CODE_USE_BEDROCK: "1"
          ANTHROPIC_MODEL: "us.anthropic.claude-sonnet-4-20250514-v1:0"
          SECRET_PREFIX: "${AGENT_LABEL}"
        run: npm start
EOF
echo ""

# Step 5: Create .gitignore if not exists
echo "Step 5: Ensuring .gitignore..."
if [ ! -f "$TEMP_DIR/.gitignore" ]; then
    cat > "$TEMP_DIR/.gitignore" << EOF
node_modules/
dist/
.env
*.log
.DS_Store
EOF
fi
echo ""

# Step 5b: Copy AGENT-INSTRUCTIONS.md
echo "Step 5b: Adding agent instructions..."
if [ -f "$ROOT_DIR/AGENT-INSTRUCTIONS.md" ]; then
    # Copy and customize with repo-specific label
    sed "s/<agent-label>/${AGENT_LABEL}/g; s/<owner>\/<repo>/${GITHUB_ORG}\/${REPO_NAME}/g" \
        "$ROOT_DIR/AGENT-INSTRUCTIONS.md" > "$TEMP_DIR/AGENT-INSTRUCTIONS.md"
fi
echo ""

# Step 6: Create label in GitHub
echo "Step 6: Creating GitHub label..."
gh label create "$AGENT_LABEL" \
    --repo "${GITHUB_ORG}/${REPO_NAME}" \
    --description "Trigger AI agent to work on this issue" \
    --color "0E8A16" 2>/dev/null || echo "Label already exists or created"
echo ""

# Step 7: Commit and push
echo "Step 7: Committing and pushing..."
cd "$TEMP_DIR"
git add .
git commit -m "Add AI agent workflow and configuration" || echo "Nothing to commit"
git push origin main || git push origin master
echo ""

# Cleanup
rm -rf "$TEMP_DIR"

ROLE_NAME="github-runner-${REPO_NAME_LOWER}"
POLICY_NAME="github-runner-${REPO_NAME_LOWER}-policy"

echo "=========================================="
echo "✅ Onboarding Complete!"
echo "=========================================="
echo ""
echo "Repository: https://github.com/${GITHUB_ORG}/${REPO_NAME}"
echo "Runner: ${RUNNER_NAME}"
echo "Label: ${AGENT_LABEL}"
echo "IAM Role: ${ROLE_NAME}"
echo ""
echo "To trigger the agent:"
echo "  1. Create an issue in the repo"
echo "  2. Add the '${AGENT_LABEL}' label"
echo ""
echo "=========================================="
echo "📝 CUSTOMIZING IAM PERMISSIONS"
echo "=========================================="
echo ""
echo "Each repo has its own IAM role. To customize permissions:"
echo ""
echo "  # View current policy"
echo "  aws iam get-role-policy --role-name $ROLE_NAME --policy-name $POLICY_NAME"
echo ""
echo "  # Update policy"
echo "  aws iam put-role-policy \\"
echo "    --role-name $ROLE_NAME \\"
echo "    --policy-name $POLICY_NAME \\"
echo "    --policy-document file://custom-policy.json"
echo ""
echo "Common customizations:"
echo "  - Restrict S3 to specific buckets"
echo "  - Remove unused services (RDS, SageMaker, etc.)"
echo "  - Add project-specific resource ARNs"
echo ""
