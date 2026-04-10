# Agent Platform Architecture

Epic 11: Agent Identity, IAM Auth & Self-Service Onboarding Platform (#238)

## 1. Overview

The agent platform enables developers to create, deploy, and govern AI agents that use the Bedrock Gateway for LLM access and AWS services for infrastructure/application work. Each agent has a unique identity, scoped permissions, budget limits, and full observability.

## 2. Core Concepts

### 2.1 Agent

An agent is a software entity that performs automated tasks using LLM capabilities and AWS services.

| Attribute | Source | Description |
|-----------|--------|-------------|
| `agent_id` | Platform (UUID) | Stable unique identifier |
| `agent_name` | Developer | Human-readable name |
| `org_id` | Developer | Organization |
| `team_id` | Developer | Team (optional) |
| `owner` | Developer | Developer who created it |
| `scope` | Developer | `shared` (team) or `personal` (developer) |
| `app_name` | Developer | Application/project this agent works on |
| `description` | Developer | What the agent does |
| `allowed_models` | Developer | Which LLM models it can use |
| `budget_monthly_usd` | Developer | Spending limit |
| `image_uri` | Developer (optional) | Container image |
| `code_repo` | Developer (optional) | GitHub repo + code path |
| `workflow_name` | Developer (optional) | GitHub Actions workflow |
| `role_arn` | Platform | IAM role ARN |
| `k8s_service_account` | Platform | K8s ServiceAccount name |
| `budget_config_id` | Platform | Reference to Postgres budget_configs |
| `status` | Platform | pending → active → disabled |
| `created_at` | Platform | Timestamp |

### 2.2 App (Project/Workspace)

An app defines the blast radius for agents. It's a logical grouping that controls what AWS resources agents can touch.

| Attribute | Description |
|-----------|-------------|
| `app_name` | Unique name, used as resource prefix (e.g., `payment-service`) |
| `org_id` | Owning organization |
| `team_id` | Owning team |
| `permission_boundary_arn` | IAM Permission Boundary for all agents in this app |
| `allowed_services` | Which AWS services agents can use |
| `resource_prefix` | Resource naming prefix (defaults to `{app_name}-`) |

An admin pre-creates apps. Developers onboard agents within an app. The app's permission boundary is the ceiling — no agent can exceed it.

### 2.3 Agent Types

| Type | Use Case | Typical Permissions |
|------|----------|-------------------|
| `infra` | Provision/manage AWS resources | EC2, RDS, S3, Lambda, CloudFormation (scoped to app) |
| `app-dev` | Build/deploy application code | ECR, ECS/EKS, CodeBuild, S3 (scoped to app) |
| `monitoring` | Observe and alert | CloudWatch, X-Ray, Logs (read-only, broader scope) |
| `security` | Scan and audit | IAM, Config, SecurityHub, GuardDuty (read-only) |
| `general` | LLM-only tasks (code review, docs) | Gateway invoke only, no AWS service access |

## 3. Architecture

### 3.1 Data Stores

```
┌─────────────────────────────────────────────────────────┐
│ DynamoDB: Agent Registry                                │
│ (Hot path — Lambda authorizer reads on every request)   │
│                                                         │
│ PK: agent_id (UUID)                                     │
│ GSI: by-role-arn (Lambda authorizer lookup)              │
│ GSI: by-org-team (listing)                              │
│ GSI: by-owner (listing)                                 │
│                                                         │
│ Fields: agent_name, role_arn, org_id, team_id, owner,   │
│         scope, app_name, budget_config_id, status, ...  │
└─────────────────────────────────────────────────────────┘
         │
         │ budget_config_id references
         ▼
┌─────────────────────────────────────────────────────────┐
│ Postgres: Budget & Usage                                │
│ (Warm path — enforcement middleware, reporting)         │
│                                                         │
│ budget_configs: limits per agent/team/org                │
│ budget_usage: actual spend per period                    │
│ usage_logs: per-request cost records                     │
└─────────────────────────────────────────────────────────┘
         │
         │ detailed logs
         ▼
┌─────────────────────────────────────────────────────────┐
│ S3: Chat Logs                                           │
│ (Cold path — full request/response bodies)              │
│                                                         │
│ Partitioned by: org/team/user/date                      │
│ Processed by: Budget Usage Tracker Lambda               │
└─────────────────────────────────────────────────────────┘
```

### 3.2 Authentication Flow

```
Agent Pod (SigV4)
    │
    ▼
API Gateway REST API (regional, streaming enabled)
    │
    ▼
Lambda Authorizer
    ├── Has Bearer token? → Validate JWT (Cognito JWKS) → human user
    └── No token? → Read IAM identity from requestContext
         └── Query DynamoDB by-role-arn GSI
              ├── Found + active → Allow, set X-Agent-* headers
              └── Not found / disabled → Deny (403)
    │
    ▼
Internal ALB (VPC Link V2, 900s idle timeout)
    │
    ▼
FastAPI Backend
    ├── X-Auth-Source: iam → Read identity from X-Agent-* headers
    └── X-Auth-Source: jwt → Read identity from JWT claims
    │
    ▼
Budget Enforcement → Model Resolution → Bedrock → Response (streaming)
```

### 3.3 Budget Enforcement Hierarchy

```
Org Budget ($1000/mo)
  └── Team Budget ($200/mo)
       └── Agent Budget ($50/mo)
```

Most specific wins. Agent budget checked first, then team, then org. If any level is exceeded, request is blocked (429).

## 4. Policy Scoping Service

### 4.1 Purpose

Centralizes all IAM policy generation logic. Takes agent attributes, returns a complete IAM policy document, permission boundary, and tag requirements. The onboarding orchestrator calls this service — it never constructs policies itself.

### 4.2 Three-Layer Security Model

| Layer | Purpose | Mechanism |
|-------|---------|-----------|
| Permission Boundary | Hard ceiling — absolute max permissions | IAM Permission Boundary per hierarchy level |
| Agent IAM Policy | Actual permissions — scoped by agent type | Name-prefix resource ARN patterns |
| Tags | Observability — cost tracking, audit | `aws:RequestTag` conditions on create actions |

- **Permission Boundary**: Even if `AdministratorAccess` is attached, the boundary blocks anything outside scope
- **Name Prefix**: Works with all AWS services (no ABAC coverage gaps)
- **Tags**: Required on resource creation for tracking, not used for access control

### 4.3 Hierarchy & Resource Prefix

```
Level       Prefix Example                          Who Creates
─────────   ─────────────────────────────────────    ──────────────
Platform    bgw-                                     Platform admin
Org         bgw-eng-                                 Platform admin
Team        bgw-eng-plat-                            Org admin
App         bgw-eng-plat-pay-                        Org admin
User        bgw-eng-plat-pay-prn-                    Developer
```

Short codes used to stay within AWS name limits (IAM role: 64 chars, S3: 63 chars).

An agent at a given level can only touch resources matching its prefix. A team-level agent (`bgw-eng-plat-*`) can access all apps and users under that team.

**Shared accounts**: Multiple developers' agents coexist in the same AWS account (e.g., a shared dev account). The name prefix provides isolation within the account — Pranav's agent (`bgw-eng-plat-pay-prn-*`) can't touch Alice's resources (`bgw-eng-plat-user-ali-*`). The AWS account acts as an additional boundary (dev agents can't reach prod), but is NOT a per-agent boundary.

### 4.4 Creation Permissions

| Creator | Can Create Agents At |
|---------|---------------------|
| Platform admin | Any level |
| Org admin | Org, Team, App, User |
| Developer | Team (shared), User (personal) |

### 4.5 Interface

```python
class AgentPolicyScopingService:
    async def generate_permission_boundary(
        self,
        level: str,           # platform, org, team, app, user
        hierarchy: dict,      # {platform, org, team, app, user} values
        region: str,
        account_id: str,
    ) -> dict:
        """Returns a Permission Boundary policy for this hierarchy level."""

    async def generate_agent_policy(
        self,
        agent_type: str,      # infra, app-dev, monitoring, security, general
        resource_prefix: str,  # e.g., bgw-eng-plat-pay-
        region: str,
        account_id: str,
        api_gateway_arn: str,
    ) -> dict:
        """Returns an IAM policy with actions scoped to the resource prefix."""

    async def generate_trust_policy(
        self,
        oidc_provider_arn: str,
        oidc_issuer: str,
        namespace: str,
        service_account_name: str,
    ) -> dict:
        """Returns an IRSA trust policy for the agent's IAM role."""

    def get_resource_prefix(
        self,
        level: str,
        hierarchy: dict,
    ) -> str:
        """Builds the resource prefix from hierarchy level."""

    def get_required_tags(
        self,
        hierarchy: dict,
    ) -> dict:
        """Returns tags that must be applied to all resources created by the agent."""
```

### 4.6 Policy Structure

Every agent policy has four sections:

1. **App-scoped resources** — Actions on resources matching `{prefix}*`
   ```json
   {
     "Sid": "ScopedResources",
     "Effect": "Allow",
     "Action": ["ec2:*", "rds:*", ...],
     "Resource": "arn:aws:*:{region}:{account}:*{prefix}*"
   }
   ```

2. **Tag enforcement** — Resources must be tagged on creation
   ```json
   {
     "Sid": "RequireTagsOnCreate",
     "Effect": "Allow",
     "Action": ["ec2:CreateTags", "ec2:RunInstances", ...],
     "Condition": {
       "StringEquals": {
         "aws:RequestTag/platform": "bgw",
         "aws:RequestTag/org": "eng",
         "aws:RequestTag/team": "plat",
         "aws:RequestTag/app": "pay"
       }
     }
   }
   ```

3. **Gateway access** — Always included
   ```json
   {
     "Sid": "GatewayAccess",
     "Effect": "Allow",
     "Action": "execute-api:Invoke",
     "Resource": "arn:aws:execute-api:{region}:{account}:{api_id}/*"
   }
   ```

4. **Read-only observability** — Always included
   ```json
   {
     "Sid": "Observability",
     "Effect": "Allow",
     "Action": ["cloudwatch:Get*", "logs:Get*", "logs:Describe*", "xray:Get*"],
     "Resource": "*"
   }
   ```

### 4.7 Permission Boundary Template

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowScopedResources",
      "Effect": "Allow",
      "Action": "*",
      "Resource": "arn:aws:*:{region}:{account}:*{prefix}*"
    },
    {
      "Sid": "AllowGlobalReadOnly",
      "Effect": "Allow",
      "Action": ["execute-api:Invoke", "cloudwatch:Get*", "logs:Get*", "xray:Get*", "sts:GetCallerIdentity"],
      "Resource": "*"
    },
    {
      "Sid": "DenyDangerous",
      "Effect": "Deny",
      "Action": ["iam:CreateUser", "iam:CreateRole", "iam:DeleteRole", "organizations:*", "account:*", "sts:AssumeRole"],
      "Resource": "*"
    }
  ]
}
```

## 5. Onboarding Flow

### 5.1 Admin Creates App (one-time)

```
POST /admin/apps
{
  "app_name": "payment-service",
  "org_id": "engineering",
  "team_id": "payments",
  "allowed_services": ["ec2", "rds", "s3", "lambda"]
}
```

Platform creates the Permission Boundary IAM policy for this app.

### 5.2 Developer Onboards Agent

Via GitHub issue, workflow, or admin API:

```
Agent name: payment-deployer
App: payment-service
Type: infra
Budget: $50/mo
Models: claude-sonnet
```

### 5.3 Platform Orchestrates

1. Generate agent_id (UUID)
2. Call Policy Scoping Service → get IAM policy + trust policy
3. Create IAM role with policy + permission boundary
4. Create K8s ServiceAccount with IRSA annotation
5. Register in DynamoDB agent registry
6. Create budget config in Postgres
7. Return agent_id, role_arn, service_account_name to developer

### 5.4 Developer Configures Pod

```yaml
spec:
  serviceAccountName: agent-payment-deployer
