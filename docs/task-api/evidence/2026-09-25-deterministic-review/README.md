# Independent V1–V3 evidence review

Reviewed merged release `d2aff6406` on 2026-09-25. This review links existing successful execution records to implementation tests. It does not claim live qualification or close the epic. No tests were rerun, tasks submitted, or deployment settings changed for this review.

## Recorded evidence

- **G:** Gateway combined suite: **675 passed**. See `release-final-gateway-tests.log`.
- **W:** Worker suite: **85 passed**. See `release-final-worker-tests.log`.
- **F:** Separate Task runtime factory / route regression suite: **59 passed**. See `release-factory-tests.log`.
- **C:** Cancellation integration and contract suite: **26 passed**; official contract checker: **369 checks passed**, exercising 62 criteria. See `cancel-final-tests.log` and `cancel-final-contracts.log`.
- **D:** Deployment/route checks: **11 passed**. See `release-route-deploy-tests.log`.
- **I:** Actual worker image `sha256:3b02abc4a587ec60dc28a90809c7a9c1a8d8ef017bd49fe2c4de8b466f23bef3`, source `ba086a30d`, exercised in an isolated Kubernetes pod. Packaged investigator completed useful and four-artifact scenarios, acknowledged cancellation, and rejected malformed output. Four 256 KiB artifacts used 32 chunks, with bounded model frames and explicit evidence omissions. Legacy worker/reviewer binaries passed syntax and selector checks. No service-account token was mounted; egress was denied; the child network observer recorded zero requests. Installed host/client/protocol/dispatch/entrypoint bytes matched the source revision. Both owned smoke resources were deleted. See `t3-final-image-smoke-*` artifacts.

The aggregate pytest logs do **not** embed source revisions or node IDs. Counts establish recorded suite outcomes, not exact-head provenance for every criterion below. Image provenance has a stronger explicit digest/source/file binding. `index.json` records hashes for all copied artifacts. Earlier failed or superseded logs are not presented as successful evidence.

Test paths below are relative to the repository root. `G-tests` abbreviates `modules/gateway/tests`; `W-tests` abbreviates `modules/agent-factory/agent-worker-image/tests`. Presence of a test is distinguished from external production observations.

## V1 — authority and persistence

| Criterion | Deterministic evidence and implementation tests | Remaining acceptance evidence |
|---|---|---|
| V1-01 | G/F: `G-tests/agentauth/test_task_admission_integration.py`, `G-tests/internal/test_task_admission_proof.py`, `G-tests/tasks/test_routes.py`; real proof binding and caller resolution seams. | Observe canonical identity through deployed ingress and gateway; live expired/revoked/wrong-audience refusals. |
| V1-02 | G: task routes, artifact routes, command routes and Dynamo read integration tests reject nonowner/body-scope substitution. | Controlled same-tenant nonowner and cross-tenant requests, including events/artifacts; prove zero unauthorized effects. |
| V1-03 | G/W: admission integration, worker `test_task_dispatch.py`, `test_task_entrypoint.py`, protocol fixtures. | Observe unknown persona rejection on actual public ingress without dispatch. |
| V1-04 | G: `G-tests/tasks/test_store.py`, `test_store_invariants.py`, real admission integration cover stable scoped idempotency and conflicts. | Live same-key replay/conflict and concurrent acceptance receipt correlation. |
| V1-05 | G: acceptance transaction and dispatch/publisher integration tests inject partial/failed writes. | Controlled deployed fault at acceptance/publication boundary; inspect actual durable rows and queue evidence. |
| V1-06 | G: storage tests keep Task records out of legacy indexes and preserve legacy invocation lookup. | Optional production query comparison against retained legacy baseline; no claim from image smoke alone. |
| V1-07 | G/D/F: protected grant digests, current-attempt transactions, scoped infrastructure and workload verifier tests. | Actual IAM negative probes and deployment policy evidence. Local mocks/Moto are insufficient for full IAM acceptance. |
| V1-08 | G/F: `test_task_model.py`, `test_task_model_binding.py`, `test_task_admission_storage.py`, factory tests cover strict budget/model bindings and separate rollout guards. | First real service-owned model operation, provider pricing receipt, usage attribution, and budget settlement. |
| V1-09 | G/C: artifact ownership/bounds, JSON numeric encoding, storage retention tests, owner-fenced abandoned admission cleanup. | Record live artifact digest and cleanup/retention disposition; asynchronous TTL is not an exact deletion guarantee. |

## V2 — runtime separation and behavior

