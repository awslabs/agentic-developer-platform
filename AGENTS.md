# Agent Instructions — ADP (Agentic Developer Platform)

To deploy ADP, read and follow the canonical agent-deploy guide:

**[`docs/adp-platform-deployment/deploy-with-agent.md`](docs/adp-platform-deployment/deploy-with-agent.md)**

It is the single source of truth for the agent-behavior layer (confirm the
target account, the phase table, the "placeholder artifact" rule, state file,
verification, teardown, when to call the user). It in turn defers to
[`docs/adp-platform-deployment/deploy-quickstart.md`](docs/adp-platform-deployment/deploy-quickstart.md)
for the exact, verified commands.

The essentials, so you don't start down the wrong path:

- **Confirm the target AWS account first** (`aws sts get-caller-identity` via the
  active `AWS_PROFILE`) and get the user's OK before Phase 1.
- **There is no upfront GitHub setup.** GitHub is wired at the END for the agent
  path (UI flow: Settings → Connections → "Set up GitHub App"; or CLI fallback
  `register-github-app.sh`). Any "Phase 0 / setup-org / 3 org-owned apps"
  instruction is the superseded legacy ARC track — do not run it.
- **`deploy-all.sh` chains Phases 1–7** (steps 1–10/11: infra, gateway, ALB wire,
  frontend, broker, first-admin bootstrap, agent-factory, webhook stack) — only
  the GitHub App wiring (Phase 8/9) remains manual, per the bullet above.

This file is intentionally a redirect so the deploy procedure has one source of
truth and never drifts across the agent-entry files.
