# Task API validation and acceptance plan

**Design:** [README.md](README.md)

**Status:** Planned validation; no implementation or live results are claimed.

**Purpose:** Define separate, assignable validation stories for the requested epic.

**Delivery epic:** [#5792](https://github.com/aws-e/adp/issues/5792).

| Validation story | GitHub-native child |
|---|---|
| V1 — Contract, authorization and storage | [#5802](https://github.com/aws-e/adp/issues/5802) |
| V2 — Worker independence and regression | [#5803](https://github.com/aws-e/adp/issues/5803) |
| V3 — Recovery, streaming and control | [#5804](https://github.com/aws-e/adp/issues/5804) |
| V4 — Live external integration and coexistence | [#5805](https://github.com/aws-e/adp/issues/5805) |
| V5 — Rollout, rollback and release acceptance | [#5806](https://github.com/aws-e/adp/issues/5806) |

The first release must prove a complete external task and preserve the existing
GitHub/Claude/Codex paths. Implementation PR checks and post-deployment evidence
are distinct. A passing mocked test, queued message, HTTP 200 or merged PR does
not establish that an agent executed or that progress streamed live.

## 1. Shared evidence requirements

Every validation report records source SHA, tested artifact/image digest,
configuration/flags, runtime versions, test command, timestamp and outcome per
acceptance ID. Live reports also name AWS account, region, cluster/environment,
service principal, permitted spend/traffic, owned fixture resources and cleanup.
Record `PASS`, `FAIL`, `NOT RUN` or `BLOCKED` with the missing prerequisite; do not
convert skipped work into a pass.

Use synthetic tenant-owned fixtures and bounded tasks. Store sanitized request
IDs, task/run/attempt IDs, event cursors, timing observations and artifact digests.
Do not commit tokens, signed download URLs, private credentials or unrelated
customer task content. A reviewer must be able to trace each result to the
specific code/configuration tested.

The contract story freezes measurable limits before acceptance: progress latency,
revocation/cancellation bounds, concurrency, event sizes, retry windows and
retention. Tests cannot choose looser limits after observing a failure. For a
controlled healthy live fixture, the proposed streaming target is two distinct
authored updates delivered within five seconds each while the task is held open;
T0 confirms or amends that target before implementation.

## 2. V1 — Contract, authorization and storage qualification

**Owner:** Validation agent independent of the ingress/storage authors.

**Depends on:** T0-T3 and the task read/report authorization surface in T6.

**Environment:** Deterministic tests and an appropriate real-storage/IAM lane;
any untested AWS semantics remain explicitly pending V4/V5.

| ID | Required proof |
|---|---|
| V1-01 | A valid external identity resolves to the same canonical principal/tenant in Lambda and gateway. Invalid, expired, revoked, wrong-issuer/audience and unregistered credentials are rejected. |
| V1-02 | Body/header-supplied tenant, service identity, owner, command target or lineage cannot replace authenticated scope. Cross-tenant and same-tenant nonowner task/event/artifact access is refused. |
| V1-03 | Only explicitly registered and permitted `agent-task-*` implementations are accepted. Unknown task prefixes, executable/path injection and existing GitHub personas are rejected without a queue publication. |
| V1-04 | Concurrent same-key/same-body submissions return one stable task/dispatch. Different-body reuse conflicts. Retry after a lost acceptance response does not create fresh work. Scope keys by tenant and principal. |
| V1-05 | Acceptance-storage or authority failure cannot return a false `202` or launch an unbound run. Exercise partial transactions/prepared states and compare actual records/publications. |
| V1-06 | New metadata, command, idempotency and progress records do not appear as extra runs in legacy tenant/user/correlation/engine-command queries. Invocation detail never resolves to a progress record. |
| V1-07 | Legacy and task workers cannot overwrite protected task scope/inputs, fabricate progress/completion or redirect artifacts. Prove the actual IAM/key/integrity boundary; a mocked writer refusal alone is insufficient. |
| V1-08 | Rate/persona/model/budget/credential restrictions remain enforced under registered service identity. No fake GitHub installation, service-name impersonation, forged human root or disabled global guard is used. |
| V1-09 | Payload bounds, S3 references, retention and cleanup preserve active tasks and deny unowned artifacts. Test expired idempotency/replay records and asynchronous TTL behavior without promising exact deletion time. |

## 3. V2 — Worker independence and existing-path regression

**Owner:** Runtime validation agent separate from task-agent implementation.

**Depends on:** T3-T5; task reporting from T6.

**Environment:** Real built worker image plus controlled dependencies; live
legacy-path proof is completed in V4/V5.

| ID | Required proof |
|---|---|
| V2-01 | One image contains the unchanged existing runtime packages and the separate task package. Existing dependency versions and entry commands are preserved; the task package is not imported by legacy executions. |
| V2-02 | A task envelope with no installation/repo/issue reaches the task branch before GitHub validation, poison-message handling, token minting, checkout and comments/checks. Unknown task personas do not fall through to Claude. |
| V2-03 | Execute a useful fixture with GitHub credentials absent and GitHub network access denied/observed. Assert zero GitHub requests and no fabricated issue/installation. Packaged GitHub tools in the shared image are not evidence of integration. |
| V2-04 | Existing GitHub, agent-trigger and EventBridge Lambda fixtures preserve routing, authentication, outputs and queue envelopes with task admission both off and on. Task-only import/initialization failure does not break those handlers. |
| V2-05 | Existing Claude persona and `agent-codex-reviewer` execution/finish fixtures preserve their behavior. Include Codex stdin/result handling and its dedicated finalization. |
| V2-06 | Task input, workspace, model access and run-bound credentials are prepared without entering the GitHub host flow. Stale/wrong-run credentials and forged task references fail before execution or reporting. |
| V2-07 | The new runtime reports substantive progress before exit, produces validated final output and stores result references before acknowledging completion. Buffered final stdout alone cannot pass. |
| V2-08 | Worker termination, reporting failure and cleanup leave explicit recoverable/terminal state. Tokens, subscriptions, heartbeats and temporary files are released without deleting another assignment. |

## 4. V3 — Failure recovery, streaming and control qualification

**Owner:** Reliability/streaming validation agent.

**Depends on:** T1-T7.

**Environment:** Integrated services with fault injection; verify distributed
storage/queue behavior against real services where emulation cannot establish it.

| ID | Required proof |
|---|---|
| V3-01 | Interrupt admission before/after record commit, queue send and send acknowledgement. Automatic recovery advances accepted work without a caller retry and does not dispatch a second active owner. |
| V3-02 | Duplicate/redelivered SQS messages and concurrent consumers respect task grouping, idempotency and ownership. An expired lease alone is not permission for conflicting external effects. |
| V3-03 | A replaced worker/attempt cannot append new progress, complete the task or acknowledge another delivery. Recovery preserves task ID and creates explicit run/attempt history. |
| V3-04 | SSE covers subscribe-before-start, mid-run subscription, completed tasks, disconnect/reconnect, duplicate frames, expired cursors and backend restart. Snapshot-to-live handoff has neither silent loss nor fabricated history. |
| V3-05 | Reconnect across the API Gateway connection limit continues the same task. Verify real flushing and heartbeat behavior; heartbeat-only traffic cannot satisfy progress latency. |
| V3-06 | A slow/disconnected consumer, full buffer or relay failure does not block useful agent work indefinitely. Bounds are enforced, gaps are explicit, and terminal evidence is recoverable. |
| V3-07 | Follow-up input is durable and consumed once according to its command ID. Distinguish accepted from consumed; an ordinary message cannot grant privileged authority. |
| V3-08 | Cancel-before-dispatch, during execution and racing completion produce honest states/receipts. Repeated cancellation is idempotent; `cancel_requested` is not reported as confirmed exit. |
| V3-09 | Revoked/expired access closes active subscriptions within the agreed bound and rejects future task/command/artifact requests. No credentials are placed in URLs or events. |
| V3-10 | Terminal failure, exhausted recovery and unavailable results are visible in the task API. Missing heartbeat/cost/trace data is not converted into success, exit or zero cost. |

## 5. V4 — Live external integration and coexistence evaluation

**Owner:** Operations/evaluation agent, with explicit environment authorization.

**Depends on:** T0-T8; V1-V3 reports with remaining live cells identified.

**Environment:** Named authorized ADP deployment; no production assumption.

| ID | Required proof |
|---|---|
| V4-01 | Record actual API Gateway integrations, Lambda version, task module configuration, queue/KEDA target and worker image digest. Prove the Task API uses the existing gateway, Lambda, queue and image family. |
| V4-02 | From outside the cluster, use a registered external service to submit a task in a tenant without GitHub integration. Receive an ADP task/run handle, not an SQS message ID masquerading as a run ID. |
| V4-03 | Observe two distinct authored progress markers within the agreed latency before a held-open task completes. Measure emitter and external receiver timestamps through the actual public API/SSE path. |
| V4-04 | Disconnect and reconnect, answer an input request, retrieve a completed result/artifact and verify its integrity. Submit a separate cancellation fixture and observe confirmed outcome. |
| V4-05 | Exercise denied cross-tenant, same-tenant nonowner, unregistered persona and revoked-credential cases; record zero unauthorized dispatches/reads. Verify the relevant real IAM/authority boundary. |
| V4-06 | Run bounded existing GitHub/Claude and Codex-reviewer regression fixtures while task traffic is active. Measure Lambda errors/throttling, queue age, worker scheduling and table latency against the agreed baseline. |
| V4-07 | Demonstrate platform model access, budget/cost attribution and permitted task credentials under the external service principal, with no GitHub credential bootstrap. Unknown evidence remains unknown. |

## 6. V5 — Rollout, rollback and operational acceptance

**Owner:** Operations evaluator independent of the rollout author where practical.

**Depends on:** Deployment/rollback implementation in T8 and V4 evidence.

| ID | Required proof |
|---|---|
| V5-01 | With task admission disabled, existing flows work and no task is accepted/queued. Enabling a task route does not enable legacy mutation flags or widen existing persona authorization. |
| V5-02 | Deploy task-capable workers before admission and prove incompatible consumers cannot receive new task messages. Exercise or explicitly exclude mixed-version queue consumption. |
| V5-03 | Stop admission with accepted/running tasks present. Drain, cancel or durably quarantine those tasks while preserving reads and results. Do not roll back all consumers until task messages are safely handled. |
| V5-04 | Rollback leaves existing handlers and agents functional; no request table is replaced, shared queue purged, or legacy records/permissions destroyed. Task-only errors stay within the bounded rollout policy. |
| V5-05 | Retention and cleanup remove only owned fixtures/artifacts/configuration at the appropriate time. List anything intentionally retained, such as sanitized evidence, with its expiry. |
| V5-06 | Produce the final matrix linking every V1-V5 acceptance ID to evidence. Record remaining limitations and obtain release acceptance; merged code or a single happy-path run cannot close the epic. |

## 7. Execution order and independence

V1 and V2 can run in parallel as their owned components become available. V3
requires the integrated contracts but may develop fault fixtures earlier. V4
uses their reports to close the real deployment cells; V5 proves rollout and
rollback after end-to-end behavior is established. Each evaluator should have
clear fixture ownership to avoid modifying another evaluation's task or flag.

The native GitHub child stories carry these V1-V5 scopes and dependencies.
Implementation agents own fixes for failed criteria;
evaluators record evidence and rerun affected criteria. No validation story
authorizes unrestricted traffic, infrastructure changes, paid model runs or
retirement of the existing paths by itself.
