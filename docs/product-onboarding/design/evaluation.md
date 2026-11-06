# Independent evaluation and design verification

## What this design can establish

Schema/example checks, source/inventory/link checks and written create/edit/replay/
revoke walkthroughs are **design evidence**. They are not an applied migration,
runtime security test or live onboarding pass. Reviewers decide AC-01–05 using
the artifacts and remaining conditions below. No new acceptance gate replaces
the owner's existing review or the parent AI-DLC state.

| Design criterion / scenario | Artifact and concrete review assertion | Current phase / owner |
|---|---|---|
| AC-01 | [Architecture](architecture.md), [reuse audit](source-audit.md), [coverage](requirements-coverage.md): every EC and PO/PON contribution has one implementation owner; none reassigns Context/L5/Insights into Profile. | Source-backed proposal present; owner design review pending. |
| AC-02 | [Database](database.md), [API](api.md), [schemas](schemas.json): create/edit/retry/revoke trace resolves the same tenant/Product/Project/actor, revision, constraints and error codes. | Static/contract checks; database runtime proof belongs to I1/I3. |
| AC-03 | [Experience](experience.md): all three starting journeys, both resume directions, team/source/financial inputs and model-free use are walkable. | Document walkthrough; actual browser/installed CLI awaits C/U stories. |
| AC-04 | [Delivery](delivery.md), full entry-point/workflow inventories: every proposed deployment unit has package/publish/migration/runtime verification/recovery ownership; each increment has I→D→C/U checkpoints. | Source trigger inspection; deployment access/state unverified. |
| AC-05 | V01–V15 below specify independent oracles, negatives, determinism and cleanup; map all ECs to design/delivery/live ownership. | Evaluation design present; live tests not implemented/run here. |
| SCN-01 | Walk minimal create → replay → edit conflict → revoke against transaction steps and API examples. Reject any request that can commit half a profile or revive stale authorization. | Manual design consistency walkthrough plus executable example validation. |
| SCN-02 | Walk Idea/existing/running paths with two tenants, a shared Product team and a legacy teamless Product visible only to platform admins, two clients and pending administrator handoff. A discoverable input requested twice or overwritten confirmation fails. | Manual design walkthrough; real assertions specified V01/V03/V05/V06/V08. |
| SCN-03 | Trace each I/D/C/U row through workflow→artifact→migration→runtime→independent oracle→cleanup; verify candidate-image migration ordering and separate UI publication. | Source-backed pipeline map; no workflow was dispatched in this assignment. |

## Fixtures, determinism and independent oracles

