#!/bin/bash
# Creates MCP Gateway epics and stories as GitHub issues
# Usage: ./create-github-issues.sh [owner/repo]
# Requires: gh CLI authenticated (gh auth login)

REPO="${1:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"

if [ -z "$REPO" ]; then
  echo "Usage: ./create-github-issues.sh owner/repo"
  exit 1
fi

echo "Creating MCP Gateway issues in $REPO..."
echo ""

# Create labels first
gh label create "epic" --description "Epic - high level feature group" --color "3E4B9E" --repo "$REPO" 2>/dev/null
gh label create "mcp-gateway" --description "MCP Gateway feature" --color "0E8A16" --repo "$REPO" 2>/dev/null
gh label create "backend" --description "Backend work" --color "1D76DB" --repo "$REPO" 2>/dev/null
gh label create "frontend" --description "Frontend/UI work" --color "D93F0B" --repo "$REPO" 2>/dev/null
gh label create "infrastructure" --description "Infrastructure/Terraform" --color "FBCA04" --repo "$REPO" 2>/dev/null
gh label create "devops" --description "CI/CD and pipelines" --color "B60205" --repo "$REPO" 2>/dev/null
gh label create "unit:mcp-1" --description "Unit MCP-1: Backend" --color "C2E0C6" --repo "$REPO" 2>/dev/null
gh label create "unit:mcp-2" --description "Unit MCP-2: Admin UI" --color "C2E0C6" --repo "$REPO" 2>/dev/null
gh label create "unit:mcp-3" --description "Unit MCP-3: Config Repo" --color "C2E0C6" --repo "$REPO" 2>/dev/null
gh label create "unit:mcp-4" --description "Unit MCP-4: Infrastructure" --color "C2E0C6" --repo "$REPO" 2>/dev/null

echo "Labels created."
echo ""
