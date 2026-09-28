# ADP Deployment Manifest

What gets deployed, where, and how to validate each component.

## Platform (Shared Foundation)

Terraform: `platform/infra/main.tf`
State key: `dev/platform/terraform.tfstate`

| Resource | AWS Service | Validation Command | Expected |
|----------|------------|-------------------|----------|
| VPC + subnets | VPC | `aws ec2 describe-vpcs --filters "Name=tag:Project,Values=adp" --query 'Vpcs[].{Id:VpcId,State:State}'` | State: available |
| EKS cluster | EKS | `aws eks describe-cluster --name adp-dev-eks-cluster --query 'cluster.status'` | ACTIVE |
| EKS nodes | EC2 (Auto Mode) | `kubectl get nodes` | ≥1 node Ready |
| ECR repos | ECR | `aws ecr describe-repositories --query 'repositories[?starts_with(repositoryName,\`adp-\`)].repositoryName'` | adp-gateway, adp-agent-runtime |
| IAM roles | IAM | `aws iam list-roles --query 'Roles[?starts_with(RoleName,\`adp-dev\`)].RoleName'` | cluster role, node role |
| OIDC provider | IAM | `aws iam list-open-id-connect-providers` | EKS OIDC listed |
| Bedrock model agreements | Bedrock | `aws bedrock get-foundation-model-availability --model-id anthropic.claude-opus-4-6-v1 --query 'agreementAvailability.status'` | AVAILABLE (set by `platform/scripts/enable-bedrock-models.sh`, run automatically by deploy-all.sh / platform-infra-apply.yml) |

## Gateway Module

### Gateway Infrastructure

Terraform: `modules/gateway/infra/main.tf`
State key: `dev/modules/gateway/terraform.tfstate`

| Resource | AWS Service | Validation Command | Expected |
|----------|------------|-------------------|----------|
| PostgreSQL | RDS | `aws rds describe-db-instances --query 'DBInstances[?starts_with(DBInstanceIdentifier,\`bedrockgw\`)].{Id:DBInstanceIdentifier,Status:DBInstanceStatus}'` | available |
| Redis | ElastiCache | `aws elasticache describe-replication-groups --query 'ReplicationGroups[?starts_with(ReplicationGroupId,\`bedrockgw\`)].{Id:ReplicationGroupId,Status:Status}'` | available |
| Cognito User Pool | Cognito | `aws cognito-idp list-user-pools --max-results 10 --query 'UserPools[?starts_with(Name,\`bedrockgw\`)].{Name:Name,Id:Id}'` | Pool listed |
| Cognito Identity Pool | Cognito | `aws cognito-identity list-identity-pools --max-results 10` | Pool listed |
| CloudFront distribution | CloudFront | `aws cloudfront list-distributions --query 'DistributionList.Items[?starts_with(Comment,\`bedrockgw\`)].{Id:Id,Domain:DomainName,Status:Status}'` | Deployed |
| S3 frontend bucket | S3 | `aws s3 ls \| grep bedrockgw.*frontend` | Bucket listed |
| S3 CloudFront logs | S3 | `aws s3 ls \| grep bedrockgw.*logs` | Bucket listed (if enabled) |
| ALB (internal) | ELB | `kubectl get ingress -n adp-gateway` | ADDRESS populated |
| CloudTrail | CloudTrail | `aws cloudtrail describe-trails --query 'trailList[?starts_with(Name,\`bedrockgw\`)].Name'` | Trail listed |
| CloudWatch dashboard | CloudWatch | `aws cloudwatch list-dashboards --query 'DashboardEntries[?starts_with(DashboardName,\`bedrockgw\`)].DashboardName'` | Dashboard listed |
| ECR repo | ECR | `aws ecr describe-repositories --repository-names adp-gateway` | Exists |
| Budget Lambda | Lambda | `aws lambda list-functions --query 'Functions[?starts_with(FunctionName,\`bedrockgw\`)].FunctionName'` | Functions listed (if enabled) |
| API Gateway | API GW | `aws apigateway get-rest-apis --query 'items[?starts_with(name,\`bedrockgw\`)].{Name:name,Id:id}'` | API listed (if enabled) |
| Lambda Authorizer | Lambda | `aws lambda list-functions --query 'Functions[?contains(FunctionName,\`authorizer\`)].FunctionName'` | Function listed (if enabled) |

### Gateway Backend (EKS)

Deploy: `kubectl apply -f modules/gateway/k8s/ -n adp-gateway`

| Resource | K8s Kind | Validation Command | Expected |
|----------|---------|-------------------|----------|
| Gateway pods | Deployment | `kubectl get pods -n adp-gateway -l app=bedrockgateway` | 2 pods Running |
| Service | Service | `kubectl get svc -n adp-gateway` | ClusterIP on port 8080 |
| Ingress (ALB) | Ingress | `kubectl get ingress -n adp-gateway` | ADDRESS populated |
| ConfigMap | ConfigMap | `kubectl get configmap bedrockgateway-config -n adp-gateway` | Exists |
| Secrets | Secret | `kubectl get secret bedrockgateway-secrets -n adp-gateway` | Exists |
| PDB | PodDisruptionBudget | `kubectl get pdb -n adp-gateway` | Exists |
| Health check | HTTP | `kubectl exec -n adp-gateway deploy/bedrockgateway -- curl -s http://localhost:8080/health` | 200 OK |

