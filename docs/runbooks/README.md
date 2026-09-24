# Runbooks

Operational runbooks for the ADP platform. Each runbook covers a specific
subsystem and is intended to be actionable — all commands should run as-is
(no placeholder substitution beyond `<profile>`).

## Index

| Runbook | Subsystem | When to use |
|---|---|---|
| [CLI Uplift Evaluation](./cli-uplift-evaluation.md) | CLI uplift (`adp` CLI, release routes, personal-AWS connect) | Dispatching the E2E evaluation on disposable EC2, reading per-case results, resuming an attempt, and verifying nothing was left running after a cancelled run |
| [Gateway Migrations](./gateway-migrations.md) | Gateway / Postgres | Applying pending Alembic migrations, diagnosing migration state, partial-apply recovery |
| [GitHub Auth Allowlist Remediation](./github-auth-allowlist-remediation.md) | Gateway / GitHub auth broker | Rolling out the fail-closed allowlist, auditing users provisioned under the old open default, configuring the org-check token |
| [Live Run-Control Evaluation](./agent-control-evaluation.md) | Agent live control | Running the Wave 1 control evaluation: fixture config contract, artifact requirements, exit codes, bounded cleanup |
| [EKS Pod IP Exhaustion](./eks-pod-ip-exhaustion.md) | Platform / EKS networking | Existing pods run but new ones fail with `failed to assign an IP address to container`: confirming subnet exhaustion, adding existing capacity subnets to the cluster's subnet set, the plan to expect, rollback |
| [NetworkPolicy Enforcement](./network-policy-enforcement.md) | Platform / EKS networking | Enabling NetworkPolicy enforcement on an Auto Mode cluster: ordered apply, verifying policies are actually enforced, rollback |
| [Superplane Monitor Grant Withdrawal](./superplane-monitor-grant-withdrawal.md) | Superplane domain app / platform monitor | Cutting the platform monitor over to the authenticated observation contract and withdrawing its direct Aurora table grant: receiver-before-sender ordering, continuity checks, expected health regressions, re-grant rollback |
| [Engine-Command Signing-Key Rotation](./engine-command-signing-key-rotation.md) | Webhook ingress + gateway orchestration | Seeding or rotating the `@agent-engine` command attribution keyring: bounded overlap window, signer/verifier inventory, proving the agent worker cohort cannot read the key |
