#!/bin/bash
REPO="PranavSharma1000/bedrock-gateway"

# Epic for deployment pipeline
gh issue create --repo "$REPO" \
  --title "Epic MCP-15: MCP Server Deployment Pipeline (GitOps)" \
  --label "epic,mcp-gateway,devops" \
  --body "## Epic MCP-15: MCP Server Deployment Pipeline

GitHub Actions-based deployment pipeline for MCP servers. Config repo is the source of truth. Workflows deploy/remove K8s resources on EKS self-hosted runners.

### Stories
- US-MCP-15.1: MCP Server Config Repository Structure
- US-MCP-15.2: Catalog Sync Workflow
- US-MCP-15.3: Server Deploy Workflow
- US-MCP-15.4: Server Remove Workflow
- US-MCP-15.5: PR Validation Workflow
- US-MCP-15.6: Custom Image Build Workflow

### Unit: MCP-3 (Config Repo & Pipelines)"

echo "Created Epic MCP-15"

# US-MCP-15.1: Config repo structure
gh issue create --repo "$REPO" \
  --title "US-MCP-15.1: MCP Server Config Repository Structure" \
  --label "mcp-gateway,unit:mcp-3,devops" \
  --body "## US-MCP-15.1: MCP Server Config Repository Structure

**As a** Platform Admin (Priya),
**I want** a dedicated GitHub repository with a defined structure for MCP server catalog definitions and org deployment configs,
**So that** all MCP server configuration is version-controlled, reviewable, and auditable.

**Epic**: MCP-15 | **Unit**: MCP-3