| Criterion | Deterministic evidence and implementation tests | Remaining acceptance evidence |
|---|---|---|
| V2-01 | W/I: image contains separate investigator plus existing worker/reviewer; installed selectors and source hashes verified. | Full dependency-version comparison and legacy execution regression are broader than syntax/selector smoke. |
| V2-02 | W: Task dispatcher/entrypoint tests exercise Task-before-GitHub routing and reject unknown personas. | Actual accepted envelope must reach the new worker without GitHub bootstrap. |
| V2-03 | I: compiled useful investigator ran with no GitHub credentials, no SA token, deny-all egress and zero observed network requests. | Live run remains needed for the deployed admission/model/report pipeline; this isolated image result is valid for runtime separation only. |
| V2-04 | Relevant tests exist in `webhook-ingress/lambda/task_api/tests` and `lambda/github/tests/test_handler_task_route_isolation.py`; D covers route/deployment checks. | No separate Lambda regression execution log is copied here; attach one before marking this criterion satisfied. |
| V2-05 | I: both existing binaries present, syntactically valid and selected correctly. W contains worker routing tests. | Actual bounded Claude and Codex-reviewer execution/finish fixtures, including reviewer stdin/result/finalization. |
| V2-06 | G/F/W: bootstrap/attempt/credential/turn/artifact tests; exact Task flag, SA and digest; generic authority remains separately gated. | Observe actual TokenReview/bootstrap/attempt registration and no GitHub credential acquisition in pilot. |
| V2-07 | W/I: substantive progress, validated output, report acknowledgment and terminal-before-ack tests; image useful result. | Public SSE progress before task completion and durable result/ack receipts; result-artifact integrity is not proven by input-artifact smoke. |
| V2-08 | W/G/C: bounded report recovery, child termination, settlement, unknown provider outcome, cleanup and owner-scoped queue acknowledgment. | Observe live stop/ack/retained-budget records; destructive fault injection not performed by this review. |

## V3 — recovery, streaming and command behavior

| Criterion | Deterministic evidence and implementation tests | Remaining acceptance evidence |
|---|---|---|
| V3-01 | G: acceptance/dispatch storage and HTTP publisher seams; admission cleanup uses owner CAS and scheduled bounded shards. | Deployed automatic recovery after interruption without a caller retry. |
| V3-02 | G: `test_task_delivery.py`, `test_task_work.py`, storage tests exercise duplicate delivery, competing journals and lease fences. | Live duplicate/redelivery evidence with one current owner and no duplicate inference. |
| V3-03 | G/F: stale-attempt writes and grant binding tests, including model handoff/cancellation races. | Deployed replaced-attempt refusal and explicit attempt-history evidence. |
| V3-04 | G: `test_streaming.py` covers initial/mid-run/terminal/restart/reconnect, gaps and expired cursors. | Actual public streaming reconnect over retained task history. |
| V3-05 | G: connection-window, flushing and heartbeat tests. | Actual API Gateway connection-limit reconnect and measured public flush/heartbeat timing. Local ASGI tests cannot close this criterion. |
| V3-06 | G/W: bounded SSE buffers and report retry tests; only committed history is replayed. No synthetic `history.gap` is fabricated. | Slow external receiver and relay fault observations; quantify useful-work impact and recoverable terminal evidence. |
| V3-07 | G/W: canonical immutable turn, FIFO command consumption/events and eight-turn tests; assigned input turn retained during outstanding model call. | Live input acceptance → exact command consumed once → next model turn; clarification reply flow where exercised. |
| V3-08 | C/G/W/I: real SQS cancellation before bootstrap drains exact envelope, releases counters; real Redis releases proven unused admission hold; no-attempt/attempt race tests; compiled and host cancellation; public no-attempt proof validates. | Live prestart cancellation plus cancellation during actual inference/completion. Unknown provider outcome must retain its hold. |
| V3-09 | G: active stream revocation/recheck tests and scoped read/command/artifact guards. | Deployed credential/policy revocation observed within agreed latency bound, with future requests denied. |
| V3-10 | G/W/C/I: unknown model receipt stays unknown, bounded model polling, no fabricated child exit/cost; malformed output produces explicit error. | Live terminal error visibility and recovery exhaustion evidence; no inference success inferred from a healthy pod. |

## Practical remaining live sequence

1. Correlate one external 202 through durable dispatch, assigned pod, confirmed attempt, provider request ID, canonical turn, report, terminal result and confirmed SQS deletion.
2. For that owned task, collect same-key replay/conflict, uploaded artifact hash, follow-up command consumption and reconnecting public SSE.
3. Verify provider pricing confidence, UsageLog, awaited S3 usage event, tracker budget ledger and Redis settlement separately. A completed task alone proves none of those financial effects.
4. Run separate prestart and running cancellation observations. Keep unknown provider spend distinct from zero; record gateway no-attempt proof only when no attempt existed.
5. Add bounded ownership/revocation/persona denials, same-queue legacy compatibility evidence, and the numeric latency/capacity measurements required by V4/V5.

The prepared two-task `qualify.py` runner explicitly leaves cross-principal, revocation, coexistence, controlled latency, absent result artifacts and absent measured costs **NOT RUN**. Preserve those gaps in the final acceptance matrix.

The evaluation manifest still contains historical `not_implemented` command entries. They are not a reliable implementation inventory. This review supplies concrete test locations without replacing whole-criterion commands with partial local checks or claiming a live evaluator exists where it does not.
