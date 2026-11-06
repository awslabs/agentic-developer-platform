# Acceptance scenarios and verification record

The [issue body](https://github.com/aws-e/adp/issues/7841) owns the single
authoritative AC table. This document defines stable scenarios mapped to those
ACs; it does not introduce a competing acceptance table. Every implementation
scenario below is **planned**, not executed by the design author. The design
checks actually executed are recorded in [coverage](coverage.md).

## Shared deterministic fixtures

Use two synthetic ADP tenants `tenant-a` and `tenant-b`, distinct synthetic domain
UUIDs, independent principals, and separate workspaces/credentials/repositories.
ADP tenant IDs are opaque strings; domain IDs remain UUIDs. Include a legacy
equal-ID case but do not let it be the only positive fixture. Fix the clock at
`2026-10-16T00:00:00Z`, pin digest/version fixtures, compare unordered permission
sets as sets, and use deterministic provider UUIDs with the same first eight
characters but different remaining bits. No account, endpoint, token, provider
secret or scan payload from a real deployment belongs in fixtures or logs.

Separate authentication fixtures from policy expectations. Sign test assertions
using test-only keys through the real request middleware; do not override the very
authorization dependency under test to obtain a positive result. Stub remote
stores with explicit results/errors and retain a call trace. Database integration
tests use isolated schemas/transactions and migrations on a supported PostgreSQL
test database; SQLite-only tests do not establish Postgres grants or constraints.
Independent expected outcomes below are literals and seeded database invariants,
never values computed by the function being tested. Cleanup rolls back fixtures,
restores monkeypatches, and fails the attempt if isolation cannot be restored.

## Pre-merge scenarios

| Scenario / AC / class | Given; When; Then; Controls |
|---|---|
| S-01 / AC-01 / automatic-integration | **Given:** registered tenant-A service with the exact metadata-read capability and separately a service with only internal scope; credentials exist in A and B and one ID is absent. **When:** send evidence and revocation requests naming own/foreign/missing tenant with both existing and absent IDs. **Then:** authorized own-tenant control succeeds; scope-only, missing-tenant and mismatch requests return the route's literal 403 body regardless of existence and make zero credential-helper calls. Authorized revocation for missing/expired records returns `admits_work=false`. **Controls:** fixed clock, independent expected JSON, fixed request IDs or excluded generated correlation value only; two isolated tenants. |
| S-02 / AC-01, AC-02 / automatic-integration | **Given:** distinct same-tenant owners, one valid stored delegation, and a service allowed to read only its own scope. **When:** name another owner, remove delegation, revoke registry capability, inject an unverifiable tools assertion, or make the current registry unavailable. **Then:** name alone never grants evidence; authorized owner/delegation remains a positive; stale/revoked capability is denied before the vault read; store failure is 503 with no facts. **Controls:** store transitions are explicit and sequenced, no cache-dependent sleep; check canonical user ID differs from Cognito subject. |
| S-03 / AC-02 / automatic-integration | **Given:** real `AdpVaultClient` signing path, producer binding A→domain-A, current human membership, `workspace:renew_credential` grant, ordinary and reserved credential records. **When:** request evidence, then change domain/ADP binding, producer registry ID, workspace ownership, named human or grant. **Then:** valid pre-executor API flow succeeds and retains domain UUID response; every wrong binding/removed grant fails; ordinary SQL uses ADP tenant and reserved lookup checks both IDs. No agent run credential is required for this service flow. **Controls:** fake SigV4 transport credentials, real request parsing, private AWS calls stubbed, exact existing producer capability-set assertion. |
| S-04 / AC-02 / automatic-integration | **Given:** verified run/pod/grant and operation/job/attempt/workspace; a separately registered cross-tenant service with specific read scope and stored target delegation. **When:** replay another principal/tenant/attempt, expire or revoke the run, omit either service capability/delegation, or omit cross-tenant target. **Then:** only fully bound own-run and explicitly delegated service controls succeed; both read endpoints deny all incomplete authority; delivery/preflight continue their existing positive and refusal semantics. **Controls:** fixed expiry boundary and operation revision; a successful material-delivery test is not a claim that evidence uses its run authenticator for every caller. |
| S-05 / AC-03 / automatic-integration | **Given:** fully composed Gateway routes and an explicit verifier classification for every mounted internal route. **When:** enumerate routes under supported composition flags and add a test-only tenant route missing authority. **Then:** coverage fails for the new route and for a helper called with only a request tenant; each exempt route has an independently exercised verifier, not a blanket internal-plane exemption. **Controls:** use mounted routes, including generated routers; static AST candidates are a cross-check, not the expected result. No route is silently skipped because imports require configuration. |
| S-06 / AC-04 / automatic-unit | **Given:** synthetic allowed HTTPS metadata/issuer hosts, schema and fake Cognito client. **When:** configure SAML and OIDC; submit invalid scheme, credentials, port, IP, fragment/query, unlisted hostname or type. **Then:** valid settings reach Cognito with no authority attribute mapping; invalid input is rejected at schema boundary before network calls. **Controls:** local allowlist, no DNS/network dependency, explicit expected mappings; new schema validation cannot trust an allowlist supplied in the request. |
| S-07 / AC-05 / automatic-integration | **Given:** two org UUIDs sharing a prefix, one owned legacy resource, one foreign resource and one unbound legacy candidate. **When:** create/read/update/delete through authenticated org administration and retry creation. **Then:** canonical names are distinct; only independently owned providers are read/changed/deleted; ambiguous/foreign resources return uniform conflict and no SDK mutation; safe retries adopt only proven same-org creation. **Controls:** migrated database, literal expected full names, audit of all SDK calls, owner/display-name columns kept distinct. |
| S-08 / AC-05, AC-08 / automatic-integration | **Given:** shared client with native and other-org providers, restrictive attributes and nondefault lifetimes/auth settings; two concurrent config changes. **When:** serialize updates and inject SDK failure before/after provider creation and before DB commit; run dry-run/apply reconciliation twice; exercise additive migration recovery. **Then:** no provider or security setting is lost, mutation is idempotent, unknown ownership is not adopted, failed setup is not enabled, rollback retains secure bindings. **Controls:** deterministic barriers for concurrency, bounded completion (30 seconds), no sleep-based assertion; reconcile observed AWS state instead of assuming SQL rollback undoes it. |
| S-09 / AC-06 / automatic-unit + automatic-integration | **Given:** protected stored selected tenant A and independently varied external/event attributes; client/provider write restrictions. **When:** run native/GitHub/HostedAuth/refresh/password-change and M2M trigger cases; switch to another authorized tenant; remove selection; mismatch subject; fail AdminGetUser. **Then:** ID/access tokens reflect only the protected selection, never the external candidate; unassigned tokens suppress tenant authority; errors cannot return an unchanged privileged event; M2M remains registry-bound. **Controls:** separately stub protected record and event values, explicit expected claim sets; IAM assertions require exact pool-scoped read action. Mocked events do not prove installed Cognito behavior. |
| S-10 / AC-06 / automatic-integration | **Given:** one subject with two legitimate memberships, explicit ADP/domain mappings, grants and a legacy-local proven member. **When:** admit a token selecting A, then foreign selection, stale selection after removal, unavailable identity reader, no local membership or service token used as a human. **Then:** only verified selected membership produces a tenant-bound principal; revoked/unknown fails 403, unavailable 503, invalid token 401; legacy and zero-workspace org-admin positive controls remain usable only with proven membership/grants. **Controls:** same subject and different tenants, fixed membership revisions; reader response tuple mismatch rejected; no fallback when strict policy is absent. |
| S-11 / AC-07 / automatic-unit + automatic-integration | **Given:** resolved tenant-A caller, allowed full repo names, foreign and unknown names, public CVE, empty and nonempty findings. **When:** identify and plan with each name, CVE/package-only input, aliases/basename collisions, empty ACL, unavailable/raising store, and all findings removed by filtering. **Then:** foreign/unknown envelopes are identical after substituting the echoed input name; no repository read occurs before permission; SQL parameters contain the allowed set; no reachability call receives a forbidden repo; denied cases emit audit; allowed existing repository with zero findings stays indexed. Verify sibling regressions also pass. **Controls:** fixed clock/CVE facts, stub query trace and telemetry, compare every response field, not only count or status. |

Each scenario maps to tests using stable test IDs and a source/requirements
revision. Multiple tests may implement one scenario; do not count their linked
ACs as multiple executed results. Capture all retries and distinguish failing,
not-run, blocked and accepted outcomes. Existing selectors such as
`modules/gateway/tests/internal/test_vault_evidence_routes.py` and
`modules/agent-context/tests/unit/test_secure_backend.py` are starting points,
not claims that the new assertions already exist.

## S-12 — AC-08 — manual post-deployment acceptance

**Given:** an explicitly authorized staging target; two run-owned test tenants,
separate repositories and credential references; a test IdP; a native recovery
administrator; deployed image digests, trigger revision, provider/client settings
and schema revision recorded privately. Identity/operations owners first reconcile
existing providers, protected attribute writers and registered callers. Unknown
ownership or missing credentials blocks this scenario, not independent unit tests.

**When:** the evaluator exercises each real consumer (signed Superplane evidence,
verified agent/domain executor, native/federated login and refresh, workspace
switch, identify and plan), including paired own/foreign/absent cases and an
authorized dependency-failure test. Check token organization and API membership
resolution independently, not just the UI. Compare Door responses for matched
foreign/unknown inputs and inspect denial logs without exposing private values.

**Then:** each own-tenant positive succeeds; no foreign credential facts or repo
existence are returned; principal/tenant mismatches are uniformly denied;
federation cannot change the platform-assigned selection; dependency failure is
closed; native recovery login remains available. Audit records correlate actor,
operation and verified tenant without secrets. A successful build/deploy job alone
does not pass any of these assertions.

**Controls:** record the final tested source and requirements revisions, public
test IDs and private evidence references. Set a 15-minute verification deadline
with polling at most every 10 seconds for rollout readiness; readiness requires
observed digest and healthy dependency probes, not elapsed time. Do not require
exact live response latency; compare semantic response shape and absence of
foreign facts. No production tenant fixtures. The evaluator removes run-owned
provider/client additions, users, workspaces, repositories and credentials through
their supported owners and verifies restoration; preserve shared resources.
Failed cleanup or unavailable IdP/target is incomplete acceptance, not a pass.

## Reproducing design checks locally

From the repository root, prerequisites are Python 3, Git, and the reviewed source
commit available locally. No AWS credentials or service startup are required:

```bash
python3 docs/security/tenant-boundaries-7841/audit_inventory.py --check
python3 docs/security/tenant-boundaries-7841/audit_inventory.py --routes --check
python3 scripts/check-public-docs.py
git diff --check
```

The publishing checker scans tracked files, so newly created design files must
be staged before that check includes them. Implementation unit/integration suites
were **not run** during this design-only assignment. The implementer must use each
component's existing CI/environment setup, then select the above new assertions
on the final implementation head. Do not interpret the commands here as a recipe
for live acceptance or a statement that future test names already exist.
