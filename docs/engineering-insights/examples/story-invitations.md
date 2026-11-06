<!-- Fictional authoring example, not a filed issue or an implemented API/test suite.
All selectors below are planned. Names, URLs and catalog bindings are illustrative. -->

## The problem in plain terms

An organization administrator needs to invite a member without allowing an
ordinary member to send invitations or creating duplicates when a request is retried.

**The fix in one line:** Authorize invitations and make repeated submissions safe.

## Description

- **Parent / reports to:** https://github.example.com/team/product/issues/40
- **Delivery owner:** Organizations maintainer (example role).
- **Completion boundary:** Reviewed implementation with deterministic contract
  tests; the release operator owns a separate live rollout story.
- **In scope:** Create-invitation authorization and request deduplication.
- **Out of scope:** Sending email, invitation acceptance and cross-region delivery.

### Lineage

| Field | Value |
|---|---|
| Kind | story |
| Project reference | project-example |
| Parent epic | https://github.example.com/team/product/issues/40 |
| Module IDs | ORG-MOD |
| Feature IDs | ORG-INVITES |

## Impact analysis

Unauthorized creation exposes membership administration. Duplicate requests can
create conflicting invitation records. Each test may create at most one owned
invitation and must delete its own fixture records during teardown.

## Design

The proposed example interface is `POST /example/invitations` with organization,
email and an idempotency key. The contract returns JSON with `status=pending` and
an opaque invitation ID. Only an administrator of that organization may create.
The same actor, organization, key and payload returns the original invitation;
conflicting payload reuse is a separate follow-up requirement, outside this example.

Use a real route-to-service-to-database path for contract integration tests.
Inject the authentication transport, clock and email transport at their production
boundaries; do not replace the authorization or deduplication function being tested.
Fixtures create two distinct organizations, one administrator and one ordinary
member. Run-owned IDs isolate concurrent executions; no pre-existing user is selected.

## Deployment

No live deployment is needed for this story's reviewed-code boundary. Run tests
against a disposable database; roll back fixture changes and assert no owned
invitation remains. The live rollout owner must separately prove deployed behavior.
No credentials or environment identities are stored in this issue.

## Validation

| ID | Setup and action | Expected result / failure condition | Required evidence | Phase and owner |
|---|---|---|---|---|
| AC-01 | Given: administrator of an owned organization and no invitation; When: submit one valid invitation | HTTP 201, pending status, one persisted invitation for that organization and email | Response plus independent database assertion at tested commit | Before review; Organizations maintainer |
| AC-02 | Given: ordinary member and separately an administrator of another organization; When: submit the same request | HTTP 403 and zero new invitation records for each actor | Two parameter cases and database absence assertion | Before review; Organizations maintainer |
| AC-03 | Given: AC-01 request succeeded; When: repeat the identical request with the same key | HTTP 200, original invitation ID and exactly one persisted invitation | Both responses plus row-count assertion | Before review; Organizations maintainer |

### Scenario mapping

| Scenario ID | Acceptance IDs | Case contract | Verification | Test reference | State |
|---|---|---|---|---|---|
| SCN-01 | AC-01 | Given: owned admin/org and empty invite table; When: POST valid input; Then: 201, pending and one matching row; Controls: frozen UTC clock, run-owned IDs, isolated database, teardown absence assertion | automatic-integration | tests/organizations/test_invites.py::test_admin_invite | planned |
| SCN-02 | AC-02 | Given: member or foreign-org admin; When: POST the same input; Then: 403 and zero new rows; Controls: two named parameter cases, isolated organizations, frozen clock, fixture rollback | automatic-integration | tests/organizations/test_invites.py::test_denied_actor | planned |
| SCN-03 | AC-03 | Given: first successful request; When: repeat exact key/payload/actor; Then: 200, identical ID and one row; Controls: sequential calls through real route, independent row query, fixture cleanup | automatic-integration | tests/organizations/test_invites.py::test_identical_retry | planned |

**Execution:** These three selectors are proposed deliverables, not current tests.
Parameter names distinguish the two denied-actor instances. Expected status codes
come from this contract, never from the handler's own constants. Record both
attempts on a test retry. A test returning 201 for either denied actor, inserting
no row on success, or inserting two rows on retry must fail.

**Completion report:** Map each AC to scenario, parameter instance, test/run/attempt,
source and requirements revisions, result and evidence. No live E2E acceptance is
claimed by these deterministic integration tests.
