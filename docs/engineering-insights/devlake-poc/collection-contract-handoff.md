# Collection implementation handoff

This repairs the code handoff from S02, S09 and S12 to
[S03 #8890](https://github.com/aws-e/adp/issues/8890). It does not mark their live
acceptance complete. The schema-v1 placeholder, unconditional migration refusal
and unavailable L5 work-authority dependency prevented S03 from implementing a
working collection path. Merging those predecessors was not evidence that their
interfaces were ready.

## Schema and SQL authority

`modules/insights/poc/devlake/schema-manifest.json` now inventories schema v2:
six raw tables, five tool tables, four domain tables and four control tables.
The selected entities are GitHub repositories/issues/pull requests and Jira
boards/issues. The column definitions are generated from the pinned DevLake
revision by `schema-models/main.go`; the CI contract check regenerates and compares
them. Activating additional upstream extractors requires inventorying their
additional tables and publishing a new compatible migration. The full upstream
migration system is not a worker entry point.

The existing IAM/TLS migrator applies schema and cell grants in one transaction,
then independently reads back the exact manifest digest with a fresh IAM token.
The v2 readiness marker means the checked migration is available; a worker still
requires a matching receipt from its actual registered cell database. Existing
v1 receipts or incompatible digests are refused. The removal-capable migration
also accepts the explicitly pinned preceding v2 manifest, updates its receipt
transactionally, and leaves existing sealed payloads and manifests unchanged.
Migration ownership, writer DML
and exporter SELECT remain separate. No database is created by these checks.

The writer can read generations/checkpoints and execute only these control
routines. It cannot directly update control tables, truncate workspace, create
tables or change sealed history.

| API | Contract |
|---|---|
| `begin_collection(cell, expected_generation, tenant, project, operation, attempt)` | Compare and advance the cell generation under a lock; first expected generation is zero. Exact active-attempt replay returns the same generation. A sealed/cancelled attempt cannot be replayed as active. Tenant/Project cannot change within a cell. Call only after current gateway authority and schema admission. |
| `lock_generation(cell, generation)` | Verify and lock the collecting generation for the caller's transaction and install transaction-local write settings. Every workspace INSERT/UPDATE/DELETE trigger takes the same lock. A successor cannot pass an in-flight workspace transaction. |
| `cancel_collection(cell, generation)` | Cancel only the current collecting generation. It then fails all workspace/seal writes. |
| `seal_batch(cell, generation, batch_uuid, item_bytes[], manifest_bytes, checkpoints)` | Verify bounded exact payload bytes and all item digests, atomically insert immutable history, compare/advance checkpoints, and seal the generation. Any mismatch rolls back everything. |

Use the writer's `lock_generation` routine instead of `SELECT ... FOR UPDATE` on
`collection_generations`: PostgreSQL requires UPDATE permission for the latter,
which the writer deliberately does not hold. S03's preserved `generation.go`
helper needs this adapter change.

`item_bytes` are the exact JSON bytes of S03's `BatchItem`, including its complete
`NativeKey`, revision, native and normalized bytes, evidence links, policy,
field-map and schema versions. `manifest_bytes` are the exact JSON bytes of
`{BatchID, CellID, Generation, ItemDigests}` already hashed by `PrepareBatch`.
The array order must match `ItemDigests`. SQL independently checks each SHA-256,
identity, cell/tenant/Project, payload presence and source-reference link scope;
then returns the SHA-256 of the supplied manifest bytes. It stores the original
bytes without JSON re-encoding. Limits are 100,000 items and 100 MiB of item bytes.
The exporter reads `item_payload`, `native_payload`, `normalized_payload` and
`payload_digest`, joined to `manifest_payload` and `content_digest` by batch ID.
Workspace changes in later generations cannot alter these rows.

`checkpoints` is a JSON array with unique `source_key`, `expected_cursor` and
`next_cursor` fields. `expected_cursor: null` means no prior checkpoint. S03 must
encode the complete source identity in `source_key`. Cursor advancement occurs
only with a successful seal; exact active-generation replay is idempotent.

### Non-native removals and empty results

A removal is an explicit control record, not an upstream observation. Its
`BatchItem` retains the previously observed `NativeKey` and the current policy,
field-map and schema versions, and sets `RemovalReason` to either
`absent_in_complete_source` or `policy_excluded`. It has no revision, native
payload, normalized payload or evidence links. Both Go preparation and SQL
sealing reject removals that contain such data. Incomplete enumeration never
implies a removal.

`PrepareIncrementalItems` converts verified delta plans into native upserts and
non-native removals. `PrepareSourceBatch` binds the selected sources into the
exact manifest bytes as canonical, sorted `SourceKeys`. SQL requires those keys
to match the atomically submitted checkpoints and rejects foreign source items.
A source-bound batch can contain zero items: it records a completed empty change
set without fabricating a native record. An empty change set does not erase
existing records; explicit removals are required for that.

The shared query verifies the manifest and item digests before applying the
latest per-native-key record. A latest removal hides that identity from current
results and metrics without modifying its earlier history. Source-bound empty
manifests distinguish an observed empty population from missing coverage.
Sealed readback still returns and verifies the original B1 bytes after B2 removes
the corresponding current records.

The collection-contract CI check compiles the pinned Go collection test binary
and runs it against disposable PostgreSQL writer and reader roles. It exercises
the actual Go planning, sealing, query and checkpoint adapters, including revoked
authority rollback, immutable B1/B2 readback and empty-result sealing. Separate SQL
tests cover interrupted transactions, conflicting replay and the recognized v2
upgrade. These are local database checks, not provider or live-worker acceptance.

## Current collection work authority

The existing authenticated `/internal/v1/connector-contexts/resolve` and
`GET /internal/v1/connector-contexts/{handle}` routes now use
`connector_work_authority.py`, which reads the existing Product operation
journal. They no longer depend on the unrelated, unimplemented L5 work provider.
They still require verified workload identity, current Product delegation,
source visibility, collection grant, cell/realm policy and enabled module.

**S03 owns trusted collection-job admission, lease renewal/cancellation and the
worker HTTP adapter.** Its gateway job owner must create a `ProductOperation`
with `operation="collectConnectorSource"`; workers must never write gateway
storage directly. The required persisted contract is:

- Product, Project, tenant and binding ID/generation match the current selection.
- `state="running"`, unexpired operation and lease, `lease_owner=attempt_id` and
  `attempt_count=delegation.lease_fence`, with a positive fence.
- The request contains exact `consumer`, `delegation_id`,
  `delegation_revision`, `source_ref`, `cell_id` and `grant_id` values.
- The credential delegation itself binds the same operation/attempt/fence and
  source generation. Caller-supplied authority fields do not create a journal
  entry, delegation or lease.

A two-tool job has one source operation/context per source and one shared cell
collection generation. Recheck both source contexts before shared writes.

This contract reuses the existing journal rather than adding a separate job or
tenant authority. S03's job owner must claim/renew with conditional updates,
reject concurrent/stale owners, and retain terminal history. The resolver does
not create work as a side effect. No journal entry means
`stale_collection_work`, not a fabricated permit.

Context reads additionally expose `binding_id`, `binding_revision`, `provider`,
`consumer`, `lease_fence` and `module_generation`. Expiry is bounded by current
work and grant deadlines. The worker must re-read this authenticated, uncached
context before/after provider pages and before committing/sealing; any changed
identity/version, failed lookup, expiry or disable cancels work. SQL generation
fencing is an additional local boundary, not a replacement for source authority.
S03 retains ownership of provider fetching, classification, normalization,
checkpoint planning, broker integration and the actual collection entry point.

## IAM pool and verification

`cmd/collector-worker/internal/celldb.Open` supplies a bounded `database/sql`
pool for a registered RDS target. Pass that pool to GORM's PostgreSQL dialector;
do not open another driver/DSN connection. The pinned pgx `BeforeConnect` hook
signs a token for every new physical connection with the SDK's renewable IRSA or
container workload provider. The helper rejects static credential providers,
ambient database settings, unsupported targets and missing/invalid CA bundles.
TLS verifies the hostname and CA, with no plaintext fallback. Connection errors
are redacted. S03 must call this helper from its real worker entry point; adding
a helper alone is not deployed runtime acceptance.

The `DevLake POC / collection contracts` CI job regenerates upstream model DDL,
runs the Go pool tests, and starts disposable local PostgreSQL to verify role
boundaries, workspace fencing, bounded sealing, checkpoint rollback, exact byte
readback and successor locking. Gateway tests exercise the actual stored work
journal and expiry/cancellation/fence/source/cell/grant changes. These checks
require no AWS deployment. Actual IAM token ageing, broker/provider traffic,
cloud isolation, runtime finalization and the cumulative campaign remain with
the existing story acceptance owners. Existing spending limits and completed
flow history are preserved when S03 is resumed.
