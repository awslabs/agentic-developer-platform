# ADP Platform Deployment Docs

Documentation for deploying ADP. Two tracks, pick one:

| Track | When to use | Doc |
|-------|-------------|-----|
| **Self-managed** | You clone the repo and drive the deploy yourself (or via your own local agent) with your own AWS credentials. No reliance on ADP's hosted services. | [`self-managed-deploy.md`](./self-managed-deploy.md) |
| **ADP-managed** | You have an ADP dashboard account and have linked your AWS account. Agents in ADP's infrastructure deploy into your account on your behalf. | [`adp-managed-deploy.md`](./adp-managed-deploy.md) |

## Shared references

- [`deployment-manifest.md`](./deployment-manifest.md) — what gets deployed where, with per-resource validation commands. Used by both tracks.
- [`self-managed-deploy-experience.md`](./self-managed-deploy-experience.md) — human-narrative companion to the self-managed track ("what to expect at each phase").
- [`customer-aws-setup.md`](./customer-aws-setup.md) — how to link an AWS account from the ADP dashboard. Required for the ADP-managed track.
