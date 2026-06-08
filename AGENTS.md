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
- **There is no upfront GitHub setup.** GitHub is wired at the END
  (`register-github-app.sh`) for the agent path. Any "Phase 0 / setup-org / 3
  org-owned apps" instruction is the superseded legacy ARC track — do not run it.
- **`deploy-all.sh` chains Phases 1–6 only** — the broker, first-admin bootstrap,
  webhook stack, and GitHub App (6c/6d/7/8) are separate scripts.

This file is intentionally a redirect so the deploy procedure has one source of
truth and never drifts across the agent-entry files.
