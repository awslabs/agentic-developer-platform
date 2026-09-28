# gbrain — Experimental Persona Learning Module

Isolated experimental deployment of [gbrain](https://github.com/garrytan/gbrain) (v0.42.26.0) for evaluating per-persona experiential knowledge storage for ADP agents.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  ADP VPC (shared)                                       │
│                                                         │
│  ┌─────────────────┐         ┌───────────────────────┐ │
│  │  ADP EKS Pods   │──MCP──▶ │  gbrain Fargate       │ │
│  │  (agent tasks)  │  :3000  │  (ECS service)        │ │
│  └─────────────────┘         └──────────┬────────────┘ │
│                                         │              │
│                              ┌──────────▼────────────┐ │
│                              │  RDS PostgreSQL 16     │ │
│                              │  (pgvector)            │ │
│                              └───────────────────────┘ │
│                                                         │
│  ┌───────────────┐    ┌──────────────────────────────┐ │
│  │  S3 Bucket    │    │  EventBridge (dream cycle)   │ │
│  │  (brain repo) │    │  daily 3am UTC               │ │
│  └───────────────┘    └──────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

## Quick Start

### Deploy (one command, fully end-to-end)

```bash
./scripts/deploy.sh
```

This single command drives the **entire** deployment lifecycle — no manual steps,
no console clicks:

1. **Terraform apply** — creates/updates all infrastructure (ECS, RDS, ECR, IAM, etc.)
2. **CodeBuild** — builds and pushes the container image to ECR from committed main
3. **Service rollout** — force-new-deployment + waits for ECS service to stabilize
4. **DB migration** — enables pgvector, runs schema migrations to v113 (idempotent)
5. **Seed** — imports existing learnings from `agent_learning/` (idempotent upsert)
6. **Smoke test** — health + tools/list + write/read round-trip; **the deploy fails if this fails**

**Idempotent**: running `deploy.sh` on an already-healthy deployment is a green no-op.

**Repeatability guarantee**: `./scripts/teardown.sh` → `./scripts/deploy.sh` → fully working
gbrain (service healthy, schema v113, seeded, smoke test green) with zero manual intervention.

### Smoke Test (standalone)

```bash
./scripts/smoke-test.sh
```

### Teardown

**Via GitHub Actions (recommended):**

Run the `Gbrain Infra Destroy` workflow — type `gbrain` to confirm.

**Locally:**

```bash
./scripts/teardown.sh
```

The hardened teardown performs 8 steps in order:
1. Asserts integration kill-switch is off (no agent mid-call)
2. Scales ECS service to 0 (drains Fargate ENIs)
3. Empties S3 bucket (handles versioned objects)
4. Runs `terraform destroy` (removes all infra including CodeBuild)
5. Force-deletes ECR repository and Secrets Manager secrets
6. Wipes Terraform state files from the state bucket
7. Orphan audit — **fails loud** if it cannot verify (never false all-clear)
8. Post-destroy assertions (CodeBuild, ECS, RDS, ECR, S3 all confirmed gone)

Every step is idempotent and tolerates "already gone." The executing role needs
`tag:GetResources` permission for the orphan audit (step 7).

## Integration

gbrain is disabled by default. To enable for agent tasks:

```bash
# In agent pod environment:
GBRAIN_ENABLED=true
GBRAIN_MCP_URL=http://<internal-service-endpoint>:3000/mcp
GBRAIN_MCP_TOKEN=<from-secrets-manager>
```

Kill switch: set `GBRAIN_ENABLED=false` — agents immediately fall back to existing memory.

## Cost

~$35-55/month for single-user evaluation:
- RDS db.t4g.micro: ~$12/mo
- Fargate (1 vCPU, 2GB): ~$25/mo
- S3/ECR/CW: ~$3/mo

All resources tagged `ExperimentId=gbrain-eval-2026-06` for cost tracking.

## Files

| Path | Purpose |
|------|---------|
| `terraform/` | Infrastructure as Code (Fargate, RDS, S3, ECR, IAM) |
| `docker/` | Container image build context |
| `scripts/deploy.sh` | Full deployment automation |
| `scripts/teardown.sh` | Clean resource removal |
| `scripts/smoke-test.sh` | Validate MCP endpoint |
| `scripts/seed-brain.sh` | Import existing learnings |
| `config/gbrain.yml` | Brain schema (persona-scoped) |
| `config/schema-pack.yml` | Knowledge graph schema |
| `EXPERIMENT.md` | Evaluation criteria and protocol |

## Related

- Parent EPIC: #1219
- Research report: #1220
- Existing agent memory: `modules/agent-factory/agent/src/complex-task-chat/memory/dynamo-memory.ts`
