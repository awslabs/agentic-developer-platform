# Workspace provisioning and retirement

This package implements the workspace provider boundary for #5534. The current
component, `load_bootstrap_retirement_inventory`, reads deletion candidates from
the completed bootstrap registration and its PostgreSQL ownership journals. It
requires a current service-resolved teardown binding. It checks the canonical
Workspace/Cluster association and metadata before returning any ownership.

The reader refuses outstanding bootstrap recovery, missing or partial prerequisite
inventories, changed canonical targets and ambiguous retained grants. Supervisor
grants adopted across bootstrap retries are deduplicated by exact immutable
identity. A pre-existing namespace remains preserved even when it carries an ADP
owner label. BYOC cluster preservation is explicit in the returned inventory.

The native bootstrap adapter also journals its six controller objects before each
create, including ServiceAccount, Deployment and both scopes of RBAC. It records
the provider UID and full observed specification digest. A retry reuses these
identities; a lost response is recovered from the committed creation marker and
provider read. Pre-existing matching objects are recorded as adopted and are never
promoted to deletion ownership. Changed objects refuse instead of being overwritten.
CRDs remain shared and are excluded from this ownership inventory.

The retirement reader exposes `components` and `components_complete`. Missing
legacy component records leave `components_complete=False`: downstream retirement
must refuse automatic component deletion in that case. A completion flag with a
missing object, unresolved creation, changed target or contradictory UID refuses.
Registration outcomes now carry the same component ownership and UIDs as the journal.

Recovery keeps the interrupted owner's state intact when another bootstrap attempt
is refused. That contender cannot clear the persisted recovery claim or restore the
owner's scheduling interlock. A process interrupted after creating the controller
uses the durable component record for handover, including when the old local
`controller_installed` flag never committed.

This is an ownership read boundary. The full story still requires the admitted
provider execution path, mode-specific account/cluster/bootstrap composition,
governed drain, durable retirement progress, grant and resource deletion, and
provider/cost verification. Consumers must recheck live immutable identities at
each deletion; the returned inventory alone is not mutation authority. Account
closure remains outside workspace retirement.

Run the producer-to-consumer tests from the repository root:

```sh
python -m pytest modules/domain-apps/superplane/workspace_provisioning/tests -q
```

Tests execute the real bootstrap and canonical registration against disposable
PostgreSQL; only cloud/Kubernetes transport is doubled. They perform no live
provisioning or retirement.