```

Agent is live. Can call the gateway and AWS services within its app scope.

## 6. Observability

| What | Where | Granularity |
|------|-------|-------------|
| Auth events | API Gateway access logs (CloudWatch) | Per request |
| LLM usage | S3 chat logs → Budget Usage Tracker → Postgres | Per request, aggregated per period |
| Budget status | Postgres budget_usage | Per agent, per period |
| Agent health | K8s pod status, CloudWatch Container Insights | Per pod |
| AWS actions | CloudTrail | Per API call, filtered by role ARN |

## 7. Security Guardrails

1. **Permission Boundary** — Hard ceiling on what any agent can do, set per app
2. **Resource scoping** — All policies scoped to `{app_name}-*` resources
3. **Deny list** — Agents can never create IAM users/roles, modify organizations, or access billing
4. **Budget enforcement** — Requests blocked when budget exceeded
5. **Model restrictions** — Agents can only use explicitly allowed models
6. **Status control** — Instant revocation by setting status to "disabled"
7. **Audit trail** — Every LLM call logged to S3, every AWS action in CloudTrail

## 8. Implementation Phases

### Phase 1 ✅ (Done)
- IAM auth on API Gateway (Lambda authorizer + DynamoDB)
- Backend dual-auth middleware
- Deploy workflow wiring

### Phase 2 (In Progress)
- Agent registry admin API with UUID identity (#248 ✅)
- Per-agent budget assignment (#249)

### Phase 3 (Planned)
- App/workspace concept with permission boundaries
- Policy Scoping Service
- Onboarding orchestrator
- Self-service via GitHub workflow or dedicated onboarding agent

### Phase 4 (Future)
- Kubernetes CRD for declarative agent management
- Agent invocation logging
- Per-user budget tracking within shared agents
- Agent marketplace (discover and reuse shared agents)

## 9. Future Consideration: Amazon Verified Permissions

[Amazon Verified Permissions (AVP)](https://docs.aws.amazon.com/verifiedpermissions/latest/userguide/what-is-avp.html) is a Cedar-based policy engine for application-level authorization. It could complement the agent platform in the future.

### What AVP could handle
- **Application authorization**: "Can developer X create a team-level agent?" "Can agent Y access model Z?"
- Fine-grained, policy-as-code access control with Cedar language
- Centralized policy management with audit logging
- Complex authorization rules: conditional access, time-based policies, delegation chains

### What AVP does NOT handle
- **IAM policy generation** — AVP doesn't produce IAM policy JSON documents. The Policy Scoping Service still needs to generate the actual IAM policies with resource prefixes, actions, and permission boundaries.
- **AWS resource access enforcement** — That's IAM's job. AVP operates at the application layer, not the AWS API layer.

### When to adopt
- Current approach (role-based checks in admin middleware) is sufficient for the initial hierarchy: platform admin > org admin > developer
- Consider AVP when authorization rules become complex: cross-team agent sharing, temporary access grants, approval workflows, or when audit requirements demand centralized policy logging
- AVP would sit alongside the Policy Scoping Service, not replace it: AVP decides "is this allowed?", the Policy Scoping Service generates "what IAM policy should this agent have?"
