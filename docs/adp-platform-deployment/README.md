# Deploy ADP

Start with **[Install and upgrade ADP](deploy-quickstart.md)**. It covers AWS
profiles, installing a GitHub Release, upgrading, connecting GitHub, checking
the result and removing a deployment.

| Need | Read |
|---|---|
| Install or upgrade a customer account | [Quickstart](deploy-quickstart.md) |
| Have an AI agent perform the deployment | [Agent instructions](deploy-with-agent.md), then the quickstart |
| Investigate an upgrade or use advanced flags | [Upgrade reference](platform_upgrades.md) |
| Diagnose a phase or verify individual resources | [Phase reference](deployment-reference.md) and [resource manifest](deployment-manifest.md) |
| Build once and promote internally | [Internal release promotion](release-promotion.md) |

The customer launcher selects a published GitHub Release and builds from its
source. Internal promotion uses a verified artifact manifest and currently
covers integration-test and pre-production. These are distinct release paths.
Hosted cross-account bootstrap is [unavailable](adp-managed-deploy.md); customer
installs use customer-controlled AWS credentials.

## Specialized references

- [Bedrock first-use readiness](bedrock-first-run.md)
- [Persona model mapping](persona-model-mapping.md)
- [Bedrock invocation logging](bedrock-invocation-logging.md)
- [Pricing release verification](pricing-release.md)
- [Customer AWS connection roles](customer-aws-setup.md) — connection roles do not bootstrap ADP

## Design history

These explain decisions and proposals, not commands to follow for deployment:

- [Deployment orchestration design](deploy-all-update-design.md)
- [Update-mode design](deploy-all-update-mode-design.md)
- [One-click design](one-click-deploy-design.md) and [decisions](one-click-deploy-decisions.md)
- [Teardown design](undeploy-design.md)
- [Archived self-managed guide](archive/self-managed-deploy.md)
- [Archived deployment walkthrough](archive/self-managed-deploy-experience.md)

Keep customer commands in the quickstart, agent behavior in the agent guide,
and detailed operations in the references. Historical implementation status and
old run results are not current deployment acceptance evidence.

Hosted cross-account bootstrap is unavailable. Dashboard-linked AWS roles are
for steady-state operations only; install ADP with customer-controlled AWS credentials.
