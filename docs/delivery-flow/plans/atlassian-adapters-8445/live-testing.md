# Live testing after the coordinated adapter release

This is the required #8454 protocol for [the delivery flow](README.md). It is a
**design contract**, not a report of tests already implemented or run. Preserve
all epic/story AC and scenario IDs from [story-mapping.json](story-mapping.json).
Do not generate replacement catalog IDs; qualify local scenario IDs by issue.

This is the **provider live acceptance** contract. It is not a demand for a
pre-existing dataset or a development-stage gate. The [test-data strategy](test-data-strategy.md)
supersedes the earlier full-fixture preflight: stages 1–7 use generated controlled
data, while live checks discover existing access and retain explicit pending
coverage. Agents own data creation/cleanup where the existing grant permits it.

## Build the test path before release

Stage `w08` implements the proposed `tests/e2e/tools/atlassian_integration.py`, real
installed-client drivers, independent provider observers, fixture ledger and
report reader. Extend the existing runner, `tools-live-evaluation.yml`, Tools CI
and existing private evidence publication; no new standalone workflow.

Run independent eligible checks concurrently inside that protected workflow.
Choose the runnable set from discovered access, available runners and provider
limits; no fixed pool of accounts/projects is assumed. Give each client its own
owned issue, browser/config home and durable operation ledger. Reads on stable
resources may overlap; mutations and cleanup on one fixture remain ordered.
Revocation, expiry experiments that alter a shared grant, adapter outage, route
handoff and rollback acquire the shared exclusive lease and wait for ordinary
checks to finish. Durable pending work survives worker loss or a queued workflow
being superseded; cancellation is never a passed check. Preserve every required
case and join complete compatible evidence at acceptance. #8454 implements this
scheduler; the flow's three-action limit is separate from workflow test fan-out.

The reviewed suite adds an explicit full-epic selector and stage/resume support;
these are deliverables, not currently supported command-line options. A default
legacy filter, single diagnostic cell, direct API call or mocked client cannot
select the complete epic. Tests of the runner/reader must reject missing/duplicate
cells, AC-05/06 omission, skipped-as-pass conversion, stale digests, mismatched
clients and a forged all-green summary. Expected cases come from the accepted
inventory, never from whatever the new service happens to advertise.

#8453 must test the real release-controller-to-workflow handoff, exact correlation,
lost dispatch acknowledgement and controller restart. #8454 must test return
publication, private evidence retrieval and cleanup recovery. Mocks establish
these local contracts only; the deployed route and provider checks below still run.

## Fixture preparation and preflight

The release owner builds a private capability inventory from actual discovery.
Start with unknown availability, not presumed test accounts or a completed fixture
manifest. Keep target/config references private and distinguish code-verified
capability, delegated authority, reachable resources and usable provider grants.

| Discover | Automate using existing authority | If unavailable |
|---|---|---|
| Dev target, reviewed release and rollback artifacts | Read account/connection/region, component digests, routes and workflow authority. | Hold the affected deployment or handoff; do not guess an ambient target. |
| Atlassian identity and accessible sites | Verify an existing consenting binding; discover allowed scopes/sites. | Continue controlled development. Record the real consent/policy action once when activation needs it. |
| Jira data | Discover an existing permitted project and legal fields/transitions; create one owned issue per client and persist its ID before subsequent mutations. | Keep corresponding live writes pending. Do not request a new project or hand-prepared issues. |
| Confluence data | Snapshot a permitted existing page or create one disposable page if creation/cleanup is already allowed. | Keep Confluence live reads pending; generated page tests continue. |
| Additional user/site contrasts | Use existing identities if available. Generated identities cover controlled ADP enforcement. | Record the real cross-account/site clauses as pending; do not require account creation for routine development. |
| GitHub/Slack/legacy Jira | Capture the existing verb baseline; use accessible repositories/channels/sites and agent-created owned data where permitted. | Keep provider-specific live cases pending, preserving the full expected inventory. |
| Client/model and readback access | Verify installed versions, actual ADP/model access, and an independent reader using existing authorized reads where sufficient. | Do not label config-only tests or an unavailable observer as client/provider proof. |
| Cleanup | Establish durable ownership records and working cleanup before live writes. | Do not start that mutation; continue reads and independent work. |

