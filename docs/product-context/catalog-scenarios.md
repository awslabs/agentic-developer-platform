# Planned catalog contract scenarios

These are **manual case contracts**, not executed surface selectors or evidence of
deployment. [Machine-readable assertions and fixture bindings](catalog-scenarios.json)
anchor the cases; [the shared verification contract](../product-onboarding/delivery/verification-contract.md#shared-fixtures-and-controls)
controls live attempts. #8269 closes at review of this contract; V12 (#8273)
owns building surface selectors, approved rollout, live reports and cleanup.
The catalog endpoints in [the API proposal](catalog-api.md) are not implemented.

For each actual attempt, record the selected target in protected configuration,
source/requirements SHA, gateway workflow/source SHA, image/CLI/UI digests as
applicable, schema head, explicit UTC as-of and recorded-at clocks, authority
source revision, tenant/Project/relationship/mapping revisions, fixture manifest
and actor grants. Compare independent readback from a new authorized API session
and the owning native-work/finance sources rather than trusting a mutation
response or a reused cached view. Keep public records sanitized; report AC →
scenario/test → run/attempt → result/evidence → source/requirements revision,
including failed, blocked, skipped and cleanup-failed outcomes. Poll only a real
operation/status API for at most 120 seconds at a cadence no faster than two
seconds; a timeout is inconclusive. Never provision a service just to review
this design. Clean only run-owned fixtures using a durable manifest; verify
absence independently without deleting shared assets, immutable histories or
receipts.

## SCN-01 — dated catalog records (AC-01)

**Given:** Two run-owned governed Projects in the permitted fixture tenant,
Checkout and Accounts Products, Commerce and Shared services Portfolios and a
shared Initiative. Pin effective UTC time `2026-10-20T12:00:00Z` for the
synthetic design fixtures; choose and record an explicit clock at live execution.
The second run-owned tenant is a negative isolation control, never a substitute
authorization source. Catalog creation is optional: Profile's three-field
Product save is unchanged.

**When:** Create Commerce→Checkout and Commerce→Accounts, then Shared→Accounts;
create the Initiative×Checkout×Project commitment. Independently read current
relationships, close Commerce→Checkout with an appended version, then read at
the earlier effective/recorded time and after the close. Try nested Portfolio,
duplicate pair via a different Project projection, duplicate current commitment,
stale supersedes revision and empty/negative interval writes.

**Then:** The readback contains two members of Commerce and two Portfolio
memberships for Accounts without counting an extra Project projection as another
membership. The unique current commitment belongs to one Product and its
governed Project. The historical read shows original effective membership and
both recorded revisions; the post-close read does not show a current link.
Each prohibited write returns the proposed typed rejection without a receipt,
new change, new native backlog item or Portfolio financial allocation target.
An independent owner/source query checks those prohibited effects. Attach
`catalog_contract_examples`, an attempt-aware observation and cleanup report.

The local `python3 docs/product-context/check_catalog_scenarios.py` checks
the fixture wiring and controls; it does **not** execute these HTTP cases.

## SCN-02 — scoped lookup and mutations (AC-02)

**Given:** An authorized actor has two governed Projects with overlapping
Portfolio projections. A second actor lacks the foreign Project grant; retain
one unknown ID for comparison and a separate run-owned tenant for isolation.
Use run-owned Product/Project memberships and
source connection revisions from the fixture manifest. A separate revoked
actor starts with access and loses its Project or source entitlement before
the next request. Pin source and grant-set revisions, actor identity and the
UTC clock; no Portfolio membership itself grants access.

**When:** List Portfolios with `limit=1`, follow the returned opaque cursor,
list Commerce memberships and attempt a permitted membership write. Independently
read back the authorized projections. Then revoke one Project, retry the old
cursor and a Project-scoped write. Attempt a revoked-source read, expired cursor,
foreign and nonexistent by-ID reads, foreign member write, action without
catalog edit rights, stale `If-Match` and a visible write without Project rights.
Use an independently authorized identity/Project/source query as the oracle;
do not derive expected grants from the catalog response under test.

**Then:** Commerce appears once across the two Project projections. Page two
contains Shared without repeating Commerce. Only permitted member edges appear;
the page has no total or hidden count. Revoked/expired cursors fail before
serving cached data. Foreign and unknown IDs receive the same 404 body; source
revocation hides even previously visible records. The permitted write has one
receipt and no native work copy. Foreign/revoked/stale/insufficient-rights writes
have their specified 404/412/403 errors and no receipt, change or grant;
neither catalog ownership nor a member relationship authorizes spending or
work access. Capture `catalog_scope_contract`, independent per-actor
observations, source revision and separate attempt/cleanup reports.

## SCN-03 — delivery and recovery (AC-03)

**Given:** The contract chooses a Gateway-owned catalog API and database, not
the separate Knowledge Layer Context runtime. Use the pinned repository source
`ad4d0e96a60896e8d639bee8de27618473bcabdc` for the workflow audit;
the accepted Profile design revision is referenced in the
[rollout decision](catalog-rollout.md). This is a *pre-review source audit*,
not proof that a gateway catalog route, schema, live target or fixture exists.
The later V12 executor must verify an authorized target and capture the actual
source/workflow revisions and artifact identities before any live evaluation.

**When:** Independently inspect the modular gateway router, PostgreSQL/Alembic
owner, gateway CI, pre-serving migration and actual gateway deploy path and the
separate frontend/CLI evaluation lanes. After dependent implementation and the
required target approval, pin the candidate image and workflow, observe the
migration's reported schema head *before* enabling catalog writes, then use a
real served browser and/or installed CLI case with independent API/source
readback. Capture one recovery attempt: disable new admissions first, verify
the compatible rollback and reconcile accepted writes without deleting their
history. Clean only resources owned by that attempt via a durable manifest.

**Then:** The observed paths support one Gateway deployment unit; no new service
or pipeline is silently claimed. The deployment receipt, reviewed source SHA,
workflow SHA, gateway image digest, schema head and any served UI manifest or
installed CLI checksum agree for a compatible release. Failure to match these
pins or a failed migration prevents a functional pass and leaves admissions
off. Previously accepted relationship versions and idempotency receipts remain
readable under current authorization after recovery; independent readback
confirms absence only for run-owned fixtures and retention of reused assets.
Capture `catalog_rollout_decision` with the source/requirements revision,
independent repository observation and local attempt report *now*; V12 later
records a **separate** deployed attempt per surface and cleanup receipt. A
target or evidence not yet available is `not-run`, not passed or nonexistent.
