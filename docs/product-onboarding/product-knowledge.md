# Product Context

Product workspaces have one **Context** tab, headed **Product Context**, with a searchable list and **Add information**. It provides shared product information, evidence, and insights for the team and agents. The standalone **Context** page contains the current user's personal information. Both use the existing registry, import dialog, bulk link-list import, indexing status, and ingestion workers.

Context is the user-facing name of the existing Knowledge capability. Existing `/knowledge` URLs, workspace identifiers, feature flags, API/MCP names, database tables, and module paths remain stable. This naming change does not automatically combine the product profile or all L5 findings into one collection; it preserves the current ingestion, retrieval, and authorization behavior.

## Ownership and access

- Product imports have `product_id` and `tenant_id`, with no `owner_sub` or legacy Knowledge `project_id`. The scope persists across delivery Projects.
- The Product row, workspace and Context are visible only to current members of the assigned ADP team or current platform administrators. Product ownership alone does not grant visibility. Contributors and Product owners can reindex or remove information when they have access. Archived Products remain readable but are read-only.
- Personal imports retain the canonical user's ownership, including public repositories and repositories reached through an organization connection. Source permission does not silently promote an import to shared or tenant ownership.
- Existing records keep their ownership. Previously promoted shared/tenant registrations are not silently migrated into personal or Product ownership. An owner can explicitly import a source into the desired scope; duplicate detection distinguishes personal, each Product, and legacy tenant/shared registrations.
- Product membership authorizes access to imported information. Connected GitHub sources and managed Product S3 storage use the configured ADP service identity after Product authorization. External source admission still applies and does not grant members provider action privileges.
- Organization administrator status alone does not grant Product access. Platform administrator authority is verified from current server state, and all Product queries retain the selected tenant boundary. Personal Knowledge remains private to its owner. Existing explicit tenant imports keep their existing authorization policy.

New Products use the creator's current primary team, or their sole team if there is no primary. If team selection is ambiguous or the creator has no team, creation returns an actionable error. Existing Products without an assigned team are visible only to platform administrators until assigned; existing ownership is not silently converted into team membership. Removing actual team membership revokes access even if a cached primary-team field still names the old team.

The API accepts `scope=product&product_id=<id>` and `search=<text>` for list requests. Create and bulk requests use the same scope and Product ID. Responses include `product_id` and server-computed `can_manage`. Detail, status, delete, and reindex routes recheck current ownership. Removed information is not readable by direct ID.

## Saved GitHub sources and automatic updates

The **Product repositories** panel in Product → Context lists repository bindings
already saved in the product profile's Connections section, across the product's
current projects. It shows the GitHub organization connection, repository, project,
ingestion status, last successful refresh and access errors. **Include in Context**
imports a selected repository. Selecting **Keep updated daily** explicitly enables
the existing scheduled refresh; the default for a newly included source is off.
An existing import of the same repository can be connected by its contributor or
Product owner after its current ingestion finishes, preserving the existing corpus.
If a repository is renamed or its saved source binding changes, **Reconnect source**
authorizes the current selection and retains the existing Context entry.

The selected connection uses the ADP GitHub App installed on the organization.
Knowledge checks the saved installation's tenant ownership and verifies its native
GitHub account and repository IDs. The controller retains the App signing key and
issues workers a short-lived installation token restricted to the selected
repository with `contents:read` and `metadata:read`. This source path uses the same
App for admission and ingestion. Personal connections keep their personal import
behavior. Repository bindings pinned to a particular assessment commit are not
automatically widened into moving Context sources; select a default-branch
repository binding for this feature.

An asset's typed `product_source` column records a product-owned source grant:
the Knowledge service, current source binding/generation, project relationship,
connection, App, installation, immutable repository/account IDs, and the person
who authorized it. User-supplied asset metadata cannot create this grant. The
existing named Kubernetes scheduler authenticates independently of a human login;
existing signed, single-attempt ingestion capabilities authorize worker calls.
The creator is audit attribution, not a user identity impersonated during refresh.
Removing that person's membership does not remove the product's source grant.

Each scheduled refresh and worker operation rechecks the product, project,
relationship, source binding, installation ownership and grant. Archiving, unlinking,
connection revocation or source replacement prevents new use. A blocked source
records an actionable error while other eligible sources continue. Turning updates
off prevents future scheduled refreshes; an already authorized attempt may finish.
Removing the Context entry invalidates its active attempt. Product membership
continues to control access to information already imported; removing a source
connection does not itself delete that information.

The UI and API use these routes (the platform ingress adds `/api`):

| Operation | Route and body |
|---|---|
| List saved repositories and refresh state | `GET /agent-context/products/{product_id}/sources`, with optional `after` cursor |
| Include or connect a saved repository | `POST /agent-context/products/{product_id}/sources/{binding_id}` with `source_ref` from discovery and `keep_updated` |
| Enable or disable refresh | `PATCH /agent-context/assets/{asset_id}/refresh` with `enabled` and the returned configuration `revision` |

The source configuration revision is independent of ingestion progress, so normal
status updates do not invalidate a refresh-setting edit. Concurrent configuration
changes return a conflict and require a reload. The dedicated include route keeps
older Gateways from silently treating the request as an ordinary import.
This configures service-authorized ingestion and refresh; ordinary agent/MCP
query authorization retains its existing human-delegation rules.