No second provider observer account or administrator token is required by default.
The observation must be independent of the tool response and supported by the
available authority. Use provider IDs/state/version readback and ADP egress/receipt
evidence; do not invent visibility into provider-global call counters. If available
access cannot establish an assertion, that live clause stays unproven.

Test data is created by the workflow, never a manual preparation assignment.
Generated transport data, seeded ADP identities and simulated expiry are labeled
controlled evidence. They cannot satisfy actual OAuth consent/managed refresh,
real provider effects, installed-client invocation or live cross-account claims.
Provider accounts still consent themselves; an ADP test user is not such a grant.

Run eligible checks without waiting for a complete dataset. Record each unavailable
capability once in the durable pending ledger; do not repeatedly prompt or spend
repair attempts on unchanged missing access. Full live acceptance still requires
the complete original inventory. Only affected live checks and activation/handoff
wait; independent implementation and gated deployment proceed.

Obtain fresh actual runtime evidence before and after a live batch and reject a
changed deployment. A secret's presence or a `connected` label is insufficient.
Keep test-client execution free of AWS credentials/CLI; infrastructure observation
uses its own scoped role. Preserve reusable fixtures and remove only run-owned data.

## Ordered deployed checks

| Phase / case groups | Stimulus through the real path | Independent pass condition |
|---|---|---|
| Release identity: `release-six-components`, `adapter-four-routes` | Read Kubernetes workloads/pod digests, served frontend/CLI, migrations and recovery consumers; inspect private edge/ALB/AgentCore destinations and make correlated calls to all four connector paths. | All six component receipts match the selected release; actual vendor calls execute in the adapter, with correct role/network/contract/group generation. A response that could come from core is insufficient. |
| User journey: `consent-two-users-sites` | Install/login/setup/launch through ADP; follow Connections consent/return/site selection for both users. | One Atlassian card and one managed MCP entry per client; recorded initiating ADP owner equals provider identity/site, reload does not replay consent, ordinary users need no app creation/PAT/manual MCP JSON. Conditional admin steps are recorded. |
| New tool cells | In **each** installed client invoke all four named reads and four writes, using separate fixtures/operation IDs and exact approval payloads. | Sixteen exact required cells; reads match known IDs/content/versions and expected query membership; each mutation has exactly one independently verified effect and matching receipt. A successful raw MCP probe is additional evidence, not client proof. |
| Existing tools: `legacy-all-verbs`, `legacy-8076`, `legacy-7266` | Invoke the entire independently captured GitHub/Slack/legacy-Jira verb inventory through the adapter. Run preserved CLI/Claude/Codex legacy scenarios. | Names, schemas, grants, errors and allowed effects match the accepted baseline; no implicit reconsent, privilege growth or omitted legacy criteria. Baseline inventory is pinned before cutover, not inferred from the candidate's tools/list. |
| Denials: `role-site-owner-denials` | Wrong owner/tenant/site, missing scope, revoked connection, read-only write, denied approval, changed approved payload, forged assertion/header, wrong role/purpose/contract and direct pod/ALB calls. | Expected denial and zero unauthorized provider data/effects, proven by independent counters/state. Distinguish denied calls from valid empty search results. |
| Identity: `identity-expiry-refresh`, `disconnect-revoke` | Retrieve after actual supported provider expiry, concurrently during managed refresh, after adapter restart, and after disconnect/revoke. | Identity returns usable same-owner access without reconsent while valid; disconnect denies new calls and remote revocation/known expiry is proved. No ADP token persistence or direct OAuth fallback. |
| Clients/catalogue: `catalogue-next-session`, `claude-schema-loading`, `codex-schema-loading` | Change the authorized server catalogue, relaunch without setup, exercise expired ADP login and profile/config collision handling; measure definitions at the model-facing boundary. | Next-session refresh; independent successful named calls and measured schema count/UTF-8 bytes for each client. Native deferral needs actual protocol proof; eager fallback stays within the canonical 128 KiB bound. |
| Isolation: `core-health-through-outage`, `unrelated-runtimes` | Stop/replace only owned adapter pods; probe core login/model APIs and the existing unrelated tool runtimes with dedicated fixtures. | Core remains available; tool errors are bounded/truthful and no automatic alternate backend executes writes. Door/browser/interpreter/search behavior stays compatible. |
| Release faults: `partial-routing-apply`, `controller-restart-stale-lease`, `group-rollback-redeploy` | Interrupt an owned route apply, restart release controller, submit a stale generation, roll the four-connector group back, then redeploy the candidate. | Partial state stays fenced; exact prior manifest restored/read back before reopen; stale controller denied; grant/receipt recovery retained. Initial rollback disables new MCP rather than mapping it to legacy Jira. Final state is the verified candidate, not the rollback baseline. |
| Effects/recovery: `unknown-write-no-replay`, `report-recovery` | Commit one owned mutation upstream then suppress its ADP reply; repeat the same operation ID, change its arguments, restart adapter/facade reconciler, and fail first reporting repair. | Exactly one effect, changed-argument conflict, original backend generation, retained unknown status until conclusive readback and successful durable reporting. No blind retry after 401/429/timeout or rollout. |
| Runner/cleanup: `runner-loss-resume`, `fixture-cleanup` | Interrupt the owned runner after an effect and during cleanup; restart the recovery consumer and resume with the original qualification/fixture ledger. | Completed effects are not repeated; run-owned data/grants/resources are removed/restored with provider absence/restoration proof. Unrelated markers and reusable baseline resources remain unchanged. |