The release integrator (assigned by #5411) owns a shared fixture manifest with two
tenants, Product A with Projects A1/A2, Product B, owner/member/viewer/revoked users,
two teams, a shared Product with A1/A2 inheriting the same team, a legacy teamless
Product and a verified platform administrator,
private-run results, verified existing repo, owned greenfield
template repo, a read-only running-product environment, document/finance fixtures
and explicit connection references. No real identities/URLs/account/role values
in public files. Fixture owner stores target configuration and receipts privately;
UI and CLI consume the same manifest, not independently invented accounts.

- **Baseline and identity:** pin implementation source SHA, API image digest,
  applied Alembic head, served UI manifest, installed CLI hashes, contract/rule
  versions and fixture revision. Verify exact selected account/role before cloud
  operations; a matching account with another role is not sufficient.
- **Isolation:** unique test-run prefix/UUID namespace per run; leases serialize
  destructive fixture access. Existing customer sources are immutable read-only
  fixtures. No tests require a real production write, Support purchase or subscription.
- **Clock and ordering:** freeze UTC at `2026-10-18T12:00:00Z` for deterministic
  unit/integration tests; live tests record provider/server clocks and use bounded
  windows, not fabricated timestamps. Fixed source revisions, sorted result sets,
  deterministic cursor/page order, fixed model responses only in non-live tests.
- **Failure injection:** deterministic response-loss boundary after provider commit,
  duplicate/reverse callback sequence, revoked generation, HTTP 429/503 and process
  restart. Live fault-control fixture must be authorized/isolated; inability to
  inject the boundary is not a passing test. Mocked boundary proof is labelled.
- **Retry bounds:** at most 10 attempts and bounded wait (scenario-specific ≤5
  minutes for callbacks; provider long-run cases use peer-owned approved timeout).
  Expiry must produce pending/blocked evidence, not time-based automatic success.
- **Independent oracle:** client stdout/DOM is the action surface, never the sole
  oracle. Read current registry via a separately authenticated API observer; compare
  scoped event IDs/versions and provider-native readback. For DB constraints, run
  actual PostgreSQL and a separate transaction. For production-import invariance,
  diff provider/config hashes before/after. For downstream success, retrieve peer
  native result, not merely the gateway's stored receipt or the same mocked function.
- **Cleanup:** fixture owner tracks created IDs/ownership and operation outcomes;
  revoke test access, cancel new work, reconcile uncertain effects, then remove only
  owned resources using the owning service. Unlink/archive are not provider deletion.
  Retain sanitized audit metadata and private receipts. Shared connections, imported
  repos/environment and customer evidence remain intact. Cleanup failure is reported
  with owner/recovery operation and prevents complete evaluation acceptance.

## Shared scenario catalog (proposed selectors)

V01–V15 are design-local labels to carry into real story/catalog stable scenario
IDs. They are not already registered tests. Each row's assertions must be independent
of the implementation algorithm. Unit/integration proofs run before review; the
listed CLI/UI and provider assertions run after the corresponding deployment.

| Scenario | Given / action | Independent expected outcome and plausible failure | EC / implementation and evaluation checkpoints |
|---|---|---|---|
| V01 Minimal Idea | Owner with a current primary or sole team membership and no repo/environment/budget; create by UI, then CLI with identical key/body | Exactly one Product, Project and current binding, owner/tenant and assigned team correct; one semantic revision; provider asset inventory/usage unchanged. Missing or ambiguous team membership returns `409 team_assignment_required` without creating records. Fails if saved state requires assets/model or duplicate IDs appear. | EC-01/05/08; I1→D1→C1/U1 |
| V02 Typed update and race | Two distinct team editors read N; non-owner editor saves name directly, other writes stale purpose; try invalid currency/date/unknown key | No owner-approval step; first write N+1; second 412 and no partial step update; invalid data 422; subsequent authorized read retains confirmed fields. Postgres composite/partial/deferred constraints reject manual invalid insert. | EC-02/06; I1/I4, C1/U1/C4/U4 |
| V03 Tenant and team access | Product A team shares A1/A2; Product B has another team; second tenant, legacy teamless Product and verified platform admin; substitute paths/body/cursor/ref and try adding a Project privacy field | Current team members see Product rows and both bound Projects; current platform admins can see Products only in the selected tenant. Ownership, org-admin role and stale primary-team pointers alone are denied. Teamless Products are admin-only; revoked membership/admin authority removes access. Visibility override rejected. Hidden resources 404 without counts/labels; known-action denial 403; no cache/history/export/operation data leaks; private-run result cannot populate shared Profile/Context/Insights. Include current source credential entitlement, not just tenant equality. | EC-04/05; all C/U checkpoints; #8148 consumer matrix separately |
| V04 Revocation and replayed read | Open UI/cache/history; revoke membership/team/source; retry old operation receipt and callback | Subsequent access denied, protected client data cleared, no cached grant extends authority; callback cannot revive binding; unchanged provider assets. Restore membership alone does not restore revoked source. | EC-04/06/07; C1/U1 core, C3/U3 producer cases |
| V05 Discovery before questions | Existing repo/team/connection and accepted purpose; provider changes branch and pagination is partial | No repeated known-ID/account/region questions; immutable native ID survives rename; accepted purpose survives refresh; conflicting branch marked; incomplete coverage not empty/success. Count contextual prompts and compare fields to provider readback. | EC-03/08/10; C2/U2 |
| V06 Bidirectional resume and admin | UI save → close → installed CLI resume; reverse path; administrator completes credential flow in separate authenticated session | Same session/Product/Project/revision and confirmed values, no duplicate bind/member/repo; no admin secret in member stdout/DOM/history/resume URI; expired handoff named, not accepted. | EC-05/08/09; C2/U2, C3/U3 external handoff |
| V07 Completion and maturity | Idea 2/2, select repo→2/3; verify repo→3/3; disconnect; attempt required N/A and set 100% client field | 100% Saved is not Getting Started; 66% then 100% math matches independent fixture map; revoked repo blocks setup; client score ignored/rejected; mandatory N/A 422; optional N/A explains denominator. | EC-02/10; C2/U2, C4/U4 expanded selections |
| V08 Only selected greenfield writes | Preview approved template/protections; confirm exact plan; drop response after provider create, then reconcile/replay | Provider shows one owned repo, stable native ID, approved visibility/protections; pipeline changes only a reviewable PR; no other repo/role/stack. If native outcome cannot be proved, outcome_unknown and no second create. | EC-03/06/08/09; C3/U3 |
| V09 Unlink/archive/restore | Existing shared sources, pending handoff and owned fixture; unlink/archive, restart and restore | Local binding tombstones/history intact; no surprise deletion/uninstall; new work fenced; restore uses current authority and does not resurrect revoked grant; peer cleanup separately observed. | EC-06/07; all C/U applicable slices |
| V10 No model / downstream outage | Disable model or deny model budget; Context/L5 unavailable; use direct UI/CLI plus guidance | Core save/edit/resume work; guidance shows deterministic next gaps; unknown spend not zero, unavailable Context not a fabricated synthesis; no hidden billable retry. Model proposals cannot set approvals or bypass revision preconditions. | EC-05/07/08/10; C2/U2 and C4/U4 |
| V11 Running-product read-only adoption | Read-only environment/repo/pipeline fixture; inventory; request production write without grant | Before/after source/config/provider hashes identical; as-is baseline and production class visible; unsupported controls explicit; unauthorized write denied. An inventory success cannot be reported as deployed/operational. | EC-03/04/07/08; C3/U3 |
| V12 Callback ordering and restarts | Pending operation; authenticated receipt sequence 2 then 1 then duplicate 2; same sequence changed body; wrong generation/producer/tenant; restart after acknowledgement commit | New event applied once; older ignored; duplicate stable acknowledgement; conflicting payload 409; invalid producer denied, revoked generation never applies. Durable operation survives restart and reporting failure without duplicate change. | EC-04/06/09; pre-merge I2/I3, C3/U3 live callback transport |
| V13 Financial authority | Fixed source document proposing 5000.00 USD and target date; confirm, override, refresh conflicting value, exceed parent or assert approval | Exact decimal/period stored; source/actor/revision/override retained; conflicting refresh cannot replace accepted value; confirmed extraction remains proposed, parent/currency/approval failures block activation. Any actual spend projection independently matches finance ledger and attribution coverage; unknown remains unavailable. | EC-02/05/06/10, PO-10/11; C4/U4; analytics/ledger assertions peer-owned |
| V14 Real peer handoff and result | Available negotiated Context/L5 adapter, scoped sources and approved selected capability; invoke from each client and retrieve native result | Receipt/result echo canonical tenant/Product/Project/profile/source revision and native operation ID; UI/CLI status matches independent peer result. Setup-only/mock response cannot pass; denied/unavailable scope does not block profile. | EC-11; C5/U5, #8149/#8150 actual result owners |
| V15 Change feed/lineage/consumer revocation | Fixed events with duplicates, gaps, colliding native IDs across sources, interrupted consumer ack and revoked Project | Consumer dedupes event identity, detects gap/replays authorized range, cannot merge cross-tenant/native collisions or read DB directly; applicable native run/story/source links retained, no fabricated optional associations. | EC-06/11, PO-13; C5/U5 with #8148/#7295; full release trace #5411 |

## Harness reuse and real entry points

Pre-merge additions belong alongside existing gateway tests:
`modules/gateway/tests/admin/test_hierarchy_postgres.py`, connection locking/revocation
tests under `modules/gateway/tests/admin/connections/`, CLI tests under
`modules/gateway/tests/cli/`, and frontend interaction tests. Proposed Product tests
must hit mounted routes and actual PostgreSQL, not just schema helpers.

Installed CLI evaluations extend `tests/e2e/cli_uplift/` and its preflight/fixture/
cleanup/observer interfaces, selected by `eval-cli-uplift.yml` and the regression
catalog. The harness launches the actual installed `adp product` / `adp onboard`
binary as an isolated client; importing proposed Python helpers is not live CLI
proof. Existing `eval-cli-onboarding.yml` tests broader platform/credential setup;
reuse only its applicable connection fixture procedures, not run an entire platform
installation for each Product test.

Browser evaluations extend the served-browser harness in `tests/e2e/new_ui/` via
`e2e-new-ui-playwright.yml`; existing tests concern UI coexistence, so **new Product
selectors are required**, not assumed present. Use the existing Blueprint navigation, header and organization selector; Products
opens the list/create journey. Use real authenticated navigation to `/products/new` and the Product workspace; independent API/provider observer
checks persisted state. Existing frontend Playwright/Vitest fixtures can supply
pre-merge interaction coverage, but those are explicitly mocked/local evidence.
The browser's asset hash must match Dn receipt and no `page.route` mock may replace
an acceptance API/provider call. Empty test selection, skipped mandatory case,
missing JUnit or missing cleanup receipt fails the evaluation coordinator.

The CLI→UI and UI→CLI resume tests each use both surfaces, but retain separate
action/oracle reports under Cn and Un. One harness result cannot automatically
close both stories. Prior journeys and denial tests recur at each release.

## Evidence and unfinished work

Each result records case/AC/EC, fixture revision, environment reference, installed
and served artifact revisions, actions, independent observations, failure injection,
provider/native operation IDs, cleanup state and owner. Sensitive details remain
private; public issue reports link approved sanitized summaries. The reporting
states are passed / failed / blocked / not-run, plus explicit mocked vs live phase.
Skipped mandatory work is incomplete. Report time-to-first-agent-PR and governed
non-production deployment only when those peer-owned steps actually occur; no
time-to-first-profile substitution.

Not checked in this design run: deployed target/access, actual DB migrations,
provider permissions/entitlements, real browser/CLI/model/provider flows and peer
results. Named service owners/integrator remain to be assigned. These uncertainties
are carried in [delivery](delivery.md), not hidden behind a successful docs check.
