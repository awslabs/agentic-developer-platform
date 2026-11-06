# Test data without manual preparation

The user reports that little provider test data is available. Development must
not depend on people creating test accounts, projects, pages or permission
matrices. This revision supersedes the earlier requirement for a complete live
fixture manifest before stage 1. It changes phase prerequisites, not the product's
authorization model or the meaning of live acceptance.

## What is actually known

| Item | Evidence available | What is not established |
|---|---|---|
| Jira/Slack scenario functions | The inspected [runner](https://github.com/aws-e/adp/blob/c8c42938888bcec577965d517acaeb7ff86d4914/tests/e2e/tools/runner.py) loads optional private Jira/Slack driver and observer modules; absent bindings return `None`. | A usable private module, consenting account, project/channel or cleanup permission for this epic. Function signatures are not live fixtures. |
| Controlled tests | Existing [Jira tests](https://github.com/aws-e/adp/blob/c8c42938888bcec577965d517acaeb7ff86d4914/tests/e2e/tools/test_jira_integration.py) and [Slack tests](https://github.com/aws-e/adp/blob/c8c42938888bcec577965d517acaeb7ff86d4914/tests/e2e/tools/test_slack_integration.py) supply synthetic drivers and observers and reject unsupported live claims. | Complete tests for the new Atlassian adapter. Those remain story deliverables. |
| Protected workflow | [tools-live-evaluation.yml](https://github.com/aws-e/adp/blob/c8c42938888bcec577965d517acaeb7ff86d4914/.github/workflows/tools-live-evaluation.yml) accepts revision/filter inputs and uses private configuration. | That required private values, installed-client/model access or new selectors are available for this epic. No secrets were inspected to establish readiness. |
| New adapter/probe/harness | Proposed paths and contracts are documented. | Implemented fixture generators, provider observers, lifecycle drivers or an authenticated Identity/Atlassian proof. |
| Target and permission | The user identified the dev target and authorized creating/deleting an ADP test user. | Delegation to the hosted worker, an Atlassian identity/consent, or rights to create provider accounts/projects/spaces. ADP user creation does not create provider access. |

This is a source audit at the linked revision, not a current inventory of private
accounts or a live qualification result. Unknown availability stays unknown until
a scoped discovery operation produces evidence.

## Three kinds of verification

| Kind | Setup and data | What it proves | When it gates progress |
|---|---|---|---|
| Controlled integration | Agents generate users, tenant/site identifiers, issue/page/message data and faults inside an isolated test runtime. Run real ADP handlers, databases, workers and adapter transport; substitute external Identity/provider boundaries only. | ADP authorization, state handling, recovery, protocol behavior, UI/config logic and cleanup under the stated model. It does not prove the real provider accepts that model. | Development phases. Missing provider data does not block these checks. |
| Deployment verification | Operations verifies the reviewed images/config/migrations/consumers on the accepted dev target. Run health and unconnected/denied behavior; run controlled integration in an isolated runtime using the same reviewed artifacts. | Correct deployment, service isolation and ADP behavior. Test configuration cannot serve ordinary connector traffic. | Every deployable increment; real deployment failures still block. |
| Provider live qualification | Discover existing authorized access; agents create only the small amount of disposable data the granted scope permits, or observe existing permitted read data. | Real Identity/provider compatibility and actual installed-client calls/effects. | Relevant connector activation/cutover and final live acceptance. Missing access does not retroactively fail independent development phases. |

Controlled cases use explicit expected state and an independently checked journal
or state readback. Do not just return a prepared “pass”. Simulators belong only
in isolated test configuration, with no production authentication bypass, fake
grant, or direct OAuth fallback. Unknown public/provider contracts remain
provisional behind disabled activation until an authenticated probe validates
them. A known incompatible contract is an engineering defect to resolve, not
something to defer as missing data.

## Agents own setup and cleanup

| Need | Agent action | Minimum external dependency / fallback |
|---|---|---|
| ADP users, tenants, grants and denied cases | Seed disposable controlled-test identities; implement supported dev-user provisioning/cleanup where needed under the existing authorization. | No new real provider accounts. Never treat seeded claims as real OAuth consent. |
| Jira reads and writes | Generate complete issue data locally. With existing authorized live project access, discover legal fields/transitions, create one owned issue per client, use it for search/get/update/comment/transition, then clean it up. Persist ownership before writing. | No dedicated Jira project or pre-populated dataset required. If no suitable project/create/delete permission exists, retain those live checks as pending; do not ask someone to manufacture issues. |
| Confluence reads | Generate pages locally. With access, snapshot one permitted existing page's ID/version/content or create a disposable page only if creation and cleanup are authorized. | No new space or bulk content. If neither read data nor create/cleanup is available, only Confluence live checks remain pending. |
| GitHub/Slack/legacy Jira parity | Capture the expected verb/schema baseline from the existing implementation and qualified catalogue; generate controlled responses and errors. Use existing granted repository/channel/site access for applicable live probes. | An open-source repository can supply public reads, not mutation authority. No assumption of a dedicated Slack workspace/channel or provider account. |
| Multiple owners/sites and error cases | Generate owner/site/scope contrasts and provider faults locally, including expired responses and lost acknowledgements. | Additional real users/sites are needed only for live claims that specifically require them. A synthetic second identity cannot establish real cross-account isolation. |
| Independent readback | Implement a separate reader that verifies IDs, fields, versions and receipts from the provider/state source rather than trusting the tool response. Use existing authorized read access where sufficient. | Independence does not automatically require another provider account or administrator token. Record any observation that cannot be obtained with the available grant as unproven. |
| Real clients | Reuse supported installers and isolate managed config; generate controlled MCP responses for wiring tests. Discover protected ADP/model access before scheduling real client invocations. | No assumption that installed binaries or a successful config test establishes model access or a live named-tool call. |
| Recovery and cleanup | Record owned IDs, operation payloads and pending cleanup durably. Retry/reconcile through the supported consumer; preserve reusable resources. | Never require a human to clean each run. If cleanup cannot be authorized and verified, do not begin the corresponding live mutation. |

These generators and lifecycle handlers are implementation tasks, not an
already available test platform. #8446 owns capability discovery, the controlled
probe and truthful evidence separation; #8447 owns generated DB/service fixtures
and recovery foundation; each feature story supplies its own controlled cases.
#8453 owns deployment/activation readiness; #8454 assembles minimal live data
creation, independent readers, durable resume and cleanup. No unrelated fixture
framework or new standalone workflow is required.

## Progression and evidence

Stages 1–7 pass their implementation and deployed-runtime clauses using controlled
integration. Each phase artifact must explicitly record
`verification_level: controlled_integration`, `fixture_source: generated`,
`provider_live_attestation: false` and `test_runtime_artifacts_match: true`, in
addition to the existing runtime/evidence/cleanup predicates. These are proposed
report fields for the story-owned runner, not currently supported CLI switches.
The independent reader verifies their provenance and the actual phase assertions.
Missing live facts go into a separate durable `pending_live_checks` ledger.

The controlled phase predicate deliberately cannot be satisfied by a partial live
run. If live access is available early, run it separately and retain that evidence;
it does not replace controlled failure coverage. The original AC/scenario IDs
are preserved. A phase pass never means an entire criterion or story is complete.

Before stage 8 changes shared connector routing, operations checks the actual
provider/Identity contract and the required parity evidence for the affected
configured routes, plus rollback readiness. Missing essential proof holds that
handoff, while implementation, gated deployment and independent verification can
continue. No simulator evidence may enable a real connector. Real expiry and
multi-account cases remain in final live acceptance; they are not prerequisites
for writing or reviewing unrelated code.

Stage 9 runs the eligible live cases and retains pending ones with their exact
missing capability. A partial result is progress, not a full-suite pass. Stage 10
requires all original mandatory live clauses and cleanup before full completion.
Do not silently shrink the expected matrix to the data discovered that day.

For an unavailable prerequisite, record it once, avoid repeated model retries or
per-test prompts, and continue independent work. When a real provider connection
is needed, reuse the normal Connections consent journey; request only the
unavoidable account-owner/admin action, grouped once. Never ask people to prepare
test datasets or repeat consent for every development stage. A required action
without authorization remains pending; elapsed time is not consent.
