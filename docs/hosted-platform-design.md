# Design: Hosted multi-tenant ADP

**EPIC:** [#308](https://github.com/aws-e/adp/issues/308)
**Status:** Design proposal, pre-implementation
**Last updated:** 2026-04-30

## Summary

Transform ADP from "clone the repo, deploy your own platform" to "install our GitHub App and go." Customers get all of ADP's agent capabilities without deploying AWS infrastructure, running ARC runners, or managing GitHub Apps themselves.

One GitHub App install. One (optional) reusable workflow line. Label an issue. Agent runs on our infrastructure against the customer's repo. Results post back to the issue as comments and PRs.

The agent runtime itself is unchanged — `agent-worker.ts` doesn't know or care whether it's running inside a GitHub Actions runner or a KEDA-spawned pod. What changes is everything around it: the trigger mechanism, the compute location, the runtime environment.

## Motivation

### The problem with the current model

ADP today requires each customer to own substantial infrastructure:

- An AWS account with admin privileges
- Terraform-deployed platform (VPC, EKS, ECR, IAM)
- Gateway, agent-factory, and vault modules
- Three GitHub Apps (dev/pm/ops personas) they create themselves
- ARC runner cluster kept operational
- Credentials and secrets management
- Their own upgrade cycle for new agent versions

Weeks of setup work, deep AWS+K8s+GitHub expertise required, and ongoing operational burden. For the product's target audiences — threat researchers, dev teams, internal tool builders — this infrastructure demand is an adoption blocker. Most people who would benefit from ADP don't have (or shouldn't be spending time on) that much AWS admin access.

### What the hosted model changes

The customer's path:

1. Visit the ADP site / GitHub Marketplace listing
2. Click **Install GitHub App** — approve permissions in the browser
3. (Optionally) add one line to `.github/workflows/adp.yml` in an enrolled repo
4. Label an issue with `developer`, `pm`, `malware-analysis-agent`, etc.
5. Watch the agent post comments and open PRs

**Total infrastructure the customer owns: zero.**
**Total time from "never heard of ADP" to first agent run: under two minutes.**

Everything else — EKS, KEDA, SQS, DynamoDB, CAPE fleet, rule management, vault, identity — is hosted by us as a multi-tenant platform.

### Why this is strategic

Three reasons the hosted model matters beyond convenience:

1. **Adoption curve.** Every piece of friction between "user hears about ADP" and "user has an agent PR in their repo" costs users. Removing AWS prerequisites shifts the addressable audience from "teams with AWS admin access" to "anyone with a GitHub account."
2. **Operational leverage.** One platform we operate is easier to improve than N customer deployments we've shipped and can't update. Agent improvements, bug fixes, model upgrades, new skills ship invisibly to every customer.
3. **Domain-app scaling.** The cyber domain (#224) is the first. Finance, legal, IR, supply chain are next. In hosted, new domains ship invisibly — just appear as new labels/personas available in any repo. Self-hosted requires every customer to update their deploy.

## Scope

### In scope

- Public-facing GitHub App (`ADP Agent Platform`) that customers install
- Webhook ingress infrastructure (API Gateway → Lambda → SQS)
- Containerized agent runtime (replaces ARC runner execution)
- Agent pod orchestration (one ScaledJob, one queue, one image, persona selected per message)
- Customer AWS role assumption for deployment tasks (STS + ExternalId + vault)
- Identity model grounded in GitHub (installation_id = tenant, sender = actor)
- Live progress UX (inline comments with edit-in-place, optional PR Check Runs in Phase 2)
- Multi-channel extensibility (GitHub first; WhatsApp, Slack, Teams, SIEM as future channels sharing the same infrastructure)

### Out of scope

- On-premise deployment of the hosted product (the dedicated-infrastructure tier in Phase 3 is "isolated tenancy on our operation," not "customer runs our stack in their DC")
- Customer-authored agent personas (customers extend via skills, rule layers, and output channels — not arbitrary agent code)
- Deprecating the self-hosted path (remains available for regulated / air-gapped customers)
- SSO via SAML / Okta / Azure AD directly (future EPIC; GitHub sign-in covers the common case via #309)

## Design principles

Four principles govern every decision in this design.

### 1. Agent code is environment-agnostic

The agent runtime — `agent-worker.ts`, `skill-agent.ts`, persona + skill loading — should not know whether it's running inside a GitHub Actions runner or a KEDA-spawned pod. It reads environment variables, does its work, exits. Both execution environments produce those env vars through different paths (workflow YAML vs pod entrypoint script), but the agent sees the same interface.

This lets self-hosted and hosted coexist without code forks.

### 2. One queue, one ScaledJob, one image

Persona selection is an attribute on the SQS message, resolved by the agent pod at runtime by loading `/app/personas/$AGENT_TYPE.md`. We do NOT build per-persona queues or per-persona ScaledJobs. Infrastructure should scale on aggregate queue depth, not be sharded by persona.

If one persona's workload ever has genuinely different resource needs (memory, runtime, network isolation), that's handled inside the persona's execution logic (dispatch to existing worker queues / CAPE fleet), not by creating new pod pools.

### 3. Lambda is ingress only. Pods do work.

API Gateway → Lambda is for webhook validation and enqueueing. Target Lambda execution time: <300ms. Lambdas do not clone repos, do not call LLMs, do not run builds, do not touch customer code.

Every piece of work that takes more than a few seconds, needs disk, or runs heavy tools happens in a pod spawned by KEDA from the SQS queue.

### 4. Customer trust boundaries are architectural, not policy

- **Reasoning vs byte-handling isolation** (preserved from cyber EPIC #224) — reasoning agents never hold sample bytes; byte-handling workers have no Bedrock or internet.
- **Tenant isolation** — per-tenant SQS `MessageGroupId`, S3 prefixes keyed by tenant, vault secrets scoped per installation, IAM tags carrying tenant_id on every CloudTrail event.
- **Short-lived credentials everywhere** — GitHub App installation tokens (1-hour lifetime), STS assume-role credentials (1-12 hour configurable), no long-lived keys stored anywhere.
- **Ephemeral workspaces** — customer repo clones live in pod-local storage, destroyed on pod termination.

## Architecture

### System overview

```
┌─────────────────────────── CUSTOMER SIDE ────────────────────────────┐
│                                                                      │
│  GitHub org (customer's)                                             │
│    - Installs ADP Agent Platform (GitHub App) — one-click            │
│    - Optionally: 1-line workflow file for workflow_dispatch trigger  │
│    - Labels issues → webhook fires to our platform                   │
│                                                                      │
└──────────────────────────────────┬───────────────────────────────────┘
                                   │ HTTPS webhook
                                   │ X-Hub-Signature-256: <HMAC>
                                   ▼
┌─────────────────────────── OUR PLATFORM ─────────────────────────────┐
│                                                                      │
│  API Gateway (HTTP API v2, new, dedicated webhook ingress)           │
│      POST /github                                                    │
│      POST /whatsapp     (future)                                     │
│      POST /slack        (future)                                     │
│                                                                      │
│      ↓ (no authorizer on /webhook routes — Lambda validates)         │
│                                                                      │
│  Webhook Lambdas (one per channel, under modules/agent-factory/      │
│                    webhook-ingress/lambda/<channel>/)                │
│      - Validate HMAC signature                                       │
│      - Resolve tenant from installation_id via DDB lookup            │
│      - Extract intent + persona from payload                         │
│      - Check tenant rate limits / quotas                             │
│      - Publish to SQS with normalized envelope                       │
│      - Return 200 within ~300ms                                      │
│                                                                      │
│      ↓                                                               │
│                                                                      │
│  SQS FIFO: adp-<env>-agent-submit.fifo                               │
│      - Single queue, all personas, all channels                      │
│      - MessageGroupId = tenant_id (per-tenant ordering)              │
│      - Persona in message body, not in queue name                    │
│                                                                      │
│      ↓                                                               │
│                                                                      │
│  KEDA ScaledJob: agent-scaledjob                                     │
│      - Single ScaledJob, min=0, max=N                                │
│      - One image: adp-agent:<tag>                                    │
│      - Spawns pods based on queue depth                              │
│                                                                      │
│      ↓                                                               │
│                                                                      │
│  Agent pod lifecycle:                                                │
│                                                                      │
│   1. Init: read SQS message, parse envelope                          │
│   2. Fetch App creds from vault (keyed by installation_id)           │
│   3. Mint GitHub App installation token (1-hour lifetime)            │
│   4. Clone customer repo to /work/repo                               │
│   5. (if operations persona) Assume customer AWS role via STS        │
│   6. Set env: AGENT_TYPE, GITHUB_TOKEN, WORK_DIR, TENANT_ID          │
│   7. Remove trigger label via gh CLI                                 │
│   8. Post "started" comment on issue                                 │
│   9. Exec: node /app/dist/agent-worker.js                            │
│      - Agent loads persona + skills from /app                        │
│      - Edits files in /work/repo                                     │
│      - Dispatches to worker queues for heavy stages (cyber agent)    │
│      - Calls Bedrock via gateway                                     │
│      - Posts progress via edit-in-place issue comments               │
│  10. Commit + push branch + open PR via gh CLI                       │
│  11. Post completion comment + results                               │
│  12. Pod terminates → workspace wiped, credentials expire            │
│                                                                      │
│      ↓                                                               │
│                                                                      │
│  Response back to customer GitHub                                    │
│      - Issue comments (via App installation token)                   │
│      - PRs (via App installation token)                              │
│      - PR check runs (Phase 2, for native streaming UX)              │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### What stays from the current ADP platform

| Component | Role in hosted model |
|---|---|
| `modules/gateway/` (Bedrock proxy) | Agent pods call Claude via the gateway — rate limits, audit, credential isolation come for free |
| `modules/agent-factory/agent/` (skill-agent runtime) | Unchanged. Same persona + skill loading mechanism. Baked into the agent image. |
| `modules/user-services/vault/` (#132) | Per-tenant credentials — GitHub App creds, external API keys, AWS role ARNs |
| SaaS identity (#181) | `installation_id` ↔ `tenant_id` mapping via DDB; identity propagates end-to-end |
| CAPE fleet (#261) | Cyber-specific sandbox — agent dispatches to it via shared SQS |
| Rule management (#271/#272) | YARA corpus, public/org/tenant layering |
| `platform/infra/` | Shared platform infrastructure continues to host the hosted environment itself |
| Worker queues + ScaledJobs (triage, static) | Unchanged — the cyber agent still dispatches to them from Stage 1 / Stage 3 |

### What's new

| Component | Purpose |
|---|---|
| ADP Agent Platform (public GitHub App) | The one install customers do |
| Webhook ingress API Gateway (HTTP API v2) | Receives GitHub + future channel webhooks |
| Per-channel webhook Lambdas | HMAC validation, tenant resolution, enqueue |
| `modules/agent-factory/webhook-ingress/` | Where the webhook code + infra live |
| Agent container image (`adp-agent:<tag>`) | Pre-built runtime replacing ARC's npm-install-every-run |
| Pod entrypoint script | Token mint + clone + env setup (replaces workflow YAML steps) |
| Customer AWS role (via CloudFormation) | STS target for operations-persona agents |
| Onboarding UI | Where customers install, configure, pay |

## Component designs

### The GitHub App (`ADP Agent Platform`)

A single public GitHub App installed by customers. Permissions requested at registration:

| Permission | Why |
|---|---|
| `contents: write` | Clone repos, push branches, open PRs |
| `issues: write` | Read issues, post comments, manage labels |
| `pull_requests: write` | Open and update PRs |
| `checks: write` | Create check runs for live-progress UX (Phase 2) |
| `metadata: read` | Default — list repos the App is installed on |

**NOT requested** (important to reviewers):
- `workflow: write` — we never modify customer Actions config
- `secrets: read` — we never touch customer secrets
- `admin: *` — we never need admin on repos
- `packages: write` — we never publish packages

Customer can install on specific repos (common) rather than org-wide. They can uninstall at any time to revoke access.

The App's private key is stored in our AWS Secrets Manager. Our backend uses it to mint installation tokens per run via JWT exchange.

### Webhook ingress

#### Location in the repo

```
modules/agent-factory/webhook-ingress/
├── README.md
├── infra/
│   ├── main.tf, api-gateway.tf, waf.tf, lambdas.tf,
│   ├── routes.tf, sqs.tf, iam.tf, secrets.tf
│   ├── variables.tf, outputs.tf, versions.tf
├── lambda/
│   ├── common/                (Python package, imported by channel Lambdas)
│   │   ├── signature.py       (HMAC-SHA256 verifier)
│   │   ├── tenant_resolver.py (installation_id → tenant_id)
│   │   ├── sqs_publisher.py   (normalized envelope + publish)
│   │   ├── envelope.py        (Pydantic models)
│   │   └── rate_limit.py
│   ├── github/
│   │   ├── handler.py
│   │   ├── intent_parser.py   (labels / PR events / @mentions → persona + intent)
│   │   └── tests/
│   ├── whatsapp/              (Phase 2+)
│   ├── slack/                 (Phase 2+)
│   └── twilio/                (Phase 2+)
├── scripts/
│   ├── register-github-app.sh
│   └── rotate-webhook-secret.sh
└── tests/                     (cross-component integration tests)
```

Rationale: webhook ingress is a way agent-factory receives triggers. Placing it outside agent-factory implies a separate product. Placing it inside matches ADP's "one module per major platform capability" convention.

#### API Gateway configuration

Dedicated REST API v1 gateway (separate from the Bedrock Gateway's API Gateway). Reasons:
- Different auth model (HMAC inside Lambda vs Cognito JWT authorizer at gateway)
- Cleaner blast-radius isolation (webhook misconfiguration can't affect Bedrock proxy traffic)
- Tighter, webhook-specific WAF rules
- Clear URL separation: `events.adp.example.com/github` vs `api.adp.example.com/*`

REST API v1 chosen over HTTP API v2 (reversed from initial design). The original choice cited ~71% lower cost and lower latency, but missed critical security primitives that HTTP API v2 does not support:
- **Direct WAFv2 association** — rate-based rules and IP allowlist (GitHub's `meta/hooks` ranges) attach directly to the REST API stage. HTTP API v2 requires CloudFront-in-front for WAF, adding CDN complexity for zero benefit on a webhook receiver.
- **Per-method throttling** — cap `POST /github` independently (e.g. 100 rps) so one abusive caller cannot exhaust the account-level 10k rps quota.
- **Resource policies** — `aws:SourceIp` allowlist at the API Gateway layer without needing WAF for basic IP scoping.

The cost savings from HTTP API v2 are negligible at webhook-ingress volumes (low-frequency traffic; pennies per month at Phase 1 scale). Cold-start latency matters less when GitHub retries on timeout and the handler runs in <300ms regardless.

#### Webhook Lambda responsibilities

Each channel has one dedicated Lambda. Minimum viable responsibilities:

1. Validate HMAC signature against a Secrets Manager-stored webhook secret
2. Parse the event payload
3. Resolve `(channel, channel_identity) → tenant_id` via DDB lookup
4. Check tenant-level rate limits (DDB counter)
5. Extract intent + persona from the payload
6. Publish a normalized envelope to SQS
7. Return `200` within ~300ms

The Lambda does NOT: clone repos, call LLMs, assume AWS roles, run builds, or do any work that takes more than a few seconds. Those happen in the agent pod.

#### Intent parsing (GitHub)

The Lambda maps GitHub events to personas using a registry:

```python
LABEL_TO_PERSONA = {
    "developer":               "developer",
    "pm":                      "pm",
    "agent-operations":        "operations",
    "agent-reviewer":          "reviewer",
    "malware-analysis-agent":  "malware-analysis-agent",
    "superpower":              "pt-superpower",
}

def extract_intent(payload):
    # Label-triggered
    if payload["action"] == "labeled":
        persona = LABEL_TO_PERSONA.get(payload["label"]["name"])
        if persona:
            return {"persona": persona, "trigger": "issue_labeled"}

    # PR opened/updated → reviewer (no label required)
    if payload.get("pull_request") and payload["action"] in ("opened", "synchronize"):
        return {"persona": "reviewer", "trigger": f"pr_{payload['action']}"}

    # @-mention in comment
    if payload["action"] == "created" and "comment" in payload:
        body = payload["comment"]["body"]
        for mention, persona in [("@agent-developer", "developer"),
                                  ("@agent-malware-analysis-agent", "malware-analysis-agent")]:
            if mention in body:
                return {"persona": persona, "trigger": "mentioned"}

    return None  # no-op, return 200
```

Matches the current workflow-based triggering exactly: label = run, open PR = reviewer, @-mention = run. UX unchanged.

#### SQS envelope schema

```json
{
  "version": "1.0",
  "channel": "github",
  "tenant_id": "acme-corp",
  "persona": "developer",
  "actor": {
    "github_id": 12345678,
    "github_login": "jane-developer",
    "is_bot": false
  },
  "source_ref": {
    "installation_id": 99887766,
    "repo": "acme-corp/flagship-app",
    "issue": 42,
    "pr": null,
    "sha": null
  },
  "intent": {
    "trigger": "issue_labeled",
    "label": "developer"
  },
  "payload": { /* raw webhook body for reference */ },
  "arrived_at": "2026-04-30T14:22:00Z"
}
```

MessageGroupId = `tenant_id`. Per-tenant FIFO ordering guaranteed; inter-tenant parallelism unrestricted.

### SQS queue + KEDA ScaledJob

One FIFO queue: `adp-<env>-agent-submit.fifo`.

- DLQ: `adp-<env>-agent-submit-dlq.fifo` with `maxReceiveCount: 3`
- Visibility timeout: 2 hours (generous for longest-running personas)
- Retention: 4 days

One ScaledJob: `agent-scaledjob` in namespace `adp-agents`.
- Image: `adp-agent:<tag>`
- `minReplicaCount: 0`
- `maxReplicaCount: 50` (tunable per tenancy scale)
- `pollingInterval: 5s`
- `queueLength: 1` (spawn a pod per message)
- `successfulJobsHistoryLimit: 5`, `failedJobsHistoryLimit: 5`
- Pod resources: `requests.cpu: 1, requests.memory: 4Gi`; `limits.cpu: 4, limits.memory: 8Gi`; `ephemeral-storage: 50Gi`

Persona resource differentiation happens at runtime (e.g. the operations persona may request higher memory via init hints), not at ScaledJob level.

### Agent pod entrypoint

The container's `ENTRYPOINT` is a small script (~150 lines Python or bash) that bridges the SQS message to the existing agent runtime.

```
/app/entrypoint.py
├── 1. Read SQS message from env var $SQS_MESSAGE_BODY (injected by KEDA)
├── 2. Parse envelope → extract tenant_id, persona, installation_id, repo, issue
├── 3. Fetch GitHub App credentials from vault:
│      - key: tenants/<tenant_id>/github-app
│      - returns: { app_id, private_key }
├── 4. Mint installation token:
│      - JWT signed with App private key
│      - POST /app/installations/<installation_id>/access_tokens
│      - token valid for 1 hour
├── 5. Set env vars:
│      - GITHUB_TOKEN, GH_TOKEN, GH_APP_TOKEN = <installation_token>
│      - AGENT_TYPE = <persona>
│      - ISSUE_NUMBER, REPO_OWNER, REPO_NAME, TARGET_REPO
│      - WORK_DIR = /work/repo
│      - CLAUDE_CODE_USE_BEDROCK = 1
│      - ANTHROPIC_MODEL = <configured-model>
│      - TENANT_ID = <tenant_id>
├── 6. Clone customer repo:
│      git clone --depth=20 https://x-access-token:$GITHUB_TOKEN@github.com/<repo> /work/repo
├── 7. Configure git identity:
│      git config user.email "<app-id>+adp-agent[bot]@users.noreply.github.com"
│      git config user.name "adp-agent[bot]"
├── 8. (if persona needs AWS) Assume customer AWS role via STS:
│      - Fetch role_arn + external_id from vault
│      - sts.assume_role(...) with session tags
│      - Export AWS_ACCESS_KEY_ID/SECRET/SESSION_TOKEN
├── 9. Remove trigger label (via gh CLI)
├── 10. Post "started" comment on issue
├── 11. Exec the agent: node /app/dist/agent-worker.js
└── 12. On success:
        - git commit + push branch agent/issue-<N>
        - gh pr create
        - Post completion comment
      On failure:
        - Post failure comment with error summary
        - exit nonzero → KEDA sees failure, DLQ on retries exhausted
```

Key: the agent binary (`/app/dist/agent-worker.js`) is unchanged from today. It reads the same env vars, does the same work. The entrypoint script is what the workflow YAML used to be — pre-run setup.

### Worker image contents

Single image `adp-agent`, tagged by git SHA + timestamp.

**Base:** `ubuntu:24.04`

**Runtimes:**
- Node.js 22 (primary — agent runtime is TypeScript)
- Python 3.12 (skill scripts, tooling)

**Pre-built agent runtime:**
- `/app/dist/agent-worker.js` — compiled from `modules/agent-factory/agent/src/agent-worker.ts`
- `/app/dist/skill-agent.js` — compiled skill-agent runtime
- `/app/node_modules/` — pre-installed dependencies
- `/app/personas/` — all persona markdown files (developer.md, pm.md, operations.md, reviewer.md, malware-analysis-agent.md, etc.)
- `/app/skills/` — all skill directories with SKILL.md + helpers
- `/app/entrypoint.py` — the pod entrypoint described above

**GitHub + source control:**
- `git` (with credential helper pre-configured for `x-access-token:$GITHUB_TOKEN`)
- `gh` CLI

**AWS tooling:**
- AWS CLI v2
- `kubectl` (for operations persona applying manifests to customer clusters)
- `terraform` 1.7+ (for operations persona infra work)

**Language package managers for customer-repo work:**
- npm, pnpm, yarn
- pip, poetry, uv
- `build-essential`, `pkg-config`, `libssl-dev`, `libffi-dev`

**Text and search:**
- ripgrep, fd, fzf, jq, yq
- curl, wget
- vim (minimal, for debugging via `kubectl exec`)

**Runtime user + filesystem:**
- User: `agent` (UID 1001), non-root
- Home: `/home/agent`
- Workspace: `/work` (ephemeral, destroyed with pod)
- Image itself is read-only where feasible

**Explicitly NOT included** (kept for specialized image variants):
- Chromium / Playwright (separate `adp-agent:browser` image for webapp-testing skills)
- LibreOffice / document converters (specialized)
- QEMU / KVM (CAPE host only, not agent pods)
- Go / Rust / Java runtimes (add when measured demand requires)

**Target size:** 2-3 GB compressed, ~5-8 GB uncompressed. Build time ~5-10 min.

**CI build:** on every push to `main` affecting `modules/agent-factory/agent/**` or `modules/agent-factory/webhook-ingress/**` or `modules/domain-apps/*/agent/**`, the image rebuilds and is tagged both as `<git-sha>` and `latest`. The ScaledJob spec references `latest` but pins via digest for reproducibility.

### Persona + skill staging

Today's self-hosted workflows (see `.github/workflows/malware-analysis-agent.yml` step "Stage malware-analysis-agent persona + skills into target repo") copy persona + skills from the checked-out ADP repo into the customer's working directory at two well-known paths:

- `$WORK_DIR/.adp-rules/personas/<AGENT_TYPE>.md` — persona the agent loads
- `$WORK_DIR/.claude/skills/<name>/SKILL.md` — skills the agent discovers

This is a clever pattern. It keeps personas and skills versioned in ADP's source tree alongside their code (under `modules/agent-factory/agent/personas/` for core personas and `modules/domain-apps/<domain>/agent/{personas,skills}/` for domain-app personas and skills), while making them available to any repo that triggers an agent run. Customers never vendor ADP content into their repos.

In the hosted pod model the same resolution paths apply — `agent-worker.ts` still looks for `$WORK_DIR/.adp-rules/personas/...` and `$WORK_DIR/.claude/skills/...`. What changes is **when** the staging happens:

- **Self-hosted:** staged per workflow run, from a fresh `actions/checkout` of the ADP repo
- **Hosted:** staged per pod run, from files baked into the container image at image-build time

#### At image build time

The Dockerfile `COPY`s personas and skills from their source locations into well-known in-image paths:

```dockerfile
# Core agent-factory personas (developer, pm, reviewer, operations, pt-superpower, ...)
COPY --chown=agent:agent modules/agent-factory/agent/personas/ /app/personas/

# Domain-app personas (one COPY per domain; add as new domains ship)
COPY --chown=agent:agent modules/domain-apps/cyber/agent/personas/ /app/personas/
# COPY --chown=agent:agent modules/domain-apps/finance/agent/personas/ /app/personas/
# COPY --chown=agent:agent modules/domain-apps/legal/agent/personas/ /app/personas/

# Skills — flat under /app/skills/<name>/SKILL.md
COPY --chown=agent:agent modules/domain-apps/cyber/agent/skills/ /app/skills/
# COPY --chown=agent:agent modules/domain-apps/finance/agent/skills/ /app/skills/
# (future domain apps follow the same pattern)
```

Result: `/app/personas/*.md` contains every persona across every domain. `/app/skills/*/SKILL.md` contains every skill. One image, all domains.

#### At pod run time

The entrypoint script (see "Agent pod entrypoint" above) stages from the in-image paths into the customer's cloned repo just before invoking the agent:

```python
import shutil
from pathlib import Path

WORK_REPO = Path("/work/repo")
STAGED_PERSONAS = Path("/app/personas")
STAGED_SKILLS = Path("/app/skills")

# Create target directories inside the customer's repo workspace
(WORK_REPO / ".adp-rules" / "personas").mkdir(parents=True, exist_ok=True)
(WORK_REPO / ".claude" / "skills").mkdir(parents=True, exist_ok=True)

# Copy all personas and all skills — matches current workflow behavior exactly
shutil.copytree(STAGED_PERSONAS, WORK_REPO / ".adp-rules" / "personas", dirs_exist_ok=True)
shutil.copytree(STAGED_SKILLS, WORK_REPO / ".claude" / "skills", dirs_exist_ok=True)
```

After this step, `agent-worker.ts` runs and finds personas at `$WORK_DIR/.adp-rules/personas/<AGENT_TYPE>.md` and skills at `$WORK_DIR/.claude/skills/<name>/SKILL.md` — identical resolution paths as today. No agent code change.

#### How a new domain app ships

The pattern makes adding a new domain app a small, predictable change:

1. Add the new domain under `modules/domain-apps/<domain>/agent/personas/<persona>.md` and `.../skills/<skill>/SKILL.md`
2. Add one `COPY` line to the Dockerfile for the new domain's personas + skills
3. Add the persona name to the webhook Lambda's `LABEL_TO_PERSONA` registry so it can be triggered
4. Rebuild the image; deploy

No new workflow YAML, no new SQS queues, no new ScaledJobs, no new Lambdas. The design of "one queue, one ScaledJob, one image" combined with the Dockerfile-based persona/skill staging means **shipping a new domain app is primarily a Dockerfile edit + image rebuild**.

#### Alternative: symlinks instead of copies

A performance-optimal variant uses symlinks instead of copies:

```python
# Instead of copytree:
(WORK_REPO / ".adp-rules" / "personas").symlink_to(STAGED_PERSONAS)
(WORK_REPO / ".claude" / "skills").symlink_to(STAGED_SKILLS)
```

Zero disk use, zero copy time. The trade-off: if the agent runs `git add -A` in `$WORK_DIR` (not the current pattern, but conceivable), it would attempt to commit symlinks pointing at `/app/...`. Copies are safer default. Start with copies; switch to symlinks only if image size or pod start time forces it.

### Customer AWS role access

Pattern: IAM role in the customer's account, trusted by our platform, gated by ExternalId, assumed via STS per agent run.

#### CloudFormation template (three tiers)

We publish three pre-built CloudFormation templates at stable URLs:

- `https://adp.example.com/cfn/read-only.yaml`
- `https://adp.example.com/cfn/scoped-write.yaml`
- `https://adp.example.com/cfn/full-admin.yaml`

Each template defines an IAM role with:

```yaml
AssumeRolePolicyDocument:
  Version: '2012-10-17'
  Statement:
    - Effect: Allow
      Principal:
        AWS: arn:aws:iam::<adp-platform-account>:role/adp-hosted-agent
      Action: sts:AssumeRole
      Condition:
        StringEquals:
          sts:ExternalId: !Ref ExternalId
```

The `ExternalId` is a per-tenant random 64-character value we generate and store in vault. Customer pastes it into the CFN stack as a parameter.

Policies differ per tier:
- **read-only:** `ReadOnlyAccess` managed policy
- **scoped-write:** customer lists specific resources (S3 bucket ARNs, EKS cluster ARNs) as CFN parameters; template generates a minimum-scope policy
- **full-admin:** `AdministratorAccess` (with UI warnings)

#### Vault entry

```json
{
  "path": "/tenants/<tenant_id>/aws-access",
  "value": {
    "role_arn": "arn:aws:iam::<customer-account>:role/adp-hosted-agent",
    "external_id": "<per-tenant-random-64char>",
    "default_region": "us-east-1",
    "session_duration_seconds": 3600,
    "permission_tier": "scoped-write",
    "allowed_regions": ["us-east-1", "us-west-2"],
    "created_at": "2026-04-30T14:22:00Z",
    "last_used_at": "2026-04-30T15:03:12Z"
  }
}
```

No IAM access keys. Ever. Only role metadata.

#### Assume-role call with session tags

```python
sts.assume_role(
    RoleArn=vault_entry["role_arn"],
    ExternalId=vault_entry["external_id"],
    RoleSessionName=f"adp-agent-{run_id}",
    DurationSeconds=vault_entry["session_duration_seconds"],
    Tags=[
        {"Key": "adp:tenant_id",           "Value": tenant_id},
        {"Key": "adp:actor_github_login",  "Value": actor.github_login},
        {"Key": "adp:actor_github_id",     "Value": str(actor.github_id)},
        {"Key": "adp:run_id",              "Value": run_id},
        {"Key": "adp:github_issue",        "Value": f"{repo}#{issue}"},
        {"Key": "adp:persona",             "Value": persona},
    ]
)
```

These tags appear on every CloudTrail event in the customer's account the agent triggers — full audit traceability back to the specific run, user, and issue.

### Identity model

Every event carries three distinct identities, extracted by the webhook Lambda and propagated end-to-end:

| Identity | Source | Scope |
|---|---|---|
| `tenant_id` | DDB lookup keyed on `installation_id` | Our internal tenant identifier; billing, quotas, rule layers, S3 prefixes |
| `actor.github_login` + `actor.github_id` | `sender.login` + `sender.id` in webhook payload | Who caused this specific event; audit trail |
| `source_ref` | Repo + issue + PR + SHA in payload | Where the work applies |

Notes on trust:
- `actor.github_id` is numerically stable (username changes don't affect it) — use as canonical identity
- `actor.github_login` is for human-readable display and logs
- `sender.email` is typically absent unless the user made it public — don't depend on it
- `installation_id` → `tenant_id` mapping is established at install time (via the GitHub App installation webhook) and stored in DDB `adp-<env>-tenant-registry`

### Live progress UX

Phase 1: inline issue comments with edit-in-place.

Agent posts a "live status" comment at run start:

```markdown
## 🔬 Agent running — last update 2s ago
### Progress
- [~] Stage 1 Triage (running, 4s elapsed)
- [ ] Stage 2 Research
- [ ] Stage 3 Static
- [ ] Stage 4 Sandbox
- [ ] Stage 5 Correlation
- [ ] Stage 6 Verdict
- [ ] Stage 7 Report

Latest: Analyzing sample at s3://.../bin-ls
```

Every ~10 seconds, the agent PATCHes the comment with an updated version. Customer refreshes the issue → sees updated progress. Not true streaming but close enough that most users won't notice.

Phase 2 adds Check Runs API for PR-triggered agents — native GitHub UX for live log streaming, matching CircleCI/Buildkite integrations.

### Multi-channel extensibility

Though GitHub is the only implemented channel in Phase 1, the webhook ingress is designed so adding WhatsApp, Slack, Teams, Twilio SMS, or SIEM webhooks is ~1-2 days each:

1. Add a new Lambda at `lambda/<channel>/handler.py`
2. Implement channel-specific HMAC validation in `common/signature_<channel>.py`
3. Add a new API Gateway route `POST /<channel>`
4. Add a tenant-resolver entry mapping channel-specific identifiers (phone_number_id, team_id) to tenant_id
5. Add an output adapter in `output_adapters/<channel>.py` for posting results back

The agent pod itself is channel-agnostic — it reads the normalized envelope, does its work, and calls the appropriate output adapter based on `envelope.channel`.

Channels split into two families:
- **Document/event channels** (GitHub, SIEM, S3 drops): state lives in the source system; no session needed
- **Conversational channels** (WhatsApp, Slack, Teams): multi-turn; reuse the chat lambda's session + history patterns

Phase 1 implements only document/event shape (GitHub). Phase 2+ extends to conversational.

## Data model

### DynamoDB tables

**`adp-<env>-tenant-registry`**
- PK: `installation_id` (string)
- Attributes: `tenant_id`, `github_org`, `github_account_type`, `installed_at`, `plan_tier`, `enabled_personas` (list)
- Written to on GitHub App install webhook
- Read from webhook Lambda for every event

**`adp-<env>-agent-runs`**
- PK: `run_id` (UUID)
- SK: `tenant_id`
- Attributes: `persona`, `actor_github_login`, `actor_github_id`, `repo`, `issue`, `started_at`, `ended_at`, `outcome`, `cost_usd`, `case_file_s3`, `pr_url`, `trigger`
- Written by pod entrypoint at start, updated at end
- Source of truth for billing, audit, support

**`adp-<env>-rate-limits`**
- PK: `tenant_id`
- Attributes: `minute_count`, `hour_count`, `day_count`, TTL-based reset
- Written by webhook Lambda at each event to enforce per-tenant quotas

### Vault paths

- `/tenants/<tenant_id>/aws-access` — customer AWS role ARN + ExternalId
- `/tenants/<tenant_id>/external-credentials/<service>` — VT, Shodan, MalwareBazaar premium keys
- `/platform/github-apps/adp-agent-platform/private-key` — the single public GitHub App's private key
- `/platform/webhook-secrets/<channel>` — per-channel HMAC signing secret

### S3 prefixes

- `s3://adp-<env>-artifacts/reports/<tenant_id>/<run_id>/` — agent run artifacts
- `s3://adp-<env>-artifacts/logs/<tenant_id>/<run_id>/` — agent run logs (if not in CloudWatch)

All tenant-scoped prefixes have bucket policies ensuring a pod running for tenant A cannot read tenant B's prefix.

## Security

### Threat model

Primary threats:
1. **Malicious customer code** executing in our infra (agent pod compromise)
2. **Cross-tenant data leak** (tenant A sees tenant B's code, secrets, reports)
3. **Webhook spoofing** (attacker POSTs fake events to our endpoint)
4. **Token theft** (GitHub App installation token exfiltrated from a pod)
5. **Customer AWS role abuse** (agent assumes role beyond its authorized scope)

### Mitigations

| Threat | Mitigation |
|---|---|
| Malicious customer code | Pod runs as non-root UID 1001; `securityContext: readOnlyRootFilesystem` where feasible; `NetworkPolicy` blocks egress except GitHub API, npm/pypi, AWS VPC endpoints; ephemeral workspace wiped on pod end |
| Cross-tenant leak (code) | Each run = new pod = new workspace; no shared volume; pod terminates immediately after work |
| Cross-tenant leak (data) | S3 prefixes keyed by `tenant_id`; bucket policies deny cross-tenant reads; SQS messages scoped per tenant; vault entries per tenant |
| Webhook spoofing | HMAC-SHA256 signature validated inside Lambda (constant-time compare); WAF rate limits on `/webhook/*`; optional IP allowlist of GitHub's published webhook CIDRs |
| Token theft | Installation tokens live in pod memory only, max 1-hour lifetime; not written to disk or logs; pod IAM prohibits reading other pods' secrets |
| AWS role abuse | ExternalId required; customer controls role permissions via CFN; session tags on every assume-role enable CloudTrail audit; session duration capped at 12h |

### Audit surface

Every agent run produces a durable record in the `agent-runs` DDB table, with:
- Who (actor_github_login, actor_github_id)
- When (started_at, ended_at)
- What (persona, trigger, repo, issue)
- Outcome (success/failure/error)
- Cost (bedrock tokens, compute time)
- Artifacts (case file S3 URI, PR URL)
- Audit events (list of STS assume-role calls, Bedrock calls, CAPE submissions)

Customer-visible:
- CloudTrail in customer's AWS account shows every action tagged with `adp:*` tags
- GitHub issue timeline shows comments + PRs

Platform-internal:
- CloudWatch log group per tenant prefix
- Bedrock audit log via gateway

## Phased rollout

### Phase 1 — MVP, private alpha (~5-6 weeks)

Goal: one friendly customer installs the GitHub App on one repo and runs any ADP persona without touching AWS.

Scope:
1. Public GitHub App registered with the 5 permissions above
2. Webhook ingress infrastructure:
   - HTTP API v2 gateway
   - `github` webhook Lambda (HMAC validation, tenant resolution, SQS publish)
   - Tenant-registry DDB with install-webhook handler
   - SQS FIFO queue + DLQ
3. Agent container image:
   - Single image `adp-agent`, all personas + skills baked in
   - Pod entrypoint script (token mint + clone + env + run + push)
   - ECR repo + CI workflow
4. KEDA ScaledJob for agent pods
5. Vault integration for GitHub App creds + (for operations persona) AWS role ARNs
6. Live-progress comment pattern (edit-in-place)
7. Customer AWS access via CFN templates + assume-role helper (3 tiers)
8. Onboarding docs: install App, connect AWS account, first agent run
9. Basic rate-limit / quota layer (DDB counter per tenant)

Out of scope for Phase 1:
- Billing integration
- Public onboarding UI (docs only for friendly-customer pilot)
- Check Runs API (Phase 2 UX)
- Per-channel beyond GitHub
- Account linking with existing Cognito users

**Phase 1 acceptance:**
- One internal repo, App installed
- AWS account connected via CFN template (scoped-write tier)
- Issue labeled with `developer` → agent runs end-to-end, opens PR
- Issue labeled with `agent-operations` → agent runs, assumes customer AWS role, applies a trivial Terraform change, opens PR
- CloudTrail in customer account shows the operations agent's actions tagged with `adp:run_id`, `adp:actor_github_login`, `adp:tenant_id`

### Phase 2 — Production-ready, early paid customers (~6-8 weeks after Phase 1)

Scope:
1. Billing (Stripe: per-run metering + monthly platform fee + free tier)
2. Public onboarding UI at `adp.example.com`
3. Check Runs API integration for PR-triggered agents
4. Per-tenant observability dashboards (cost, usage, error rate)
5. SOC 2 Type 1 readiness (audit log retention, access reviews, runbooks)
6. DLP / PII scrubbing on inputs
7. Multi-repo support (install once, use on N repos)
8. Self-service persona selection (customer enables / disables personas per install)
9. Customer data residency docs (US-only + EU roadmap)
10. Additional channels: WhatsApp, Slack, Teams (via the multi-channel extensibility model)

### Phase 3 — Scale and differentiation (after Phase 2 stabilizes)

Scope:
1. Multi-tenant rule management (#271) fully wired
2. Customer-authored skills (drop an MCP skill address, their agents pick it up)
3. Org-level analytics (which agents get used, which issues resolved, team velocity)
4. Dedicated-infrastructure tier for enterprise customers (isolated EKS namespace, same software)
5. GitHub Marketplace + AWS Marketplace listings
6. Advanced access control (GitHub org-membership-based persona access)

## Dependencies and relationships

### EPICs this design depends on

- **#181** (SaaS identity) — tenant scoping, identity propagation
- **#132** (vault) — per-tenant credential storage for GitHub App + AWS role metadata + external API keys
- **#224** (cyber domain) — first domain app that benefits from hosted platform; proves the architecture
- **#309** (GitHub-based web auth) — complementary, shares GitHub identity surface but operates at a different layer

### What this EPIC unblocks

- Every future domain app (finance, legal, IR, supply chain) ships invisibly — customers don't need to redeploy anything
- Multi-channel ingress (WhatsApp, Slack, Teams, SIEM) via the extensibility model
- Product positioning as SaaS rather than "reference implementation"

## Open questions

Not blockers, but decisions that shape the design as it firms up:

1. **Free tier shape.** N runs/month always-free? Or time-limited trial? Affects onboarding funnel design.
2. **Pricing model.** Per-run metering? Per-seat monthly? Platform fee + per-run? Where do we sit vs. commercial sandbox precedent ($50-150k/year enterprise)?
3. **Data handling contract.** GDPR, HIPAA, sectoral regs — what do we accept, what do we reject? Legal conversation needed before regulated-industry customers.
4. **Self-hosted relationship.** Sunset self-hosted after hosted matures? Keep dual-track forever? Hybrid/open-source products typically keep both.
5. **SOC 2 timeline commitment.** Type 1 = few months; Type 2 = ~1 year of clean operations. Worth committing to a timeline?
6. **Marketplace strategy.** GitHub Marketplace has discovery value. AWS Marketplace has enterprise procurement. Both eventually; which first?
7. **Beads integration in hosted.** Today Beads is optional (`BEADS_ENABLED=false` works). In hosted, Beads becomes part of the pre-built agent image; per-tenant Beads state lives in our DDB rather than customer's. Detail to design in Phase 1.
8. **Custom composite actions.** Today's `modules/agent-factory/actions/setup-beads` and `update-board-status` are GitHub Actions composite actions. They need to be rewritten as Python/shell for the hosted pod model (or skipped if optional). Phase 1 skips; Phase 2 rewrites.

## References

- EPIC: [#308](https://github.com/aws-e/adp/issues/308) — Hosted multi-tenant ADP
- EPIC: [#309](https://github.com/aws-e/adp/issues/309) — GitHub-based web auth (sibling)
- EPIC: [#224](https://github.com/aws-e/adp/issues/224) — Cyber-security domain (first domain app)
- EPIC: [#181](https://github.com/aws-e/adp/issues/181) — SaaS identity
- EPIC: [#132](https://github.com/aws-e/adp/issues/132) — User vault
- Current workflow pattern: `.github/workflows/agent-developer.yml` (canonical per-persona workflow)
- Current agent runtime: `modules/agent-factory/agent/src/agent-worker.ts`
- Agent skill runtime: `modules/agent-factory/agent/src/skill-agent.ts`
- Repo-clone + two-workspace pattern: agent-developer.yml steps "Checkout agent code (adp)" + "Checkout target repository"
- Commercial precedent:
  - Devin (hosted agent platform)
  - Sourcegraph Cody Deep Context
  - Replit Agent
  - Datadog / Snyk / Lacework / Wiz (cross-account STS pattern)
- GitHub Apps docs: https://docs.github.com/en/apps/creating-github-apps
- Reusable workflows: https://docs.github.com/en/actions/using-workflows/reusing-workflows
- Check Runs API: https://docs.github.com/en/rest/checks/runs
- AWS STS + ExternalId best practices: https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create_for-user_externalid.html