### Acceptance Criteria
- [ ] GitHub repo \`mcp-servers\` created with structure:
  \`\`\`
  catalogue/
    github-mcp/catalogue.json
    postgres-mcp/catalogue.json
    playwright-mcp/catalogue.json
    terraform-mcp/catalogue.json
    aws-mcp/catalogue.json
    context7-mcp/catalogue.json
    filesystem-mcp/catalogue.json
  deployments/
    _template/example.json
  .github/workflows/
    sync-catalogue.yml
    deploy-server.yml
    remove-server.yml
    build-custom-image.yml
    validate-pr.yml
  \`\`\`
- [ ] catalogue.json schema defined and documented (name, displayName, description, category, deploymentType, sharingMode, credentialLevel, image, port, identityProviderName, identityAuthFlow, identityScopes, requiredCredentials, documentationUrl)
- [ ] deployment.json schema defined and documented (catalogueName, instanceName, orgSlug, replicas, envOverrides)
- [ ] 7 initial Phase 1 catalog entries committed
- [ ] README with schema docs and onboarding instructions
- [ ] Branch protection: main requires PR review"

echo "Created US-MCP-15.1"

# US-MCP-15.2: Catalog sync workflow
gh issue create --repo "$REPO" \
  --title "US-MCP-15.2: Catalog Sync GitHub Actions Workflow" \
  --label "mcp-gateway,unit:mcp-3,devops" \
  --body "## US-MCP-15.2: Catalog Sync Workflow

**As a** Platform Admin (Priya),
**I want** catalog changes in the GitHub repo to automatically sync to the MCP Router database,
**So that** the marketplace reflects the latest available servers without manual intervention.

**Epic**: MCP-15 | **Unit**: MCP-3

### Acceptance Criteria
- [ ] \`sync-catalogue.yml\` triggers on push to \`catalogue/**\` on main branch
- [ ] Workflow reads each changed catalogue.json file
- [ ] Calls MCP Router API: \`POST /admin/mcp/catalogue\` (create) or \`PUT /admin/mcp/catalogue/{id}\` (update)
- [ ] Authenticates to MCP Router API using a service account JWT
- [ ] Logs sync results as workflow summary
- [ ] Fails the workflow if any API call returns error
- [ ] Runs on EKS self-hosted runners"

echo "Created US-MCP-15.2"

# US-MCP-15.3: Deploy workflow
gh issue create --repo "$REPO" \
  --title "US-MCP-15.3: Server Deploy GitHub Actions Workflow" \
  --label "mcp-gateway,unit:mcp-3,devops" \
  --body "## US-MCP-15.3: Server Deploy Workflow

**As an** Org Admin (Omar),
**I want** deployment configs committed to the GitHub repo to automatically deploy MCP servers to EKS,
**So that** server deployment is automated, auditable, and rollback-able via git revert.

**Epic**: MCP-15 | **Unit**: MCP-3

### Acceptance Criteria
- [ ] \`deploy-server.yml\` triggers on push to \`deployments/**\` on main branch
- [ ] Workflow reads the deployment JSON + corresponding catalogue JSON
- [ ] For self_hosted servers:
  - [ ] Generates K8s Deployment manifest (image from catalogue, env from deployment, secrets from Secrets Manager via ExternalSecret)
  - [ ] Generates K8s ClusterIP Service manifest
  - [ ] Runs \`kubectl apply\` in mcp-servers namespace
  - [ ] Waits for pod Ready (timeout 120s)
  - [ ] Calls \`tools/list\` on the new server to verify MCP protocol works
- [ ] For remote servers:
  - [ ] Calls remote URL with initialize to verify reachability
- [ ] Calls MCP Router API to update deployment status: DEPLOYING → DEPLOYED (or FAILED)
- [ ] Includes tool_count in the callback
- [ ] On failure: sets status=FAILED with error details
- [ ] Runs on EKS self-hosted runners
- [ ] K8s service named \`{instance-name}-{org-slug}\` for org-specific deployments"

echo "Created US-MCP-15.3"

# US-MCP-15.4: Remove workflow
gh issue create --repo "$REPO" \
  --title "US-MCP-15.4: Server Remove GitHub Actions Workflow" \
  --label "mcp-gateway,unit:mcp-3,devops" \
  --body "## US-MCP-15.4: Server Remove Workflow

**As an** Org Admin (Omar),
**I want** deleting a deployment config from the GitHub repo to automatically remove the MCP server from EKS,
**So that** server removal is clean and tracked in git history.

**Epic**: MCP-15 | **Unit**: MCP-3

### Acceptance Criteria
- [ ] \`remove-server.yml\` triggers on file deletion in \`deployments/**\` on main branch
- [ ] Workflow identifies which deployment was removed from the git diff
- [ ] Runs \`kubectl delete deployment\` and \`kubectl delete service\` in mcp-servers namespace
- [ ] Calls MCP Router API to update deployment status: REMOVED
- [ ] Cleans up any associated ExternalSecret resources
- [ ] Runs on EKS self-hosted runners"

echo "Created US-MCP-15.4"

# US-MCP-15.5: PR validation
gh issue create --repo "$REPO" \
  --title "US-MCP-15.5: PR Validation GitHub Actions Workflow" \
  --label "mcp-gateway,unit:mcp-3,devops" \
  --body "## US-MCP-15.5: PR Validation Workflow

**As a** Platform Admin (Priya),
**I want** PRs to the config repo validated automatically before merge,
**So that** invalid configs never reach production.

**Epic**: MCP-15 | **Unit**: MCP-3

### Acceptance Criteria
- [ ] \`validate-pr.yml\` triggers on PR to main branch
- [ ] Validates catalogue.json against JSON schema (all required fields present, valid values)
- [ ] Validates deployment.json against JSON schema
- [ ] For self_hosted: checks Docker image exists in ECR
- [ ] For remote: pings remote URL to verify reachability
- [ ] Runs \`kubectl apply --dry-run=server\` for generated K8s manifests
- [ ] Posts validation results as PR comment
- [ ] Blocks merge if validation fails (required status check)"

echo "Created US-MCP-15.5"

# US-MCP-15.6: Custom image build
gh issue create --repo "$REPO" \
  --title "US-MCP-15.6: Custom Image Build GitHub Actions Workflow" \
  --label "mcp-gateway,unit:mcp-3,devops" \
  --body "## US-MCP-15.6: Custom Image Build Workflow

**As a** Platform Admin (Priya),
**I want** catalog entries with custom Dockerfiles to be automatically built and pushed to ECR,
**So that** custom MCP servers are built via CI/CD, not manually.

**Epic**: MCP-15 | **Unit**: MCP-3

### Acceptance Criteria
- [ ] \`build-custom-image.yml\` triggers on push to \`catalogue/*/Dockerfile\` on main branch
- [ ] Detects which catalogue folder has a Dockerfile change
- [ ] Builds Docker image using the Dockerfile in that folder
- [ ] Tags with: latest + git SHA
- [ ] Pushes to ECR repository (repo name matches catalogue entry name)
- [ ] Updates catalogue.json image field if needed
- [ ] Runs on EKS self-hosted runners"

echo "Created US-MCP-15.6"

echo ""
echo "All pipeline stories created!"