## Storage and retrieval

[Product Context storage](product-context-storage.md) defines the optional S3
service-role boundary, per-Product encryption selection, original uploads and
CLI configuration.

Migration `015_product_knowledge` adds nullable Product ownership to the agent-context registry and extends its active-source unique index. Products live in the Gateway database, so current membership is checked there rather than by a cross-database foreign key. A constraint rejects contradictory Product/personal/legacy-project ownership.

New registered Product and personal imports use isolated semantic indexing. Repositories, crawled URL text, and converted authorized S3 documents reuse existing embedding and storage facilities. Structural graph, exact code search, dependency/SBOM, and global repository catalog writes are intentionally excluded for these imports because those stores identify repositories by source name alone. Legacy catalog ingestion remains available through its existing paths.

Vector keys and metadata include the registry asset and ingestion attempt. Door checks the active registry row, current ownership and attempt before releasing results. A removed, failed, queued, or superseded attempt is not readable. A stale worker cannot overwrite or delete the current attempt's keys. Superseded generations can remain physically stored. Searches first filter by current active attempts so obsolete chunks cannot consume the result limit; final registry checks still reject revocations or reindexes that race with a query. Active attempts are bounded at 1,024 per queried ownership scope and sent in batches of at most 100; exceeding the bound refuses the query with an explicit error. Retention cleanup is separate from read authorization.

Product vectors use the existing tenant index with required Product metadata filters. Personal vectors use the existing personal index. All updated readers reject Product vectors from ordinary tenant/shared/personal searches. S3 paths likewise include Product and asset/attempt provenance. S3 source admission accepts the authorized ownership subtree or an explicitly allowed external prefix; a same-tenant Product ID does not permit another Product's stored documents.

## Ordered rollout using existing workflows

Frontend, Gateway, and agent-context deploy independently. Keep `SCOPED_KNOWLEDGE_IMPORTS_ENABLED=false` until all prerequisites below are running. Gateway uses additive `to_jsonb` field reads so personal/legacy registry APIs still work before the new column exists. Product import attempts explain unavailable storage instead of failing with a missing-column SQL error. The frontend rejects responses from older Gateways that do not return the requested ownership scope.

1. Apply agent-context migration `015_product_knowledge` through the existing agent-context deployment path. The migration does not change any existing ownership.
2. Deploy updated Door/vector readers and all ingestion workers and refresh producers. Verify old replicas have drained. New readers must be active before any Product vectors enter tenant indexes; new personal registrations also require the isolated registry ingestion path.
3. Deploy the updated Gateway and frontend using their existing workflows. Their order relative to the migration is tolerant, but personal and Product imports remain disabled until the workers/readers and refresh producers are updated.
4. Set SSM parameter `/adp/<environment>/gateway/scoped-knowledge-imports-enabled` to `true` and rerun the existing Gateway deployment workflow. It renders `SCOPED_KNOWLEDGE_IMPORTS_ENABLED` into the existing ConfigMap. The canonical agent deployment script also accepts the explicit `SCOPED_KNOWLEDGE_IMPORTS_ENABLED=true` environment setting after the same prerequisites; the default is `false`.
5. Verify one personal import and one Product import using an owner and another current member. Check indexing completes, search retrieves the current generation, personal and other-Product results are absent, a nonmember is refused, and removing membership/content blocks subsequent access. Check reindex changes the generation and old hits are refused.

The single flag gates personal and Product registration, bulk imports and reindex. Disabling it stops new owned imports; reads and removal remain available. It is not sufficient to make old unscoped Door binaries safe: never restore such readers while Product vectors remain in tenant indexes. The migration refuses downgrade while any Product records remain, including removed records, because dropping ownership would turn them into tenant records.

No new workflow, storage bucket, per-Product vector index, or manual review step is required. Deployment still follows the canonical platform deployment guide.

For the saved-source panel, apply agent-context migration
`017_product_source_refresh` before the Gateway update, then deploy the frontend
after all Gateway replicas use the new source-grant checks. Reuse the existing
ingestion scheduler and worker images; no worker rebuild or new service deployment
is required. Existing imports retain their behavior until explicitly connected.
The migration refuses to discard retained source grants on downgrade, including
removed entries, so rollback cannot restore implicit source authority. An older
Gateway must not be restored while these grants are in use.

## Validation

The registry tests in `modules/gateway/tests/knowledge/test_product_postgres.py` run actual migrations and route SQL on isolated PostgreSQL 16. They cover old-schema compatibility, NULL uniqueness, the same source in personal and two Products, active membership/revocation, cross-tenant and personal privacy, current Product owner/contributor management, removed IDs, rollout gates, generation dispatch, search and bulk imports. They use the repository's PostgreSQL fixture; `BG_TEST_POSTGRES_URI` can point it to an isolated test server.

Door tests exercise current registration and attempt filtering against PostgreSQL. Ingestion tests check Product/personal provenance, same-source key isolation, source boundaries, semantic-only repository ingestion and URL/document embedding. Frontend unit and browser tests check the Product tab, Project switching, personal separation, imports, management controls and existing card styling.
