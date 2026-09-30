# Runbooks

Operational runbooks for the ADP platform. Each runbook covers a specific
subsystem and is intended to be actionable — all commands should run as-is
(no placeholder substitution beyond `<profile>`).

## Index

| Runbook | Subsystem | When to use |
|---|---|---|
| [Regression testing](../regression-testing/README.md) | CLI Uplift and combined nightly regression | Choosing coverage, configuring targets, dispatching evaluations, interpreting results, and cleanup |
| [Gateway Migrations](./gateway-migrations.md) | Gateway / Postgres | Applying pending Alembic migrations, diagnosing migration state, partial-apply recovery |
| [GitHub Auth Allowlist Remediation](./github-auth-allowlist-remediation.md) | Gateway / GitHub auth broker | Rolling out the fail-closed allowlist, auditing users provisioned under the old open default, configuring the org-check token |
| [Live Run-Control Evaluation](./agent-control-evaluation.md) | Agent live control | Running the Wave 1 control evaluation: fixture config contract, artifact requirements, exit codes, bounded cleanup |
| [EKS Pod IP Exhaustion](./eks-pod-ip-exhaustion.md) | Platform / EKS networking | Existing pods run but new ones fail with `failed to assign an IP address to container`: confirming subnet exhaustion, adding existing capacity subnets to the cluster's subnet set, the reviewed scoped saved plan to expect, retention across deployments, rollback |
| [NetworkPolicy Enforcement](./network-policy-enforcement.md) | Platform / EKS networking | Enabling NetworkPolicy enforcement on an Auto Mode cluster: ordered apply, verifying policies are actually enforced, rollback |
| [Superplane Monitor Grant Withdrawal](./superplane-monitor-grant-withdrawal.md) | Superplane domain app / platform monitor | Cutting the platform monitor over to the authenticated observation contract and withdrawing its direct Aurora table grant: receiver-before-sender ordering, continuity checks, expected health regressions, re-grant rollback |
| [Engine-Command Signing-Key Rotation](./engine-command-signing-key-rotation.md) | Webhook ingress + gateway orchestration | Seeding or rotating the `@agent-engine` command attribution keyring: bounded overlap window, signer/verifier inventory, proving the agent worker cohort cannot read the key |
| [Identity Provenance Enforcement Rollout](./identity-provenance-rollout.md) | Webhook ingress (agent authority) + gateway identity index | Landing deny-by-default authority for unproven identity links: writer→backfill→enforce ordering, the cross-tenant ambiguity reduction, verifying both the legitimate path and the refusal metric, code-level rollback |
