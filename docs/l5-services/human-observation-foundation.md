# Bounded human observations for L5 release evidence

The gateway can record what a selected signed-in human saw in a Product view.
These records provide attributable partial evidence. They do not complete a
release check, qualify a native provider, approve a flow, or replace machine
collectors. Every response and observation retains
`release_check_completed: false` and `native_qualified: false`.

## API

All paths are relative to the gateway's operator API:

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/orchestration/flows/{flow_id}/l5-observations/prepare` | Prepare one bounded observation. |
| GET | `/orchestration/flows/{flow_id}/l5-observations/{challenge_id}` | Revalidate and read the observation and sanitized launch progress. |
| POST | `/orchestration/flows/{flow_id}/l5-observations/{challenge_id}/endorse` | Record the actual Product owner's consent to the exact snapshot. |
| POST | `/orchestration/flows/{flow_id}/l5-observations/{challenge_id}/observe` | Record the selected human's displayed facts or mismatch. |

These routes require an access JWT for a current human workspace member,
`PLAN_APPROVE`, and a successful logout-revocation check. Preparation additionally
requires platform administrator authority. `PLAN_APPROVE` here means current
human workspace membership; designated ordinary Product members can read,
endorse, and observe. The preparer must remain the original release policy owner
and have current Product team access. The actual Product/configuration owner is
derived from server records and must retain their own Product authority.
Service/IAM actors and caller-supplied identity claims cannot record observations.

Preparation supplies an explicit `request_key` and selects the node, expected plan version/hash/attempt, confirmed
delivery dispatch action, Product/project/service/binding, original required check,
observer, subject, optional exact result, and reason. The strict request model
is `PrepareObservation` in `l5_observation_contract.py`. A request key replays the
same preparation; reusing it for different input is refused. After expiry, use a
new request key to prepare a fresh challenge without changing its reason or scope.

When the actual Product owner differs from the preparer, that owner must read
and endorse the exact challenge snapshot through their own human session before
any observation or selected launch. The endorsement is append-only, expires no
later than its owner's session or challenge, and grants no new Product access or
provider credentials. Existing native session/delegation checks still apply.
Owner endorsement is consent to collect evidence, not evidence that the owner
performed any UI or SSO action.

The server reconstructs the accepted contract, current execution claim, verified
running workflow identity, effective release revisions, current Product and
team visibility, relationship/configuration/binding revisions, and selected result.
It also verifies the locally installed L5 wheel/schema identity. Full deployed
gateway/frontend/worker identity remains independent machine evidence.

The workflow action must be a confirmed `delivery_workflow_dispatch` belonging
to the current assignment. Its `release_job` run ID and attempt must already have
been pinned by the gateway's existing GitHub OIDC job authorization. A fresh
provider read verifies that exact run, installed App bot, immutable repository,
dispatch revision, workflow path, and correlation. Collection requires
`in_progress` with no conclusion. A caller cannot provide a substitute run ID.
This allows preparation during qualification, before success evidence exists;
the successful release receipt and acceptance rules remain unchanged.

The snapshot's `release` section has provenance
`oidc-authorized-delivery-dispatch-and-provider-run`, dispatch action and execution
IDs, cycle/plan version, source/definition/dispatch revisions, run/attempt,
correlation, contract digest, repository identity, claim generation, and a stable
`dispatch_context_sha256`. It is running dispatch provenance, not a successful
`CorrelatedWorkflowReceipt` or a completed release check. Every live read,
endorsement, observation, and pre-provider launch rechecks the current scope.

Supported subjects are configuration display and result display for Security,
DevOps, FinOps, Well-Architected, and Transform. DevOps additionally supports
native UI display and native access denial. A check must occur in the original
accepted delivery contract; the endpoint's allowlist does not add checks.

Challenges expire at the earliest of 30 minutes, the preparer's session expiry,
policy expiry, and current claim lease expiry. Paused/terminal flows, expired or
released claims, and changed assignments cannot collect evidence. The selected human records the
snapshot hash and only the displayed revision, result identity, or native
hostname appropriate to that subject. Arbitrary evidence URLs, pass flags,
source revisions, and actor IDs are rejected. Repeating the same recorded body
is idempotent; changing an existing observation is refused.

Records use the existing append-only `OrchestrationDecision` log. They do not
alter flow state, attempts, retry caps, release checks, browser policy, or native
resources. A selected negative-access observer receives no foreign Product
snapshot.

An agent may help prepare a challenge or administer explicitly authorized fixture
state. It must not submit displayed/denied human observations using fixture
credentials. Actual signed-in humans must perform and record UI/SSO observations
through their own sessions. Neither fixture setup nor owner consent substitutes
for that human provenance.

## DevOps launch receipts

The existing browser-launch route accepts the optional
`X-ADP-Observation-ID` header. This binds a manually requested launch to the exact
human, target binding, and configuration revision in a prepared challenge. No
automation is started by preparing or reading an observation.

The server commits `prepared` before entering the launch path and commits
`provider_started` after fresh scope validation immediately before requesting a
provider session. It then records `issued`, `denied`, or `unknown`. These separate
transactions survive rollback of the original HTTP request. A challenge permits
at most one launch, including lost-response retries. Read the `launch_attempt`
receipts to investigate an uncertain outcome instead of repeating federation.

A denial can report `provider_session_calls: 0` only before the provider marker.
After that boundary the count is unknown, never an invented zero. Denials retain
a bounded error code and HTTP status. Receipts retain no federation URL, token,
ticket, or credential. A human's native display/denial observation must reference
the exact server-owned receipt chain; reads revalidate that chain.

## Integration still required

Static display evidence does not establish configuration changes, check/replay
effects, successful native jobs, independent artifacts, or full UI/CLI/MCP parity.
Each original release criterion still requires its own complete evidence.

The release publisher can retrieve the exact human facts through
`POST /orchestration/operations-release-job/l5-observations/export`. It authenticates
with its current GitHub OIDC identity using the existing release-job audience.
The request contains the existing `NativeJobRequest` in `job`, a `challenge_id`,
the expected `snapshot_sha256`, and an exact `selection`:

```json
{
  "product_id": "selected-product",
  "project_id": "selected-project",
  "service_id": "transform",
  "binding_id": "selected-binding",
  "configuration_id": "selected-configuration",
  "configuration_revision": 1,
  "check_id": "ui-cli-mcp-parity",
  "subject": "result",
  "result_id": "selected-result",
  "result_revision": 1,
  "result_run_id": "selected-run"
}
```

The three result fields are null for other subjects. These fields are expectations
to compare, not grants of access. The publisher must still hold the exact active
release assignment and claim. The server revalidates current Product ownership
and team/workspace access, verifies historical collection and endorsement times,
and refuses another run, source or contract. An expired human collection window
does not invalidate an observation already recorded inside that window; it does
not authenticate the old human session either.

The response uses `l5-human-observation-export/v1`, includes immutable source
decision references and an append-only export receipt, and retains both partial
flags as false. It contains no raw configuration settings or provider credentials.
`qualification_human_observations.collect_into(journal, selectors)` performs the
current OIDC call, checks the exact release and selected fixture, and appends
`human-observation-export` records to the driver's existing private journal.
Fixtures cannot supply a response or a pass declaration in place of that call.

Copied response JSON, screenshots, and browser state must not be treated as
verified release success. The complete release integration must:

1. Use the protected export consumer to derive release authority without
   accepting a human identity from the caller.
2. Compare the stored observation against the exact source/definition,
   workflow run and attempt, correlation, original delivery contract digest,
   Product scope, fixture, result, and check selected by the driver.
3. Return the verified partial observation and immutable decision references,
   keeping native facts and machine evidence separate. Preserve actual human
   observation provenance; fixture credentials and owner endorsements cannot
   supply displayed/denied observations.
4. Publish the complete result through the existing OIDC evidence publisher
   only after every original criterion is satisfied.

The live challenge GET intentionally refuses expired challenges, changed
snapshots, and released claims. It is not an archival export API. Archival
verification must separately authenticate the current reader and prove that
preparation, owner endorsement, launch markers, and observation were valid at
their recorded times under the original authority and accepted contract. It
must not demand an active historical claim after execution has concluded or
treat today's authority as proof of historical validity. Expired human sessions
cannot be reused to authenticate the current reader.

The active publisher exports before its workflow concludes. A later machine
archival verifier must use its own current purpose and storage authority to
validate the existing immutable OIDC publisher records and exact export hashes;
it cannot mint a fresh OIDC token for a completed historical run or reuse its
acceptance in another run. No new signing service or key is introduced here.


The export retains both the original accepted `release.definition_revision` and
its actual `release.dispatch_revision`. A repaired main dispatch can have a
newer SHA while the accepted workflow definition remains unchanged. The Gateway
verifies the original definition against the accepted contract; the driver
compares `dispatch_revision` with its actual `GITHUB_WORKFLOW_SHA`. The existing
release journal calls that actual dispatch pin `definition_revision`; it must
not overwrite or be equated to the original accepted pin in the exported facts.

## Canonical request effects and current-run discovery

An optional `X-ADP-Observation-Effect: <challenge-id>` header records a canonical
configuration update, readiness check, or replay of an **existing** submission
receipt. The original `ui-cli-mcp-parity` check must be accepted. The selected
observer's current authenticated session and ordinary service permissions still
apply. The effect has `authenticated-canonical-request-effect` provenance; it
never establishes that a human operated or saw a UI.

Use a configuration challenge for `save`/`check`, or a result challenge for
`replay`. Updates require an existing configuration; replay requires the exact
selected result's run, original input, and original idempotency key. Replay cannot
capture sources, create a new run, or republish existing work. Header-free calls
retain their normal behavior. The installed CLI accepts `--observation-effect`
on `service check`, `service config update`, and `service run`. The installed MCP
HTTP endpoint accepts the header for those same canonical operations.

A separately committed `before` event precedes the operation. A second committed
`after` event retains an independent configuration readback, sanitized HTTP
status/error, response hash, and operation-specific identity. No settings,
credential, or raw error body is copied into the effect. A `before` event alone
means **unknown outcome**. HTTP errors retain `returned_error` and the actual
after-state; a failed handoff may follow a committed configuration change. Neither
case permits blind retry. A successful response is `returned`, not release
acceptance. Configuration display after an update requires a fresh human
observation of the new revision; the original snapshot equality is unchanged.

`GET /orchestration/flows/{flow_id}/l5-observations/{challenge_id}/effects`
lets an authorized current human inspect historical effect facts after its
collection window or configuration revision has changed. It validates the
original accepted scope, collection-time endorsement and immutable versions,
separately from the reader's current Product and policy authority.

The current GitHub workflow can export its own exact effect with
`POST /orchestration/operations-release-job/l5-observations/effects/export`.
Its strict body is the human export body plus `operation` (`save`, `check`, or
`replay`) and `request_sha256`. The digest covers the canonical object:

- `save`: `settings`, `idempotency_key`, `if_none_match`, and `if_match`.
- `check`: an empty object.
- `replay`: `input` and `idempotency_key`.

The `l5-request-effect-export/v1` response contains `effect`, `export_receipt`, and
both partial flags. `qualification_request_effects.collect_into` records a fresh
OIDC-authenticated export in the release journal without sealing any criterion.

A frozen fixture must not contain future current-run challenge IDs. Instead,
`POST /orchestration/operations-release-job/l5-observations/discover` accepts
`job`, exact runtime-readback `selection`, stable `preparer_id` and `observer_id`,
and `evidence` (`human` or `effect`). Effect discovery also requires `operation`
and `request_sha256`. It searches only the caller's exact dispatch/run/attempt
and original accepted check. It returns a unique completed selector or `pending`
with current matching prepared challenge IDs. Duplicate completed observations
are ambiguous, never resolved by choosing the newest. Old-run evidence is never
reused.

The source-owned `qualification_observation_wait.wait_for_preparation` waits for
one real owner-prepared effect challenge before a canonical operation. It refuses
an already attempted effect, including an unknown result. The caller then passes
the returned ID to the installed CLI/MCP/canonical operation. The companion
`collect_by_scope` waits for human or effect evidence and invokes the protected
export, journaling the wait and timeout. Both waits are bounded; neither rewrites
the fixture, operates a UI, submits a human observation, or repeats native work.

To relate an update to what a human saw, keep the saved effect's
`returned_configuration` (`configuration_id`, `revision`, `sha256`) and original
release identity. Prepare a new configuration challenge after the update, and
require its snapshot revision/hash to match that returned state. Only that fresh
challenge may receive the human `displayed` observation. Its separate export must
name the same configuration revision and exact release. Recording the saved
revision against the old pre-update challenge is rejected.

This shared path is intentionally scoped to the delivery flow's tenant. It does
not authorize a second tenant's Product or export that tenant's observations.
Security's original two-tenant qualification therefore requires a separate,
explicitly authorized cross-tenant evidence path before its full UI acceptance
can complete. The chat sandbox qualification policy is not L5 Product authority.
