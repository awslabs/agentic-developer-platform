# Shared Product Profile

The Profile tab belongs to a Product. Switching the workspace Project changes
Build, budgets and execution scope; it does not change the Profile resource list
or section completion.

The Profile contains Product details, Code repositories, Environments, Backlog
and Knowledge. Repository purpose is Product code or Reference; a Product may
choose one default code repository. This is separate from the delivery home
repository used for epics. Named environments have a type, a connected account
or deployment application, and an optional deployment target. Dev and Prod can
share an account while identifying different namespaces, stacks or applications.

Manage connections reuses the existing source picker. New product defaults are
linked through the Base Project. The connection manager can also inspect another
Project's explicit source selections. Knowledge opens the existing Product team
collection; personal knowledge remains separate.

## Persistence and API

Migration `183_product_profile_config` adds nullable
`products.profile_configuration`. Existing Products need no data rewrite.
Repository settings use a stable identity derived from source kind, provider,
namespace and native ID. The Product catalogue groups matching bindings across
active Projects. Existing named environment selections are included until
explicitly edited or removed. Labels are display metadata, never source authority.

`GET /v1/products/{product_id}/profile` returns the shared resources, environments,
section completion and Base Project. `PATCH` accepts Product fields and a partial
`profile_configuration` update. It uses the existing `If-Profile-Revision` and
`Idempotency-Key` contract, product-wide audit history and stale-write responses.
Repository map entries update individually. Environment map entries upsert by ID;
`null` removes that named environment without unlinking its account. A default
repository must be accessible Product code. Optional section applicability is
saved separately and never deletes linked resources.

Audit receipts retain raw `profile_configuration`; responses derive the authorized
`shared_profile`. Its `revision` identifies the saved settings, while
`catalogue_revision` identifies the current Product resource catalogue. Source
permissions and health are always checked at read time, including historical reads.

The existing project profile read (including `adp product show --json`) also
returns `shared_profile`. This gives agent and service consumers the same saved
repository purposes and environment definitions alongside project configuration.
The standalone Product read includes the Knowledge store's saved asset count;
project reads mark this cross-store completion check unavailable when not read.
An unavailable Knowledge store is not reported as an empty collection.

## Completion and authority

Completion counts five sections, not individual resources. Product details need
name, purpose and owner. Code requires a Product code repository; environments
require a named connected target; backlog requires a linked tracker; Knowledge
requires a shared asset or linked document. Optional sections can be marked not
applicable. Missing sections link directly to their card. Connection verification
and ingestion health are separate from saved configuration completion.

Product team and platform-admin visibility checks remain in effect. Resource
metadata is redacted when the actor cannot access the underlying connection.
Service projections include only bindings permitted by the current source grant.
The product's saved completion does not drop because connection access or health
changes, although an inaccessible resource's details remain hidden.

The catalogue retains source binding IDs, generations and revisions, and Product
Project relationship IDs and revisions for Engineering Insights lineage. Shared
settings do not widen grants, copy credentials, rewrite project selections or
change run attribution. Execution continues to resolve an explicit Project or
the Product's Base Project and apply existing source permissions. The engine
requires no Product-specific rules or new readiness gates.
