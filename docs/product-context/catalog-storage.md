# Proposed catalog storage and transaction contract (AC-01)

**Design only; no catalog tables, API routes or deployment are claimed.** The
catalog authority and workflow decision are tracked in AC-03. The authoritative
record shapes are [Context v1](context-model-v1.schema.json); the strict catalog
wire definitions in [catalog-schemas.json](catalog-schemas.json) reuse its types.
Do not add Portfolio fields to Profile creation or use Profile's `product_changes`
as the owner of these relationships. See the [Profile business boundary](../product-onboarding/design/api.md#business-catalog-boundary).

## Identities and writes

An authoritative Portfolio and Initiative each have a stable `(tenant_id,
source_connection_id, id)`, an immutable revision and recorded time; neither
has a governing Project merely to exist. A Contribution has the same qualified
identity plus a **governed** `project_id`, and its `product_ref.project_id` must
equal that Project. Its `initiative_ref` is an authorized projection, not a new
Initiative copy. Preserve external native IDs, namespace and native revision;
display names are not keys. Product records/Project bindings remain Profile-owned.
Neither Portfolio membership nor Contribution creates native work, new access,
funding authority or a Portfolio allocation target.

Catalog mutations require an authorized scope, an idempotency key and the exact
expected current revision; a replay with the same key and payload returns the
original receipt, while a changed payload returns `409 conflict`. A stale
revision returns `412 stale_revision`, and a missing precondition `428
precondition_required`. A checked mutation atomically appends its version and
receipt, advances the current pointer and emits a revision-pinned change record.
Commit only after endpoint/source authorization, type and interval validation;
on failure append nothing. A change record includes source connection, native
record revision, relationship revision, mapping version, effective interval and
recorded time. History is append-only; even a retroactive correction adds a new
version with `supersedes_revision`, never updates the previous version.

## Relational constraints (proposed)

- `catalog_records`: PK `(tenant_id, source_connection_id, kind, id, revision)`;
  `kind IN (portfolio, initiative, contribution)`; immutable typed payload and
  `recorded_at`. Current pointer unique per qualified logical record. Each
  Contribution stores `project_id`, `initiative_ref`, `product_ref`, its
  `[valid_from,valid_to)` and the exact canonical tuple.
- `catalog_membership_versions`: PK `(tenant_id, relationship_id,
  relationship_revision)`, immutable qualified Portfolio and Product refs,
  `source_connection_id`, `mapping_version`, `[valid_from,valid_to)`,
  `recorded_at`, `supersedes_revision` and `source_refs`. Resolve the logical
  pair by `(tenant_id, portfolio source/id, product source/id)` **without**
  Project projection as a second membership. A pair may have successive,
  non-overlapping effective intervals, never two effective memberships at one
  time. No `portfolio→portfolio` edge or synthetic parent ID exists.
- `catalog_contribution_versions`: immutable payload/interval versions, unique
  active commitment by `(tenant_id, initiative source/id, product source/id,
  project_id)`; at most one *current* commitment for that tuple. An effective
  interval cannot be empty or overlap another current version for the tuple.
  Closed history remains readable under present authorization. A revised
  objective/dates retains the same Contribution ID and appends a version.
- `catalog_mutation_receipts`: unique `(tenant_id, actor scope, operation,
  idempotency_key)` with payload digest, outcome revision and change ID;
  locks and version comparisons share the same transaction as each mutation.
  Retain receipts and versions on rollback. Use indexed tuple lookup and
  exclusion on effective ranges of **current logical versions** (not their
  immutable superseded snapshots). Enforce endpoint types, tenant, Product ↔
  Project binding and authorized source in the service as well as FK-compatible
  indexed keys; keep `recorded_at` separate from effective time.

Close a membership by appending a version with `valid_to`, preserving its
original `valid_from`; reopening later creates a new logical relationship ID.
For Contributions, ending a commitment closes its current interval by appending
a version; reopening after the end uses a new ID. Serialize writes on sorted
tenant/qualified pair or commitment-tuple locks, then check overlap and expected
revision under that lock. This prevents two racing requests from both passing a
read-before-write check. A revision-pinned query resolves the latest version
known at `recorded_at ≤ decision_time` before evaluating the effective interval.

The [fixture cases](catalog-contract-examples.json) specify schema-valid shapes,
many-to-many membership and rejected nested/duplicate/invalid-history writes.
Run `python3 docs/product-context/check_catalog_contract.py` with the documented
`jsonschema` dependency to check them independently of any deployed service.
