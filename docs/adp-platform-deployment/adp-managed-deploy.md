# ADP-Managed Deploy

> **Status:** Work in progress. Being built phase-by-phase via deploy-instance issues (see EPIC #684 + #966). Sections below get filled in as each deploy-test phase is unblocked and verified end-to-end. Do not assume any section is complete until the deploy-instance for that phase has posted a `## VERDICT: PASS`.

## When to use this track

Choose the **ADP-managed** deploy if:

- You already have an account on the hosted ADP platform.
- You've linked your AWS account in the ADP dashboard (Settings → AWS Access).
- You want agents running in **ADP's** infrastructure to do the deploy on your behalf.

You will not run `terraform`, `aws`, or `kubectl` yourself. You'll click a button (eventually — see [UI-driven deploy EPIC](#)) or comment on a GitHub issue, and an agent runs it.

If you don't have an ADP account, or you don't want to give ADP cross-account role access, use [self-managed-deploy.md](./self-managed-deploy.md) instead.

## Prerequisites

- An ADP dashboard account.
- An AWS account linked in the dashboard with a verified `aws_role` credential. The role's `MaxSessionDuration` should be ≥ 3600s (longer is better — single phases can exceed an hour).
- The label of your linked credential (e.g. `Dep-testing`). You can confirm via the dashboard's Settings → AWS Access page.

## How it works (high level)

```
┌──────────────────────┐                                ┌────────────────────┐
│  You                 │  1. file deploy-instance       │  Your AWS account  │
│  (ADP dashboard      │ ────────────────────────────►  │  (linked, verified)│
│   or GitHub)         │                                │                    │
└──────────────────────┘                                │  ▲                 │
                                                        │  │ STS AssumeRole  │
                                                        │  │ (role from vault)│
┌──────────────────────────┐                            │  │                 │
│  ADP platform account    │                            │  │                 │
│  (879318057152)          │  2. agent pod auto-assumes │  │                 │
│                          │ ─────────────────────────► │  ▼                 │
│  agent-scaledjob runs    │                            │  Resources         │
│  orchestrator + workers  │                            │  appear here       │
└──────────────────────────┘                            └────────────────────┘
```

The agent pod has STS credentials for **your** account in env at startup (auto-assumed via the gateway's `/internal/v1/credential-assume-role` endpoint, which reads your vaulted role). Every `aws`, `terraform`, `kubectl` command the agent runs lands in **your** account, not ADP's.

## Deploy phases

This is the same dependency order as the [self-managed deploy](./self-managed-deploy.md), but each phase runs inside an ADP agent pod instead of on your laptop.

| # | Phase | Status | Issue template |
|---|-------|--------|----------------|
| 1 | Bootstrap (state bucket + lock table) | _pending_ | #685 |
| 2 | Preflight | _pending_ | #686 |
| 3 | Platform infra (VPC + EKS + ECR + IAM + CodeBuild) | _pending_ | #687 |
| 4 | Gateway infra (RDS + Cognito + ElastiCache + CloudFront + API GW + KMS) | _pending_ | #688 |
| 5 | Gateway backend (FastAPI on EKS + ALB) | _pending_ | #689 |
| 6 | Gateway frontend (S3 + CloudFront SPA) | _pending_ | #690 |
| 7 | Webhook ingress (API GW + Lambda + SQS + DynamoDB) | _pending_ | #691 |
| 8 | Agent delivery (KEDA + ARC + WebSocket API + chat infra) | _pending_ | #692 |
| 9 | Smoke test | _pending_ | #693 |

(Status flips from "pending" → "verified" only after a real deploy-instance has run the phase end-to-end against a customer account.)

## Validation per phase

See [`deployment-manifest.md`](./deployment-manifest.md) for the canonical resource → validation-command mapping. The same validation suite applies to both tracks.

## Common failure modes

_To be filled in as each phase is verified. Each entry will name a real symptom seen during deploy-instance #946 (or successors), the root cause, and the fix that's been merged._

## Triggering a deploy

_The dashboard "Deploy ADP" button is a separate EPIC — see [#TBD]. Today, deploys are triggered by spawning a deploy-instance issue via the `Spawn Deploy Instance` workflow, which clones the canonical templates into per-deploy children. The orchestrator agent reads its assigned issue and walks the phases._

## References

- [`self-managed-deploy.md`](./self-managed-deploy.md) — the do-it-yourself track
- [`deployment-manifest.md`](./deployment-manifest.md) — what gets deployed + validation
- [`customer-aws-setup.md`](./customer-aws-setup.md) — connecting your AWS account to the ADP dashboard
- EPIC #684 — Minimum Viable Platform deploy test (parent EPIC for this work)
- Issue #966 — Per-workflow customer-account roles design
