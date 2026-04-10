#!/bin/bash
set -euo pipefail

# Add auto-fix capability to a repository's CI workflows
# Usage: ./add-auto-fix-to-repo.sh <repo-name> [workflow-file]

if [ $# -lt 1 ]; then
    echo "Usage: $0 <repo-name> [workflow-file]"
    echo ""
    echo "Examples:"
    echo "  $0 my-repo                    # Add to all workflows in repo"
    echo "  $0 my-repo ci.yml             # Add to specific workflow"
    echo ""
    echo "This adds an auto-fix job that creates an issue when CI fails."
    echo "The existing agent will pick up the issue and fix the code."
    exit 1
fi

REPO_NAME=$1
WORKFLOW_FILE=${2:-""}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Get GitHub org from tfvars
GITHUB_ORG=$(grep 'github_org' "$SCRIPT_DIR/../infrastructure/terraform.tfvars" | cut -d'"' -f2)

TEMP_DIR="/tmp/auto-fix-${REPO_NAME}"
rm -rf "$TEMP_DIR"

echo "=========================================="
echo "Adding Auto-Fix to: $REPO_NAME"
echo "=========================================="

# Clone repo
gh repo clone "${GITHUB_ORG}/${REPO_NAME}" "$TEMP_DIR"
cd "$TEMP_DIR"

# Find workflow files to modify
if [ -n "$WORKFLOW_FILE" ]; then
    WORKFLOWS=".github/workflows/$WORKFLOW_FILE"
else
    WORKFLOWS=$(find .github/workflows -name "*.yml" -o -name "*.yaml" 2>/dev/null || true)
fi

if [ -z "$WORKFLOWS" ]; then
    echo "No workflow files found in .github/workflows/"
    exit 1
fi

REPO_NAME_LOWER=$(echo "$REPO_NAME" | tr '[:upper:]' '[:lower:]' | tr '_' '-')
AGENT_LABEL="${REPO_NAME_LOWER}-agent"

# Auto-fix job to append (runs on shared EKS runner)
AUTO_FIX_JOB=$(cat << 'EOF'

  # Auto-fix: Creates issue when CI fails, agent picks it up
  # Runs on shared EKS runner for unlimited parallelism
  auto-fix-on-failure:
    needs: [JOB_NAMES_PLACEHOLDER]
    if: failure()
    runs-on: arc-runner-auto-fix
    steps:
      - name: Fetch logs and create issue
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          # Fetch logs
          gh api repos/${{ github.repository }}/actions/runs/${{ github.run_id }}/logs \
            -H "Accept: application/vnd.github+json" > /tmp/logs.zip 2>/dev/null || true
          unzip -o /tmp/logs.zip -d /tmp/logs 2>/dev/null || true
          find /tmp/logs -name "*.txt" -exec tail -200 {} \; 2>/dev/null | tail -c 15000 > /tmp/logs.txt || echo "Could not fetch logs" > /tmp/logs.txt
          
          REPO_NAME=$(echo "${{ github.repository }}" | cut -d'/' -f2)
          REPO_NAME_LOWER=$(echo "$REPO_NAME" | tr '[:upper:]' '[:lower:]' | tr '_' '-')
          AGENT_LABEL="${REPO_NAME_LOWER}-agent"
          LOGS=$(cat /tmp/logs.txt)
          
          # Create issue
          cat > /tmp/issue.md << 'ISSUEEOF'
## 🤖 Auto-Fix Request: CI Pipeline Failure

### Instructions for Agent

1. **Analyze the error logs** below to understand what failed
2. **Identify the root cause** - test failure, build error, lint issue, or dependency problem
3. **Locate the failing code** - use file paths and line numbers from logs
4. **Make the minimal fix** - only change what's necessary
5. **Verify your fix** - run the same command that failed
6. **Create a PR** with clear description

### Failed Workflow

ISSUEEOF
          
          echo "- **Workflow**: ${{ github.workflow }}" >> /tmp/issue.md
          echo "- **Run**: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}" >> /tmp/issue.md
          echo "- **Branch**: \`${{ github.ref_name }}\`" >> /tmp/issue.md
          echo "- **Commit**: \`${{ github.sha }}\`" >> /tmp/issue.md
          echo "" >> /tmp/issue.md
          echo "### Error Logs" >> /tmp/issue.md
          echo "" >> /tmp/issue.md
          echo '```' >> /tmp/issue.md
          echo "$LOGS" >> /tmp/issue.md
          echo '```' >> /tmp/issue.md
          
          cat >> /tmp/issue.md << 'ISSUEEOF'

### Success Criteria
- [ ] The specific error is fixed
- [ ] CI passes on the fix branch
- [ ] No new errors introduced
ISSUEEOF
          
          gh issue create \
            --repo "${{ github.repository }}" \
            --title "🔧 Auto-fix: CI failure in ${{ github.workflow }}" \
            --body-file /tmp/issue.md \
            --label "$AGENT_LABEL"
EOF
)

MODIFIED=0

for WF in $WORKFLOWS; do
    if [ ! -f "$WF" ]; then
        continue
    fi
    
    # Skip if already has auto-fix
    if grep -q "auto-fix-on-failure" "$WF" 2>/dev/null; then
        echo "  Skipping $WF (already has auto-fix)"
        continue
    fi
    
    # Skip if it's the agent trigger workflow
    if grep -q "agent-trigger" "$WF" 2>/dev/null; then
        echo "  Skipping $WF (agent trigger workflow)"
        continue
    fi
    
    # Get job names from the workflow
    JOB_NAMES=$(grep -E "^  [a-zA-Z0-9_-]+:" "$WF" | sed 's/://g' | tr -d ' ' | grep -v "^#" | head -5 | tr '\n' ', ' | sed 's/,$//')
    
    if [ -z "$JOB_NAMES" ]; then
        echo "  Skipping $WF (no jobs found)"
        continue
    fi
    
    echo "  Adding auto-fix to $WF (depends on: $JOB_NAMES)"
    
    # Replace job names placeholder and append
    echo "$AUTO_FIX_JOB" | \
        sed "s/JOB_NAMES_PLACEHOLDER/$JOB_NAMES/g" >> "$WF"
    
    MODIFIED=$((MODIFIED + 1))
done

if [ $MODIFIED -eq 0 ]; then
    echo "No workflows modified."
    rm -rf "$TEMP_DIR"
    exit 0
fi

# Commit and push
git add .
git commit -m "Add auto-fix on CI failure

When CI fails, automatically creates an issue with error logs.
The AI agent picks up the issue and creates a fix PR."

git push origin main || git push origin master

rm -rf "$TEMP_DIR"

echo ""
echo "=========================================="
echo "✅ Auto-fix added to $MODIFIED workflow(s)"
echo "=========================================="
echo ""
echo "When CI fails, an issue will be created with label: $AGENT_LABEL"
echo "The agent will automatically pick it up and create a fix PR."
echo ""
