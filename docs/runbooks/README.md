# Runbooks

Operational runbooks for the ADP platform. Each runbook covers a specific
subsystem and is intended to be actionable — all commands should run as-is
(no placeholder substitution beyond `<profile>`).

## Index

| Runbook | Subsystem | When to use |
|---|---|---|
| [Gateway Migrations](./gateway-migrations.md) | Gateway / Postgres | Applying pending Alembic migrations, diagnosing migration state, partial-apply recovery |