New tool cells are:
`atlassian___search_jira`, `get_jira_issue`, `search_confluence`,
`get_confluence_page`, `create_jira_issue`, `update_jira_issue`,
`comment_jira_issue`, and `transition_jira_issue`, each with the `atlassian___`
prefix in the actual catalogue and each independently in Claude and Codex.
The JSON mapping contains the exact full names. Assert semantic tool selection,
arguments, result/side effects and receipts; do not compare model prose bytewise.

Before new writes, the full release/read/denial checks must pass. Write opt-in
and existing per-operation approvals still apply. The preaccepted fixture plan
may authorize exact bounded test mutations where the platform supports it;
it cannot supply a blanket approval or fake the approval service's records.
Use unique run-owned resources for each client. A title match or eventually
consistent empty search is not proof that an uncertain create did not happen.

## Long-lived checks, faults and cleanup

The current workflow has a 60-minute job ceiling. Keep each batch bounded within
it; managed token expiry may take longer. Record provider expiry/clock evidence,
schedule a due follow-up with the same qualification ID and ledger, then resume
without treating a sleeping CI job as the lifecycle mechanism. The new stage/run
inputs and scheduled continuation are #8453/#8454 deliverables. Final acceptance
waits for real refresh/revocation evidence; a mock or revoked token is not a
substitute for an expiry refresh.

A failed run cannot retry until uncertain effects are reconciled. Safe reads
retain the existing bounded retry policy. Each resumed write keeps its original
operation ID and payload; confirmed effects are read back, never resubmitted.
Code changes require a newly verified release and fresh affected-test evidence,
with a retained link to previous failed attempts. Do not combine passes from
incompatible releases into one all-green result.

Run fault injection only against owned ADP resources/fixtures. Automatic handoff
and rollback can return bounded retryable tool unavailability; no scheduled
maintenance window or zero-interruption claim is introduced. Keep the prior
artifacts for rollback/reconciliation without admitting new writes to a retired
generation.

Use workflow `always()` cleanup for normal failure **and** a durable due-cleanup
record consumed after runner termination. #8454 owns the run-owned cleanup
handlers; #8453 installs/verifies their supported schedule and permissions.
An existing facade reporting reconciler does not establish provider-fixture
cleanup by itself. Do not invent completion when the required consumer is absent.
Cleanup assertions require provider absence/restored state, grant revocation or
known expiry, empty owned-resource inventory and unchanged unrelated sentinels.
Only delete run-owned registrations/accounts; shared deployment clients remain.
Unknown cleanup is an incomplete test and survives the flow's runner lifetime.

## Evidence contract and completion

Proposed report `atlassian-flow-summary.json` is emitted in the existing private
`tools-live-evaluation-{run_id}-{run_attempt}` bundle. It contains:

- `matrix_complete`; exact `tool_cells` with `cell_id`, `status` and `cleanup`;
  exact `cases` with `case_id` and `status`; `cleanup_status`,
  `unresolved_operations`, and `final_release_state`.
- Qualification/run/attempt IDs, requirements/schema/workflow revisions, approved
  source and six component source/digests, four-route/generation observations,
  client binary/model/loading modes and fixture definition digest.
- Per-case expected/actual values, trace/receipt/effect/cleanup references and
  every failure/retry. Nested legacy cells cover the exact pre-cutover inventory;
  a single aggregate legacy pass must be rejected if any nested case is absent.
- Independently observed actual provider identities, object IDs/state transitions,
  effect counts and absence/restoration evidence, kept private and hashed.

The reader is separate from the driver, checked in and reviewed. It validates the
complete row inventory, client and scenario mapping, all revision bindings, raw
provider/runtime observations and cleanup before the workflow reports success.
Pin its definition with the harness. The engine then verifies the exact latest
workflow run/attempt, mandatory `evaluate` job, private evidence provenance and
explicit JSON predicates in the authored proposal. A stale success cannot mask
a newer failure. Evidence age is bounded; refresh live observations before final
acceptance if the release or grant changed.

The repository observer remains `live_attestation: false`: it proves the selected
workflow/artifact contract. The accepted policy delegates live acceptance to the
independently reviewed reader and exact complete-criterion artifact predicates. The final machine evaluation requires the reviewed independent reader to verify
those records and actual runtime/provider observations against every existing
story criterion and EC-01–06. No human evidence decision is inserted. A missing fixture,
mandatory skipped/not-run row, provider uncertainty, cleanup failure or incomplete
legacy inventory prevents completion. Record each as blocked/fail/unknown with
owner/action; never convert it to pass, local deletion, or a completed issue.

## Actors, phase evidence and automatic completion

The [capability audit](agent-capabilities.md) defines the executable actor path.
Developers implement the existing AC/scenario procedures; reviewers inspect and
repair code. The operations worker controls allowed workflows through mediated
tools and diagnoses their logs; it cannot run a browser, install Claude/Codex or
call provider APIs directly. Those actions run in the protected workflow with
private fixture authority. The machine observer validates its evidence. Stages 1–7 use the generated-data
phase contract in [the data strategy](test-data-strategy.md); this live protocol
cannot become an upfront requirement for provider fixtures.

Proposed `atlassian-stage-summary.json` records `stage_id`, exact issue-qualified
`criteria` with `phase_status` and `evidence_verified`, `runtime_verified`,
`independent_verification`, `cleanup_status` and `unresolved_operations`. Each
criterion also maps the original scenario/procedure and explicit clauses due in
that phase to commands and independent observations. Controlled phase reports
include `verification_level: controlled_integration`, `fixture_source: generated`,
`provider_live_attestation: false`, `test_runtime_artifacts_match: true` and a
separate `pending_live_checks` ledger; no controlled observation accepts a live clause. A phase pass cannot accept
future live clauses. #8453/#8454 construction/deployment qualification precedes
the coordinated handoff; their remaining live clauses are required afterward.

The final summary explicitly requires `provider_live_attestation: true`, verified
against actual provider observations by the independent reader. It additionally
contains exact `criteria` rows for all nine
stories and EC-01–06 (`status: pass`, `evidence_verified: true`), all `stages`,
`unresolved_prerequisites: []` and `completion_ready: true`. The independent reader
checks the complete clause mapping; fields alone cannot establish success.
The final operations owner must remain active through this result and cleanup,
so failures can use its existing repair tools and verified revision chain.
#8453 integrates that completion condition and affected-component release;
#8454 implements the evidence and idempotent issue-completion consumer.

Qualify actual agent execution using #8453 AC-02/04 and #8454 AC-04/05/06:
retained assignment -> scoped repair PR -> real required CI -> protected merge
-> corrected deployed artifacts -> rerun of failed original criteria -> automatic
successor. Verify worker restart from `repair_tasks`. Reuse these existing
criteria; this is not a new arbitrary test catalogue.
