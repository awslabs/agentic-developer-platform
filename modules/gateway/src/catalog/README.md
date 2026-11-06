# Portfolio catalog (I18)

This gateway module owns optional Portfolio records and dated, many-to-many
Product membership. Product creation and governed Project bindings stay in the
existing Profile registry. Initiatives and Contributions are optional; this module does not own native work,
provide a catalog UI, support nested Portfolios, or create a financial allocation target.

## Operations and authority

The gateway origin serves `/v1/catalog/portfolios`; public clients use
`/api/v1/catalog/portfolios` because CloudFront removes the leading `/api` before
forwarding to the origin. The collection supports authorized list, create, get, and complete
record replacement (`PATCH`). `/{portfolio_id}/memberships` supports authorized
list and create; `/{portfolio_id}/memberships/{relationship_id}` supports dated
closure (`PATCH`). The membership list accepts `effective_at` and `decision_at`
timestamps for historical readback; `X-Catalog-View` identifies `current` or
`historical` responses. Lists have signed scope-bound cursors, never total counts.

Records use the catalog's direct `adp.catalog` source namespace; Product endpoints
must resolve as `adp.profile` records with a current governed Project binding and
matching revision. An org catalog editor can manage Portfolio records, but cannot
read or mutate a Product outside their current Product/Project permissions.
External source records are not writable through these routes. Edits require an
idempotency key and exact `If-Match` revision; invalid or inaccessible requests
do not create a receipt or change record.

Catalog **writes** require `BG_CATALOG_ENABLED=true` in the serving gateway
configuration; they are off by default. Existing authorized reads remain
available when writes are disabled. Do not enable writes before the approved
target's schema, resolver, source mapping, and client artifacts are confirmed
compatible. This flag is not a substitute for request-time authorization.

## Initiative contributions

Gateway-origin `/v1/catalog/initiatives` supports authorized list, get, create,
and complete replacement (`PATCH`). Project-scoped
`/v1/catalog/projects/{project_id}/contributions` supports list, get, create,
and complete replacement. A Contribution pins the Initiative, Product, governed
Project and explicit Epic/Story/Release source references; it never copies work
or infers that one Product owns another Product's stories. Native work revisions
are pinned opaque source tokens; overlapping links to one native work identity
are rejected even when the source revisions differ. Work links have dated
intervals and immutable correction revisions, with one current tuple enforced
in storage. Historical reads use effective and decision times. Only authorized
Project and configured tracker-source references are returned, and removing a
tracker source hides its links and invalidates list cursors.

Configured tracker-source records are Project scope evidence, **not native work
record verification**. The gateway currently has no provider-native work-item
resolver that proves an arbitrary Epic, Story or Release ID exists or is visible
within that tracker. Without an authorized provider resolver, work-link writes
fail with `503 dependency_unavailable` and reads omit previously linked work;
passing a source check does not establish native-record access. An in-process
test double exercises the proposed verification boundary with synthetic IDs,
not a live provider. Do not enable catalog writes on a protected target on the
strength of these local tests alone.

## Storage and recovery

Gateway migration `114_catalog_portfolios` adds immutable Portfolio and dated
membership versions, separate current pointers, revision-pinned change records,
and idempotency receipts. Additive migration `116_catalog_initiatives` adds
Initiative and Contribution versions and current pointers without replacing
those histories or receipts. Closing a membership appends a correction with its
recorded and effective times. Reopening later uses a new relationship ID;
earlier versions are not edited. Disable new admissions before rolling back an
image; never down-migrate these tables to erase accepted history or receipts.

The in-process gateway tests exercise authorized CRUD, overlapping membership,
dated correction, scope denial, and rollback refusal without deploying a service.
`tests/e2e/product_onboarding/test_i18.py` contains the three local scenario
selectors. Those checks are pre-merge evidence, not protected-target rollout or
live BOLT acceptance; the verification owner handles the latter separately.
