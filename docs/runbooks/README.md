# Runbooks

Operational runbooks for the ADP platform. Each runbook covers a specific
subsystem and is intended to be actionable — all commands should run as-is
(no placeholder substitution beyond `<profile>`).

## Index

| Runbook | Subsystem | When to use |
|---|---|---|
| [Gateway Migrations](./gateway-migrations.md) | Gateway / Postgres | Applying pending Alembic migrations, diagnosing migration state, partial-apply recovery |
| [GitHub Auth Allowlist Remediation](./github-auth-allowlist-remediation.md) | Gateway / GitHub auth broker | Rolling out the fail-closed allowlist, auditing users provisioned under the old open default, configuring the org-check token |
| [Live Run-Control Evaluation](./agent-control-evaluation.md) | Agent live control | Running the Wave 1 control evaluation: fixture config contract, artifact requirements, exit codes, bounded cleanup |