### Gateway Frontend (S3 + CloudFront)

Deploy: `npm run build` → `aws s3 sync` → CloudFront invalidation

| Resource | Where | Validation Command | Expected |
|----------|-------|-------------------|----------|
| Static files | S3 | `aws s3 ls s3://<frontend-bucket>/ --summarize \| tail -1` | Total Objects > 0 |
| CDN | CloudFront | `curl -s -o /dev/null -w "%{http_code}" https://<cloudfront-domain>/` | 200 |
| API proxy | CloudFront → ALB | `curl -s https://<cloudfront-domain>/api/health` | `{"status":"healthy"}` JSON **body** — never assert HTTP 200 alone: when the VPC origin is missing, `/api/*` falls through to the S3 SPA fallback which also returns 200 (HTML). Both 608-deploy incidents (#3085) were masked by status-only probes. |

## Agent Factory Module

### Agent Factory Infrastructure

Terraform: `modules/agent-factory/infra/main.tf`
State key: `dev/modules/agent-factory/terraform.tfstate`

| Resource | AWS Service | Validation Command | Expected |
|----------|------------|-------------------|----------|
| Runner IAM role (IRSA) | IAM | `aws iam get-role --role-name adp-dev-agent-runner-role --query 'Role.Arn'` | Role exists |
| Permissions boundary | IAM | `aws iam get-policy --policy-arn arn:aws:iam::<ACCOUNT>:policy/adp-dev-agent-runner-boundary` | Policy exists |
| Secrets (GitHub App) | Secrets Manager | `aws secretsmanager list-secrets --filter Key=name,Values=adp/gh-app --query 'SecretList[].Name'` | 6 secrets (3 IDs + 3 keys) |
| Beads DynamoDB | DynamoDB | `aws dynamodb describe-table --table-name adp-dev-agent-beads-manifest --query 'Table.TableStatus'` | ACTIVE |
| Beads S3 bucket | S3 | `aws s3 ls \| grep adp-dev-agent-beads-state` | Bucket listed |
| EKS access entry | EKS | `aws eks list-access-entries --cluster-name adp-dev-eks-cluster \| grep runner` | Entry listed |

### Agent Factory — ARC Runners (EKS)

Deploy: Helm via Terraform `arc-runner` module

| Resource | K8s Kind | Validation Command | Expected |
|----------|---------|-------------------|----------|
| ARC controller | Deployment | `kubectl get pods -n arc-systems` | 1 pod Running |
| Runner namespace | Namespace | `kubectl get ns arc-runners` | Active |
| Runner service account | ServiceAccount | `kubectl describe sa github-runner-sa -n arc-runners` | IRSA annotation present |
| Runner scale set | AutoscalingRunnerSet | `kubectl get pods -n arc-runners` | 0 pods (scales on demand) |
| GitHub registration | GitHub API | `gh api orgs/<ORG>/actions/runners --jq '.total_count'` | ≥0 (runners register on job) |

### Agent Factory — Agent Gateway

Terraform: `modules/agent-factory/infra/gateway-main.tf`
Deploy: `modules/agent-factory/scripts/deploy-gateway.sh`

| Resource | AWS Service | Validation Command | Expected |
|----------|------------|-------------------|----------|
| WebSocket API | API Gateway v2 | `aws apigatewayv2 get-apis --query 'Items[?starts_with(Name,\`adp\`)].{Name:Name,Endpoint:ApiEndpoint}'` | API listed with wss:// endpoint |
| Input SQS queue | SQS | `aws sqs list-queues --queue-name-prefix adp --query 'QueueUrls'` | Queue URL listed |
| Response SQS queue | SQS | (same as above) | Second queue URL |
| DynamoDB sessions | DynamoDB | `aws dynamodb list-tables --query 'TableNames[?starts_with(@,\`adp\`) && contains(@,\`session\`)]'` | Table listed |
| Ingest Lambda | Lambda | `aws lambda list-functions --query 'Functions[?contains(FunctionName,\`ingest\`)].FunctionName'` | Function listed |
| Response Lambda | Lambda | `aws lambda list-functions --query 'Functions[?contains(FunctionName,\`response\`)].FunctionName'` | Function listed |
| KEDA | Helm | `kubectl get pods -n keda` | KEDA operator Running |
| SQS consumer | ScaledJob | `kubectl get scaledjobs -n adp-gateway-agents` | ScaledJob listed |
| Consumer image | ECR | `aws ecr describe-images --repository-name adp-agent-gateway --query 'imageDetails[0].imageTags'` | Image tagged |

## Deployment State File

Location: `s3://<state-bucket>/deploy/<environment>/state.json`

The deploy-all.sh script and the agent both read/write this file to track progress:

```json
{
  "version": 1,
  "environment": "dev",
  "account_id": "123456789012",
  "aws_region": "us-east-1",
  "github_org": "acme-corp",
  "started_at": "2026-04-16T14:30:00Z",
  "modules": ["gateway", "agent-factory"],
  "phases": {
    "bootstrap":         {"status": "complete", "completed_at": "..."},
    "platform_infra":    {"status": "complete", "codebuild_id": "...", "completed_at": "..."},
    "gateway_infra":     {"status": "complete", "codebuild_id": "...", "completed_at": "..."},
    "gateway_backend":   {"status": "complete", "codebuild_id": "...", "completed_at": "..."},
    "gateway_frontend":  {"status": "complete", "codebuild_id": "...", "completed_at": "..."},
    "agent_factory":     {"status": "running",  "codebuild_id": "adp-dev-agent-factory:abc123"},
    "agent_gateway":     {"status": "pending"},
    "github_apps":       {"status": "pending"},
    "verification":      {"status": "pending"}
  },
  "outputs": {
    "eks_cluster": "adp-dev-eks-cluster",
    "cloudfront_domain": "d1234.cloudfront.net",
    "cognito_user_pool_id": "us-east-1_abc123",
    "ecr_registry": "123456789012.dkr.ecr.us-east-1.amazonaws.com",
    "gateway_ws_endpoint": "wss://abc123.execute-api.us-east-1.amazonaws.com/prod"
  },
  "validation": {
    "eks_cluster": "ACTIVE",
    "gateway_pods": "2/2 Running",
    "gateway_health": "200 OK",
    "frontend": "200 OK",
    "rds": "available",
    "arc_controller": "1/1 Running",
    "github_runners": "registered"
  }
}
```

### Status values
- `pending` — not started yet
- `running` — in progress (check `codebuild_id` for build status)
- `complete` — finished successfully
- `failed` — failed (check `error` field)
- `skipped` — user chose to skip this module

### Agent resume logic
1. Read state file from S3
2. Find first phase that is not `complete` or `skipped`
3. If `running` with a `codebuild_id`, poll that build
4. If `failed`, retry the phase
5. If `pending`, start the phase

## Production Deploys via GitHub Actions vs. Local Cold-Start via deploy-all.sh

### CI-first flow (recommended for production)

Infrastructure changes are deployed via GitHub Actions workflows that run on ARC self-hosted runners with IRSA credentials. Each Terraform module has a plan + apply workflow pair:

| Module | Plan Workflow | Apply Workflow | Path Filter |
|--------|--------------|----------------|-------------|
| Platform | `platform-infra-plan.yml` | `platform-infra-apply.yml` | `platform/infra/**` |
| Gateway | `gateway-infra-plan.yml` | `gateway-infra-apply.yml` | `modules/gateway/infra/**` |
| Agent Factory | `agent-factory-infra-plan.yml` | `agent-factory-infra-apply.yml` | `modules/agent-factory/infra/**` |
| Agent Context | `agent-context-infra-plan.yml` | `agent-context-infra-apply.yml` | `modules/agent-context/terraform/**` |

**How it works:**
1. Open a PR that touches a module's infra path. The plan workflow runs and posts a comment on the PR with the plan output.
2. Review the plan. Merge the PR. **Merging does NOT auto-apply.**
3. When ready to deploy, the operator triggers the apply workflow manually: Actions → `<module> Infra Apply` → Run workflow → main.
4. Apply is gated by `environment: production` (requires reviewer approval in GitHub) AND by the `destructive-apply-approved` label gate: if resources would be destroyed, the source PR must have the label.

**Why manual apply:** separates "reviewed" from "deployed" — prevents Friday-evening surprise applies on merge, lets operators batch multiple merged PRs into one apply, and matches the project's "carefully consider reversibility and blast radius" rule. For routine non-destructive changes this is one extra click; for anything risky, it's the right default.

**Safety guardrails:**
- Per-module concurrency control (`concurrency.group: tf-apply-<module>`) prevents state lock races.
- Destroy-safety gate requires explicit label approval for destructive changes.
- Agent-context has a GraphRAG cost guard: `graphrag_enabled=true` requires the `CONFIRM_GRAPHRAG_COST=yes` repo variable.
- Plan comments are updated in-place (one per module per PR).

**Prerequisites:**
- `production` environment created in repo Settings -> Environments with required reviewers.
- ARC runner controller deployed and `arc-runner-org` runner label available.
- `destructive-apply-approved` label created in the repo.

### Local cold-start via deploy-all.sh

For bootstrapping a new AWS account or running everything locally (no CI):

```bash
# Full deploy (CodeBuild-based)
./platform/scripts/deploy-all.sh

# Local mode (needs Terraform, Docker, Node, kubectl locally)
./platform/scripts/deploy-all.sh --local
```

### CI validation mode

After CI owns the infra path, use `--ci` to verify all modules have been applied:

```bash
./platform/scripts/deploy-all.sh --ci
```

This checks Terraform state files in S3 for each module and validates the EKS cluster is active. If any module is missing, it prints the URL of the GitHub Actions workflow to run. It does NOT re-apply anything.
