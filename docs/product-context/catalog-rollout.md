# Catalog owner and rollout decision (AC-03)

**Decision: extend the existing gateway, not the separate Knowledge Layer Context
runtime.** This is a contract decision, not an implemented catalog module or an
approved deployment. The **Gateway catalog unit** owns Portfolio, Initiative,
Contribution and membership API/storage in gateway PostgreSQL; the existing
Profile registry remains owner of Products, Projects and their bindings. Build
the unit as a modular router in `modules/gateway/src/` with gateway Alembic
migrations under `modules/gateway/alembic/versions/`. Its tenant/Project resolver
must reuse the *implemented* Profile permissions and source entitlement contract
when available; Profile API design alone does not establish that resolver exists
today. Reject requests rather than falling back to a personal Knowledge Layer
Project, owner name or unverified external namespace. Catalog-to-Profile and
Insights reads cross via authorized versioned references, never direct writes
to another owner's tables. The existing ADP story assignment records the named
agent executor. Independent implementation and pre-merge tests proceed once
their code prerequisites are met; missing live access or bindings do not block
that work. Actual target, runtime Project/catalog identity and fixture bindings
must be resolved and verified before the corresponding live action. Human scope
ownership and the existing human BOLT evidence decision remain unchanged; this
adds no human design, readiness, staffing or release approval gate and assigns
no new privileges. Normal automated review/CI, canonical identity checks,
reviewed-main deployment and existing live prerequisites still apply.

## Source audit and actual deployment path

This decision inspects the repository tree at `ad4d0e96a60896e8d639bee8de27618473bcabdc`
(including its existing workflow files) and the merged Product Profile design
PR #8155. The shared [verification contract](../product-onboarding/delivery/verification-contract.md#current-pipeline-evidence)
records an earlier main audit (`ff122e12295aea61c4d791a91817c580c9457c83`);
its separate Context/Knowledge Layer pipeline **did not establish a Product
Context catalog runtime**. This checkout was initially shallow; fetching the
named main and design revisions confirmed that the earlier gateway workflow
contains the migration path and the design API reserves catalog CRUD for a
separate owner. The current-tree observations below are independently checked
without historical fetches by `python3 docs/product-context/check_catalog_rollout.py`.

| Responsibility | Inspected source and decision |
|---|---|
| API/storage owner | `modules/gateway/src/app.py` registers `UNIT_MODULES` routers; `modules/gateway/alembic/env.py` owns gateway migrations. A new catalog router in this existing image owns the proposed `/api/v1/catalog` routes and additive PostgreSQL tables. These routes/tables do not exist yet. |
| Build and pre-serving migration | `.github/workflows/gateway-ci.yml` checks gateway changes. `.github/workflows/gateway-deploy.yml` reacts to `modules/gateway/src/` and Alembic changes on reviewed main (or exact authorized dispatch); it builds the release image, invokes `modules/gateway/scripts/migrate-before-rollout.py` before serving, and uses a pinned image's `alembic upgrade head`. A separate `run-gateway-migrations.yml` is an existing one-shot mechanism, not a new service. |
| Client assets | `.github/workflows/gateway-frontend-deploy.yml` separately publishes browser changes. CLI delivery rides the gateway image; `.github/workflows/eval-cli-uplift.yml` and `.github/workflows/e2e-new-ui-playwright.yml` are existing evaluation lanes but contain no ready catalog selectors or evidence yet. |

**No new deployable and no deployment child for C01:** the selected Gateway unit
fits its existing build, migration and deploy workflow. If implementation moves
catalog storage/API to a new runtime or needs an uncovered pipeline, that is a
decision change: file and link a real deployment child before declaring I18/I19/I20
implementation ready. Workflow files being present does not prove a protected
target, permissions or a successful release. The PR remains a review checkpoint;
merge and target selection are separate actions.

## Migration, compatibility and recovery

1. At implementation, allocate a new Alembic revision against the **then-current**
   gateway head, not the historical design graph head. Add catalog/version/
   receipt tables, scoped FKs, uniqueness and exclusion constraints, indices
   and explicit namespace mapping. Backfill only from authorized versioned
   sources with recorded effective/decision clocks and source revisions; never
   infer a Product/Project binding from names or an Initiative from a Portfolio.
   Verify row counts and qualified identities through independent readback.
2. Deploy a candidate image with admissions disabled until compatible schema,
   Profile resolver, source adapter and all gateway clients are ready. The
   existing pre-serving migration job must succeed and reported Alembic head
   must match the pinned candidate before enabling writes. An additive schema
   permits the old serving image to keep working while new clients roll out;
   the frontend is separately published and verified at its own digest.
3. Pin source/requirements SHA, workflow definition SHA, migration head, gateway
   image digest, CLI checksum and frontend manifest for each release/evaluation;
   verify that they describe one compatible implementation. On error, disable
   *new* catalog admissions first, stop effects/callbacks, then restore a
   compatible serving image through the existing authorized rollback path.
   Retain immutable dated history and idempotency receipts; no destructive down
   migration or deletion to make rollback look green. Reconcile already
   accepted writes and validate readback before re-enabling. Target approval
   and protected-environment rules remain with their existing authority.

## Live fixtures and evidence owner

V12 (#8273) owns built-and-run acceptance after I18/I19/I20; this design PR
does **not** perform that work. Under an approved target, use the shared
[fixture and recovery controls](../product-onboarding/delivery/verification-contract.md#shared-fixtures-and-controls):
two run-owned tenant scopes; owner, editor, nonmember and revoked actor;
two Projects with overlapping Portfolio memberships, one foreign Project,
two Products sharing one Initiative with distinct Contributions, and a
source with an explicitly captured external ID and revision. Capture the
as-of clock, association revisions and separate surface attempts. Read back
with an independent authorized API/source observation; assert accepted and
rejected mutations, effective-time history, hidden count/foreign denial and
unchanged native work/financial allocations. Poll asynchronous status with
a deadline and maximum cadence from the shared contract; missing evidence is
incomplete, not success. A fixture owner supplies protected target/provider
references (never copied into public docs). Log a durable manifest for each
run-owned resource, clean **only** those resources via the owner, verify absence
by independent readback and keep cleanup failures red. Existing Products,
provider connections, foreign resources, historical receipts and evidence are
never deleted by fixture cleanup.
