# Model policy and attributable persona costs

Read personal costs or a tenant-authorized service principal's catalogue/costs:

```sh
adp models costs --persona architect --json
adp models costs --persona architect --service-principal PRINCIPAL --chain CHAIN --json
adp models catalog --persona architect --service-principal PRINCIPAL --json
```

Costs retain the canonical preference-owner identity, authenticated tenant, chain
filter, and observed/estimated/partial/unknown status. Entries are filtered by
persona; the explicitly labelled aggregate remains all personas for the selected
owner/chain. Unknown amounts stay null and partial amounts remain lower bounds.
Managed catalogue uses the selected service principal's destination/restrictions,
not the administrator's personal eligibility. A catalogue name is not proof a
model is executable: retain selectable, reason and probe evidence.

Platform administrators can inspect and change defaults and runtime posture:

```sh
adp admin models default show --compatibility-class claude-agent-sdk --json
adp admin models default set --compatibility-class claude-agent-sdk --model CANONICAL_MODEL --dry-run --json
adp admin models default set --compatibility-class claude-agent-sdk --model CANONICAL_MODEL --expect-version 1 --operation-id UUID --reason 'reviewed SDK evidence' --yes --json
adp admin models posture show --compatibility-class claude-agent-sdk --json
adp admin models posture set --compatibility-class claude-agent-sdk --posture enforcing --dry-run --json
adp admin models posture set --compatibility-class claude-agent-sdk --posture enforcing --expect-version 1 --operation-id UUID --reason 'qualified rollout' --yes --json
adp admin models posture rollback --compatibility-class claude-agent-sdk --to-version 1 --expect-version 2 --operation-id UUID --reason 'restore audited posture' --dry-run --json
```

Use a fresh UUID for each reviewed change and reuse the exact UUID, class, version,
reason and requested value when recovering unknown delivery. The gateway stores
the receipt and policy change in one transaction. Replay returns the original
response without rewriting newer policy. A different request with that UUID
conflicts. Distinct concurrent changes compare against the version in the
canonical conditional update. `--dry-run` overrides `--yes` and never writes.

Default previews use the same platform-destination SDK evidence checks as default
promotion. Missing or stale evidence remains unavailable. Posture changes retain
the existing disabled/report_only/enforcing vocabulary and measured propagation
bound. Rollback resolves `--to-version` from authoritative historical audit entries;
it never trusts a client-supplied old posture. It creates a new monotonic revision
with lineage back to the source audit. Missing historical evidence refuses rollback.

Only platform administrators can change global default/posture policy. Tenant
admins' authority over service mappings does not extend to platform policy.
Inherited selections, explicit preferences and profile precedence remain in their
canonical services. Profile lifecycle and trigger propagation remain owned by
#5564; these commands do not implement a profile store.

E38 adds served catalogue/cost reads to the existing nightly evaluation. Live
platform changes, rollback/replay races, and bounded local/hosted inference with
model-decision evidence and restoration remain held pending owned fixtures.
Saved policy, passing tests and readback do not establish that a worker selected
or invoked a model.
