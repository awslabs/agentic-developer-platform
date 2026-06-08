# Deploying ADP (Kiro steering)

You are the deployment agent for ADP. Deploy it end-to-end from a fresh clone,
keep the user informed, and only ask when you genuinely need input.

**Follow these two files — do not deploy from memory or from this stub:**

1. [`AGENTS.md`](../../AGENTS.md) — the agent-behavior layer: how to confirm the
   target account, maintain `.adp-deploy-state.json`, the phase table, the
   "placeholder artifact" rule, verification, teardown, and when to call the user.
2. [`docs/adp-platform-deployment/deploy-quickstart.md`](../../docs/adp-platform-deployment/deploy-quickstart.md)
   — the authoritative, verified procedure: exact phase sequence, commands,
   verification, and gotchas (maintained against real end-to-end runs).

The essentials, so you don't start down the wrong path:

- **Confirm the target AWS account first** (`aws sts get-caller-identity` via the
  active `AWS_PROFILE`); get the user's OK before Phase 1.
- **There is no upfront GitHub setup.** GitHub is wired at the END
  (`register-github-app.sh`, Phase 8) for the agent path; gateway-only needs no
  GitHub. Any "Phase 0 / setup-org / 3 org-owned apps" instruction is the
  superseded legacy ARC track — do not run it.
- **`deploy-all.sh` chains Phases 1–6 only.** Phases 6c (broker), 6d
  (bootstrap-admin), 7 (webhook stack), and 8 (GitHub App) are separate
  stage-by-stage scripts — without them you have no working login, first admin,
  or agent path. See the phase table + placeholder-artifact rule in AGENTS.md.

This file is intentionally a redirect so the deploy procedure has one source of
truth (AGENTS.md + deploy-quickstart.md) and never drifts across copies.
