# ADP Platform Deployment Docs

Documentation for deploying ADP. Two tracks, pick one:

| Track | When to use | Doc |
|-------|-------------|-----|
| **Self-managed** | You clone the repo and drive the deploy yourself (or via your own local agent) with your own AWS credentials. No reliance on ADP's hosted services. | [`self-managed-deploy.md`](./self-managed-deploy.md) |
| **ADP-managed** | You have an ADP dashboard account and have linked your AWS account. Agents in ADP's infrastructure deploy into your account on your behalf. | [`adp-managed-deploy.md`](./adp-managed-deploy.md) |

## Shared references

- [`deployment-manifest.md`](./deployment-manifest.md) — what gets deployed where, with per-resource validation commands. Used by both tracks.
- [`self-managed-deploy-experience.md`](./self-managed-deploy-experience.md) — human-narrative companion to the self-managed track ("what to expect at each phase").
- [`platform_upgrades.md`](./platform_upgrades.md) — how to update an already-deployed platform (`deploy-all.sh --update`): commands, destroy gate, verification, rollback. Both tracks.
- [`customer-aws-setup.md`](./customer-aws-setup.md) — how to link an AWS account from the ADP dashboard. Required for the ADP-managed track.

## Phase status

Single source of truth for where each phase stands across both tracks. Updated as fixes land.

**Status legend:**
- ✅ **Verified** — phase has been run end-to-end against a real target account, all `deployment-manifest.md` validation commands pass, and the doc procedure has been followed without surprises.
- 🟡 **Code ready** — code/script changes are merged that should make the phase work for this track, but no end-to-end run has been done yet.
- 🟠 **Doc updated, code unchanged** — docs reflect current behavior, no code change was needed.
- ❌ **Blocked** — known gap in code or workflow that prevents this phase from running cleanly for this track. Linked issue tracks the fix.
- ⬜ **Not yet audited** — phase content for this track hasn't been reviewed against current code.

| # | Phase | Self-managed | ADP-managed | Code PR(s) | Doc PR(s) |
|---|---|---|---|---|---|
| 1 | Bootstrap (state bucket + lock table) | 🟡 Code ready | 🟡 Code ready | [#967](https://github.com/aws-e/adp/pull/967) | [#970](https://github.com/aws-e/adp/pull/970) |
| 2 | Preflight | 🟠 Doc updated, code unchanged | 🟠 Doc updated, code unchanged | — | [#970](https://github.com/aws-e/adp/pull/970), [#972](https://github.com/aws-e/adp/pull/972) |
| 3 | Platform infra (VPC + EKS + ECR + IAM + CodeBuild) | 🟡 Code ready | 🟡 Code ready | [#973](https://github.com/aws-e/adp/pull/973), [#974](https://github.com/aws-e/adp/pull/974), [#975](https://github.com/aws-e/adp/pull/975), [#976](https://github.com/aws-e/adp/pull/976) | _this PR_ |
| 4 | Gateway infra (RDS + Cognito + ElastiCache + CloudFront + API GW + KMS) | 🟡 Code ready | 🟡 Code ready | (same Stage A–D) | _this PR_ |
| 5 | Gateway backend (FastAPI on EKS + ALB) | 🟡 Code ready | 🟡 Code ready | (same Stage A–D) | _this PR_ |
| 6 | Gateway frontend (S3 + CloudFront SPA) | 🟡 Code ready | 🟡 Code ready | (same Stage A–D) | _this PR_ |
| 7 | Webhook ingress (API GW + Lambda + SQS + DynamoDB) | 🟡 Code ready | 🟡 Code ready | (same Stage A–D) | _this PR_ |
| 8 | Agent delivery (KEDA + ARC + WebSocket API + chat infra) | 🟡 Code ready | 🟡 Code ready | (same Stage A–D) | _this PR_ |
| 9 | Smoke test | ⬜ Not yet audited | ⬜ Not yet audited | — | — |

Stage A–D ([#973](https://github.com/aws-e/adp/pull/973) [#974](https://github.com/aws-e/adp/pull/974) [#975](https://github.com/aws-e/adp/pull/975) [#976](https://github.com/aws-e/adp/pull/976)) introduced `config/deployment.yml` + the `load-deploy-config` helper / composite action and refactored 15 workflows to consume it. With those merged, the workflow path no longer hardcodes the platform account, which **unblocks every later phase for both tracks**. End-to-end verification (a real deploy from a fresh checkout into a customer-linked account) is the path from 🟡 → ✅.

Definition of done for **each** phase:

- [ ] Self-managed track: a copy-paste operator can run the phase against their own AWS account from a clean clone, and the validation commands in [`deployment-manifest.md`](./deployment-manifest.md) all pass.
- [ ] ADP-managed track: an orchestrator dispatches the phase to a sub-agent, the sub-agent runs against the customer-linked account, and the same validation commands pass.
- [ ] `adp-managed-deploy.md` row flips to "✅ Verified" with the actual deploy-instance issue link as proof.
- [ ] `self-managed-deploy.md` and `self-managed-deploy-experience.md` for that phase match real commands + outputs.
- [ ] Status row above updates to ✅ for the relevant track(s).
