# Complete the code, then qualify one release

This execution strategy replaces the remaining per-story deployment checkpoints
for epic #8445. The owner requested this change on 2026-10-25 after repeated
deployment and reporting repairs delayed implementation. Completed work and its
evidence remain historical facts. Design v5 and every original acceptance
criterion remain in force.

## Execution

| Step | Work and owner | Completion evidence |
|---|---|---|
| Parallel code development | Codex developers implement reads and legacy connector parity (#8449), Connections UI (#8451), and release automation (#8453) concurrently, within the existing three-action limit. Release automation uses the established foundation and interface contracts; it does not wait for later runtime acceptance. | Each PR has focused tests, repository CI, independent review and a native verified merge. No story-specific deployment is required to release another developer. |
| Dependent code and integration | After reads merge and pass CI, developers implement writes/recovery (#8450) and client wiring (#8452). The harness owner (#8454) integrates all five merged implementations and finishes the shared test runner, fixture lifecycle and independent reader. | All six code stories are merged with their required CI. The harness is exercised locally/in CI, including its command entrypoints, negative evidence cases and cleanup failure recovery. |
| One coordinated release | Existing operations executors deploy the compatible gateway, frontend, adapter, facade, interceptor and platform from the final integration revision, using their existing protected workflows and required component ordering. | One manifest binds the actual deployed artifacts, source, configuration and workflow receipts. This is one release campaign; it can contain several existing component workflows. |
| One complete acceptance campaign | The protected test workflow runs the accumulated controlled scenarios and eligible provider-live cases against that release. Its independent reader verifies the observations and cleanup. | The existing final acceptance gate requires every original story AC and epic EC, both clients, all required tool cells, legacy parity, authorization, refresh, recovery, rollback and cleanup. |

The code evidence nodes observe existing PR CI and merge receipts. They do not
dispatch stage deployments or wait for stage runtime reports. Deployment-specific
checks stay in repository workflows and tests; this change adds no engine code.
Existing automatic main-branch workflows remain enabled for other repository
work. They do not block code-only development; the combined acceptance campaign
uses the final integrated candidate.

## Shared test ownership

Each implementation story contributes its scenarios and independent expected
results to the shared suite. It does not build a separate stage deployment,
publisher or evidence-reader stack. #8453 owns the release entrypoints and repair
handoff. #8454 owns the combined runner, fixture ledger, result aggregation and
independent reader, integrating the other stories' cases.

The final `atlassian-flow-summary.json` retains its existing contract and adds
`stage_reports`, keyed by `identity`, `reads-parity`, `connections`, `write-recovery`,
`clients`, `release-automation` and `harness`. Each entry carries the same fields
and measured predicates previously required by that stage's separate gate.
The final gate applies those predicates at `/stage_reports/<stage-id>/...`.
Moving their execution does not mark unrun tests as passed or replace provider
observations with generated data.

The unfinished intermediate Identity operation is retired with its history and
actual outcome retained. Its acceptance predicates move to this same final
campaign; it is not marked successful merely to start the remaining developers.

Generate disposable fixtures automatically. Discover real provider and client
access during implementation so missing consent or policy is identified before
the final campaign. Controlled results and provider-live results remain distinct;
the final live requirements cannot be satisfied by a mock or a skipped case.

## Failure handling and boundaries

Keep the same integration owner through failure diagnosis, fixes and cleanup.
Fix a concrete failing case, run the affected checks, and deploy a new candidate
only when the fix changes deployed artifacts. The final acceptance report must
still describe a coherent, verified release; a result from an incompatible older
candidate cannot be reused. A single campaign is not a guarantee that the first
test run finds no defects.

Preserve the existing GitHub App creation, registration and ADP sign-in flow.
Keep AgentCore Identity responsible for consent and token handling. Preserve
retry counts, budgets, completed-node history and the final live acceptance
criteria. Do not add product-specific engine checks or a new deployment engine.
