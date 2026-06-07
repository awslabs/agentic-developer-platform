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

### Deploy

```bash
./scripts/deploy.sh
```

This builds the Docker image, pushes to ECR, and runs `terraform apply`.

### Smoke Test

```bash
./scripts/smoke-test.sh
```

### Teardown

```bash
./scripts/teardown.sh
```

Destroys all resources, cleans ECR/Secrets Manager/state files, and verifies no orphans.

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
