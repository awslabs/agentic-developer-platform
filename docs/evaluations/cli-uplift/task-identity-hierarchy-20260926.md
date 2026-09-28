# Task identity hierarchy correction

All Task personas share resolve_task_model for admission and each paid model
operation. Its service-principal policy context previously set team and
department to empty strings. That skipped team Bedrock mappings and both
team/department budget limits. The Task settlement event also omitted department
and hard-coded account_type=service, including for human-owned Tasks.

Tasks now request a full hierarchy from current server-side identity records.
Cognito aliases use strongly consistent agent-client metadata reads. IAM agent
aliases use tenant-scoped registry discovery followed by a strongly consistent
base-row read. Legacy service-account aliases resolve through tenant-scoped SQL
rows. Every active alias must identify the same hierarchy. Human Tasks retain
the existing workspace-aware primary-team resolution and validate that team's
department. SQL joins validate the tenant, team, and department together.

Missing, disabled, foreign-tenant, contradictory, and unsupported assignments
raise task_identity_hierarchy_unavailable before routing, quoting, or provider
handoff. There is no tenant-only fallback. The lookup runs again for each paid
operation; changed assignments use current hierarchy and failed reads cannot
reuse an earlier successful result. Canonical principal IDs and Task ownership
remain stable. Usage events carry the resolved department/team and actual
human/service caller kind.

The change requires no database migration. Existing Task service principals
without hierarchy assignments will be refused until their authoritative identity
metadata is configured. No default team is silently assigned. Non-Task policy
resolution retains its existing contract. The independent Opus pricing fix is
PR #6418; identity hierarchy does not refresh pricing evidence.

A read-only isolated-process check against live records confirmed that
sophos-labs-hierarchy-opus5-check (principal
d6a4b683-7e10-4f81-8dd4-1d4facfc5026) resolves to tenant sophos-labs, department
sophos-labs-dept-default, and team sophos-labs-team-default. Budget hierarchy
includes service account, team, department, and organization. The existing
sophos-labs-tao principal is refused because its metadata has no full hierarchy.
This check did not modify serving code, identities, policies, or preferences and
made no model calls. It is not a deployment receipt.
