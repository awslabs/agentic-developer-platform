# Proposed catalog lookup and write contract (AC-02)

**Design only.** The routes below are proposed gateway operations, not present
endpoints. The [OpenAPI 3.1 contract](catalog-openapi.json) and
[wire schemas](catalog-schemas.json) specify request/response/error shapes;
[storage](catalog-storage.md) owns write invariants. Existing Profile create and
Project access behavior are unchanged. Exact external source revisions, not names,
are identifiers; imported sources remain read-only until their designated
authority explicitly supports writes. Existing tenant membership, current
governed Project grants and source entitlements are checked at request time.

## Routes and scope

All routes are under the proposed `/api/v1/catalog` gateway prefix. A tenant is
derived from the authenticated session, **never** from a request parameter.
`GET /portfolios`, `/initiatives`, `/projects/{project_id}/contributions` and
`/portfolios/{id}/memberships` list only authorized projections; `GET
/portfolios/{id}` and `/initiatives/{id}` return one authorized projection.
An ordinary Project viewer sees a Portfolio only through one of its permitted
Product/Project bindings, an Initiative only through its permitted Contribution,
and only those member edges and work links whose endpoints and source are
currently visible. One cross-project business identity can appear in multiple
Project snapshots but is returned **once** by a catalog list. Explicit catalog
editor/owner roles can manage empty Portfolio and Initiative records without
inheriting access to any member Project; ordinary viewers cannot discover them.

`POST /portfolios` and `/initiatives` create direct catalog records with existing
tenant catalog write rights; `PATCH /portfolios/{id}` and `/initiatives/{id}`
revise them, never rewriting history. Catalog edit rights alone never grant
Project reads. `POST /portfolios/{id}/memberships` requires catalog edit rights,
current Product/Project association rights and readable qualified endpoints;
`PATCH /portfolios/{id}/memberships/{relationship_id}` closes one relationship
with an exclusive end date. `POST /projects/{project_id}/contributions` requires
existing Project write rights, source-authorized Initiative selection and
Product binding; `PATCH /projects/{project_id}/contributions/{id}` revises
objective/dates in that same scope. `DELETE` is intentionally absent: close or
retire, retaining history and receipts. A Portfolio has no nested member,
synthetic Project or financial allocation operation.
For record PATCH, send the complete typed `data` object as a replacement; omit
no required fields. The mutation cannot reassign stable catalog identity.

Create accepts `Idempotency-Key` (1..128 chars); a byte-equivalent retry returns
the prior receipt. PATCH requires `If-Match` with the **exact** record or
relationship revision and an idempotency key. The response includes `id`,
`revision` and `change_id` (201 for new record/edge, 200 for revision); these
are persisted only after the atomic checks in the storage contract. A stale
`If-Match` yields `412 stale_revision`; absent precondition yields `428
precondition_required`; duplicate tuple or conflicting retry yields `409
conflict`. The request's `portfolio_ref` must refer to the path Portfolio and
its authorized revision. Product/Project must still be bound; matching IDs do
not authorize actions. External native IDs in readback retain their qualified
source namespace; clients cannot mint a fabricated external ID by POST.

## Pagination, freshness and errors

Collection `limit` defaults 50, min 1, max 100. Sort by qualified stable
business ID and Project projection key before deduplication; filter denied
records **before** filling pages, counting or generating `next_cursor`.
The cursor is an opaque signed token (max 2048 chars, expires after 15 minutes)
binding actor/session, tenant, the authorized Project-set digest, source
entitlement digest, route/filters/limit, catalog high-water revision and last
qualified sort key. Re-evaluate the current grants and source entitlement on
*every* page, record and mutation. Changed scope or source/expired/mismatched
cursor returns identical `400 invalid_cursor` without echoing hidden IDs. The
high-water mark prevents additions from duplicating or skipping records; changes
to an already selected record are read at that revision only when still
authorized. No total count, all-project count, visible/hidden ratio or partial
claim of coverage is returned. Never use a cached projection to revive revoked
access; current grants win over historical readback and pagination snapshots.

Unauthenticated callers receive 401; nonexistent and foreign/revoked by-ID
resources both receive `404 not_found` with the same public body. A visible
resource with insufficient action rights receives `403 permission_denied`;
neither code includes a foreign identifier or hidden counts. Typed errors use
strict `code`/`message` shapes; malformed input/cursor is 400, semantically
invalid fields/intervals 422, expired historical revision 410. Invalid writes
produce no receipt or event. Existing Project/source ACLs, not owner refs,
Portfolio selection, inferred knowledge, or a catalog cursor, decide access.

Reproduction with [scope fixtures](catalog-scope-examples.json) and
`python3 docs/product-context/check_catalog_scope.py` is an **independent
design check**, not an authorization test against a deployed service.
