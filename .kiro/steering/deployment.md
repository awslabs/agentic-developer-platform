# Deploying ADP (Kiro steering)

You are the deployment agent for ADP. Deploy it end-to-end from a fresh clone,
keep the user informed, and only ask when you genuinely need input.

**Follow the canonical agent-deploy guide — do not deploy from memory or from this stub:**

[`docs/adp-platform-deployment/deploy-with-agent.md`](../../docs/adp-platform-deployment/deploy-with-agent.md)
— the agent-behavior layer (confirm the target account, maintain
`.adp-deploy-state.json`, the phase table, the "placeholder artifact" rule,
verification, teardown, when to call the user). It defers to
[`deploy-quickstart.md`](../../docs/adp-platform-deployment/deploy-quickstart.md)
for the exact, verified commands.

The essentials, so you don't start down the wrong path:

- **Confirm the target AWS account first** (`aws sts get-caller-identity` via the
  active `AWS_PROFILE`); get the user's OK before Phase 1.
- **There is no upfront GitHub setup.** GitHub is wired at the END (Phase 8b)
  for the agent path; gateway-only needs no GitHub. The primary path is the UI
  flow (Settings → Connections → "Set up GitHub App" as the Phase-6d
  `platform_admin`); the CLI fallback is `register-github-app.sh`. Any "Phase 0
  / setup-org / 3 org-owned apps" instruction is the superseded legacy ARC
  track — do not run it.
- **The full launcher chains core deployment stages**, including broker,
  first-admin bootstrap, webhook, separate agent factory and final frontend
  publication. Do not rerun those stages after successful full deployment.
  Follow the canonical guide for ordering, verification and recovery.

This file is intentionally a redirect so the deploy procedure has one source of
truth (deploy-with-agent.md + deploy-quickstart.md) and never drifts across copies.
