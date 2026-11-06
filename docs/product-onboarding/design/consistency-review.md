# Product onboarding consistency review

The architecture, Profile API/DB design, Context business model, Engineering
Insights lineage and standalone mockup use the same identities and relationships.
This is design and prototype consistency, not evidence of deployed services.
Portfolio administration and the Insights registry adapter still need their owned
implementation contracts; the Profile does not silently implement either one.

## Behavior and ownership map

The [API](api.md), [OpenAPI](openapi.json) and [schemas](schemas.json) define Profile
operations; the [database](database.md) defines their proposed persistence. The
[architecture](architecture.md) and [shared model](data-model.md) define peer ownership.

| User behavior / concept | API and schema | Persistence / authority | Mockup and lineage meaning |
|---|---|---|---|
| Save name, purpose and intended users | `createProduct`, `CreateProduct` → `Profile` | Atomic `products`, `projects`, `product_project_bindings`, revision and receipt | Default owner and first scope; no Portfolio, Initiative or Release prerequisite |
| Edit common Product details | `updateProduct`, `ProductPatch` | `products`; one aggregate concurrency revision | Shared across scopes; clean forms refresh, dirty forms require conflict review |
| Select or manage delivery scopes | `bindProject`, `unbindProject`, `ProjectSelection`; scoped reads | Dated `product_project_bindings`; immutable link versions in `product_changes` | Hide selector for one scope; show it for multiple. Canonical `project_id` remains the delivery/access/evidence scope |
| Choose connections and verify them | `listSources`, `createBinding`, `verifyBinding`, `listBindings`; typed bindings and operations | `product_source_bindings`, `product_operations`; existing connections retain credentials | Native IDs retained. Unverified/expired evidence cannot certify progress or readiness; revoked details are redacted |
| Map a team or add a person | `updateProduct` team mapping / `startHandoff`; existing team/membership APIs | `products.team_id`, existing `teams` and memberships | One Product team inherited by scopes; administration permissions still apply |
| Save a financial plan / optional Initiative reference | `updateStep`, `StepUpdate`, `Financials` | `projects.configuration` with typed allocation/provenance | Proposed funding, editable period; reference an existing Initiative, not a Portfolio. No approval, Contribution or ledger entry is created |
| Review selected suggested fields | `guideOnboarding`, `updateStep` with `StepUpdate.action=confirm` | Session proposals plus atomic configuration/provenance/change journal | Unselected proposals survive resume; manual values are retained unless selected |
| Resume progress or external work | `getOnboarding`, `getOperation`, `Session`, `StepState` | `product_onboarding_sessions`, `product_operations` | Saved checks and current evidence drive section status. Later, N/A, waiting and untouched sections are distinct |
| Create a repository or connect a provider | `startHandoff`, `HandoffRequest`; existing provider APIs | Durable operation/receipt; provider owns resulting native asset | Explicit separate action. Prototype simulates it; completing a Profile never deploys or grants permission |
| Export / CLI / recover a save | `getProfile`, revision and idempotency contract | Frozen change snapshots, operation receipts; local unsaved draft outside server state | Strict Profile export; same IDs in CLI. Offline drafts and uncertain-save receipts do not become duplicate writes |
| Organize Products into Portfolios | Context `portfolio` record and `portfolio_product` relationship; no Profile mutation | Catalog/Context owns identity and dated many-to-many membership | Optional explanation only. No nested collections, access grants, mandatory parent or allocation target |
| Coordinate Initiatives and Contributions | Context business records/relationships; existing authorized references | Portfolio/Context authority, configured tracker for native work | Initiative spans Products, even across Portfolios. Contribution identifies Initiative × Product × Project; no copied backlog |
| Plan and deliver Releases | Release service contract, Context planned/observed relationships | Release registry owns plan; producers own artifact/deployment observations | Target date is not a Release ID. Products/work/releases can associate many-to-many; deployment attempts remain separate |
| Connect Engineering Insights | `listChanges` + authorized `getRevision`; proposed registry/relationship adapter | Profile owns changes; Insights owns ingestion mappings and derived measurements | Six-field references, immutable record/link revisions, effective dates and deduplication; no generic Profile event in raw v1/v2 |

The physical Profile ERD covers its own tables. The broader Context relationship
schema covers peer records; it is not a promise that Profile has tables or CRUD
for every business concept. A current Project has one managing Product in v1.
That configuration/access rule does not constrain many-to-many business attribution
of work, Contributions or Releases.

## Checked behavior

[verify_mockup.py](../mockups/verify_mockup.py) checks actual browser interactions
and validates captured simulated requests/responses against the strict Profile
schemas. It covers first-save limits, optional scope controls, scope isolation,
shared edits, section-state/evidence agreement, expired-source handling, selective
imports, role handoffs, revocation, reload, conflict/receipt recovery and responsive
layouts. [validate_design.py](validate_design.py) checks OpenAPI, examples and
Context/Insights compatibility, including optional many-to-many Portfolio membership.

The [section-state rules](experience.md#section-state-and-freshness) and freshness
overlay are shared design requirements. Browser fixtures demonstrate them locally;
they do not prove real authorization, storage transactions or provider behavior.
The [Insights integration contract](../../engineering-insights/lineage-schema.md#52-profile-and-context-integration-contract)
retains native IDs, same-tenant/same-Project raw references, dated mappings and
deduplication of original work/cost across authorized business views.

## Remaining implementation contracts

- Catalog/Context owners must specify Portfolio/Initiative/Contribution lookup,
  CRUD, pagination, membership mutation and physical storage before those features
  ship. The [Context catalog boundary](../../product-context/README.md) and typed
  examples provide the shared record/relationship contract, not those endpoints.
- Insights must own publisher admission and a versioned registry/relationship
  adapter, with replay, quarantine and cross-scope acceptance tests. Current raw
  v1/v2 schemas do not admit Portfolio or generic registry records.
- Profile implementation still requires migrations, real gateway/UI/CLI wiring,
  live access checks and the independent evaluations in [delivery](delivery.md).
  The mockup uses fictional browser state and makes no service/provider calls.

These are explicit delivery boundaries, not hidden prerequisites for saving the
three-field Product profile. This PR remains a proposal for review.
