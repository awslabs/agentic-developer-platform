# Domain Apps

Domain-specific application packs built on top of the ADP platform. Each domain app brings its own agents, tools, skills, infrastructure, and (optionally) UX — but relies on the shared platform for compute, LLM access, agent runtime, and the harness for cross-cutting concerns.

## What This Is

A domain app is a vertical solution for a specific professional domain. It uses ADP as substrate: the Bedrock Gateway for LLM calls, the Agent Factory for runtime, the harness for tool routing and policy, and shared infrastructure (EKS, IAM, Secrets Manager) for operations.

Domain apps are peers — they communicate with each other only through the harness (events, tools, context), never via direct imports.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Domain Apps Layer                             │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │  cyber/      │  │  ml/ (future)│  │  data/       │          │
│  │              │  │              │  │  (future)    │          │
│  │  Threat      │  │  ML Platform │  │  Data        │          │
│  │  Research    │  │  Adapters    │  │  Engineering │          │
│  └──────┬───────┘  └──────────────┘  └──────────────┘          │
│         │                                                       │
└─────────┼───────────────────────────────────────────────────────┘
          │ uses
          ▼
┌─────────────────────────────────────────────────────────────────┐
│  ADP Platform (shared)                                          │
│  ├── Bedrock Gateway (LLM egress)                               │
│  ├── Agent Factory (runtime, GitHub triggers, scaling)          │
│  ├── Harness (tools, jobs, events, artifacts, HITL)             │
│  └── Infrastructure (EKS, VPC, IAM, Secrets Manager)            │
└─────────────────────────────────────────────────────────────────┘
```

## Domain App Shape

Every domain app follows the same folder structure:

```
modules/domain-apps/<domain>/
├── agent/              # Agent declarations (personas + skills)
│   ├── personas/       # Markdown persona definitions
│   └── skills/         # Skill playbooks (SKILL.md + code)
├── workers/            # Domain-specific worker containers
├── infra/              # Domain-specific Terraform (VPCs, queues, compute)
├── k8s/                # Kubernetes manifests (ScaledJobs, services)
├── codebuild/          # Build specifications
├── scripts/            # Deploy and operational scripts
└── image-builder/      # Custom AMIs or container images (if needed)
```

## Current Domain Apps

### `cyber/` — Threat Research Assistant

A research assistant for threat researchers that performs automated malware analysis and URL triage.

**One persona** (`malware-analysis-agent`) drives **two analytical paths**:

#### File Analysis (7-Stage Pipeline)

```
Sample submission
    │
    ├── Stage 1: Triage (sandboxed worker) — hashes, file type, strings, IOCs
    ├── Stage 2: OSINT Research (reasoning agent) — prior reporting, MITRE hypothesis
    ├── Stage 3: Static Analysis (sandboxed worker) — YARA, IAT, oletools
    ├── Stage 4: Dynamic Analysis (CAPE host) — detonation, process tree, behavior
    ├── Stage 5: Correlation (reasoning agent) — combine evidence → MITRE ATT&CK
    ├── Stage 6: Verdict (reasoning agent) — severity, confidence, IOCs
    └── Stage 7: Report (reasoning agent) — case file bundle (STIX, CSV, hunt queries)
```

Key safety property: the reasoning tier never holds sample bytes. File bytes stay in the isolated Threat Research VPC (separate EKS cluster, CAPE hosts with KVM/libvirt, no internet, INetSim for fake services).

#### URL Analysis (Single-Turn Skill)

```
URL submission
    │
    ├── Pre-flight denylist check
    ├── AgentCore Browser session (AWS-managed ephemeral Chromium)
    ├── Navigate + capture (screenshots, forms, redirects, downloads)
    ├── Enrichment (WHOIS, PassiveDNS, crt.sh, VirusTotal, URLhaus, MISP)
    ├── Deterministic verdict scoring (verdict.py)
    └── Markdown forensic report
```

Key safety property: URL content never leaves the AgentCore Browser session (AWS-managed, ≤5-min TTL, destroyed after use).

#### Cyber Infrastructure

| Component | Purpose |
|-----------|---------|
| Threat Research VPC | Isolated network for file bytes. CAPE hosts, KVM sandboxes, INetSim. No internet. |
| CAPE Fleet | EC2 Spot instances with nested virtualization. Scale 0-50. Snapshotted between samples. |
| AgentCore Browser | AWS-managed ephemeral Chromium for URL visits. SigV4 CDP WebSocket. |
| SQS FIFO + KEDA | One pod per message. Per-tenant `MessageGroupId` prevents cross-tenant blocking. |
| Evidence Bucket | S3 for URL analysis screenshots and evidence envelopes. AES-256, 30-day lifecycle. |
| DynamoDB | Task state, case tracking, CAPE host registry |
| Three-tier YARA | `public/` (hourly ingestion) + `org/` (MSSP-curated) + `tenant/` (customer private) |

#### Cyber Deployment

```bash
cd modules/domain-apps/cyber/scripts
./deploy.sh
```

Deploys: Terraform infrastructure (VPC, peering, SQS, KEDA, DDB, ECR, IRSA) + K8s ScaledJobs + CAPE host bootstrap.

## Adding a New Domain App

1. Create `modules/domain-apps/<domain>/` with the standard folder shape
2. Define personas in `agent/personas/` (markdown files the agent runtime loads)
3. Define skills in `agent/skills/` (SKILL.md playbooks + supporting code)
4. Add domain-specific infrastructure in `infra/` (Terraform)
5. Register tools/jobs/events with the harness via contracts (when harness surfaces are built)

The domain app should:
- Use the Bedrock Gateway for all LLM calls (inherits rate limiting, audit, cost tracking)
- Use the Agent Factory runtime for agent execution (inherits scaling, GitHub integration)
- Keep domain-specific bytes/data in isolated infrastructure (separate VPCs, dedicated buckets)
- Never import code from other domain apps — communicate via harness surfaces only

## What Doesn't Belong Here

| Kind | Where It Goes |
|------|---------------|
| Platform substrate (LLM proxy, agent runtime) | `modules/gateway/`, `modules/agent-factory/` |
| Harness surfaces (tool routing, job scheduling) | `modules/harness/` |
| Per-user products (vault, knowledge) | `modules/user-services/` |
| Shared infrastructure (VPC, EKS, IAM) | `platform/infra/` |

**Rule of thumb:** if it's domain-specific knowledge, tooling, or infrastructure that only makes sense for one professional vertical — it's a domain app.
