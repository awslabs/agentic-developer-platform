# Task API waves and evaluation definitions

External applications need to give ADP useful work and follow it through to a result without turning every request into a GitHub issue. This epic adds a Task API so a service can submit work, receive a stable task handle, observe live progress, provide follow-up input, request cancellation and retrieve the result. It must work for tenants with no GitHub integration.

Tasks will run through the existing ADP API Gateway, ingress Lambda, queue and worker infrastructure, using a separate task agent inside the existing worker image. Durable acceptance, idempotency, recoverable dispatch and retained progress let clients reconnect without losing accepted work or its history. Authentication, tenant isolation, model, budget and credential policies remain authoritative.

The delivery covers contracts, storage and APIs, worker execution, interaction and recovery, external integration, and release qualification. Each wave has an independent evaluation. Rollout must preserve accepted tasks and existing GitHub, Claude and Codex paths; retiring those paths is outside this epic.

## Wave plan and evaluation ownership

| Wave | Implementation stories | Evaluation |
|---|---|---|
| 1 — Contracts and design | [T0 #5793](https://github.com/aws-e/adp/issues/5793) | [V0 #5821](https://github.com/aws-e/adp/issues/5821) |
| 2 — API and persistence | [T1 #5794](https://github.com/aws-e/adp/issues/5794), [T2 #5795](https://github.com/aws-e/adp/issues/5795), [T3 #5796](https://github.com/aws-e/adp/issues/5796), [T6 #5799](https://github.com/aws-e/adp/issues/5799) | [V1 #5802](https://github.com/aws-e/adp/issues/5802) |
| 3 — Worker execution | [T4 #5797](https://github.com/aws-e/adp/issues/5797), [T5 #5798](https://github.com/aws-e/adp/issues/5798) | [V2 #5803](https://github.com/aws-e/adp/issues/5803) |
| 4 — Interaction and recovery | [T7 #5800](https://github.com/aws-e/adp/issues/5800) | [V3 #5804](https://github.com/aws-e/adp/issues/5804) |
| 5 — External integration | [T8 #5801](https://github.com/aws-e/adp/issues/5801) | [V4 #5805](https://github.com/aws-e/adp/issues/5805) |
| 6 — Release acceptance | Release qualification over T8/V4 evidence | [V5 #5806](https://github.com/aws-e/adp/issues/5806) |

V0 is the explicit contract-baseline evaluation replacing the draft's generic `component-evidence` node. All 84 original T0–T8/V1–V5 criteria remain unchanged; V0 adds eight contract/evaluation-readiness criteria.

T0 → V0 establishes the start boundary. After V0 passes, implementation stories may prepare and develop in parallel against versioned fixtures. Each wave completes through its own independent evaluation. V1 and V2 can qualify as their required components are ready; V3 requires T1–T7 and V1/V2; V4 requires T0–T8 and V1–V3; V5 requires T8/V4. Integration requirements in the existing stories continue to gate closure.

T0 owns the command/report/coverage manifest and runnable V0 checks. T1/T2/T3/T6 supply the V1 fixtures; T3/T4/T5/T6 supply V2; T1–T7 supply V3. T8 integrates those into the external client and bounded V4/V5 live/rollout tooling. V1–V3 no longer wait on T8 or a generic component-evidence barrier.

Each evaluator records criterion outcomes and reproducible source/fixture/runner evidence. FAIL returns defects to the implementation owner and requires an independent rerun; NOT RUN/BLOCKED never completes the wave. This update defines planning and evaluation responsibilities; the existing flow remains paused with no accepted execution policy or newly granted live authority.

### Display names and purpose

Display metadata preserves the stable wave references used by node addresses.

| Display name | Purpose | Stable wave ref |
|---|---|---|
| Contracts and design | Freeze the task API, lifecycle and data contracts, resolve implementation decisions, and establish the fixtures and evaluation manifest. V0 verifies the contract baseline. | `component-delivery` |
| API and persistence | Build durable task storage, authenticated submission, recoverable dispatch, and status/progress reporting. V1 qualifies contracts, authorization and storage. | `validation-v1` |
| Worker execution | Implement the task worker entrypoint and independent task agent in the existing worker image. V2 verifies task isolation and compatibility with the existing worker path. | `validation-v2` |
| Interaction and recovery | Deliver follow-up input and cancellation to running tasks. V3 qualifies failure recovery, durable streaming and control behavior across the integrated components. | `validation-v3` |
| External integration | Provide the external client example and bounded qualification tooling. V4 verifies live integration and coexistence with the existing platform. | `validation-v4` |
| Release acceptance | Qualify rollout, rollback, cleanup and operational readiness using the integrated implementation and V4 evidence. V5 records release acceptance. | `validation-v5` |


## Evaluation execution contract

### Evidence and failure handling

The evaluation owner is independent of the relevant implementation author where practical. Use the versioned command/fixture manifest introduced by T0 and run the actual owned checks against an immutable tested revision. The manifest records command, test selection, fixture hash, required evidence lane, resource/traffic bounds and report location for each criterion. Commands that are not implemented are BLOCKED; an empty test selection, skip, unavailable required real-service lane or absent evidence cannot pass.

Each report includes the wave/evaluation issue, source SHA, actual image/configuration where applicable, fixture and runner revisions, evaluator identity, exact commands and exit status, collected/executed counts, timestamps, outcome per required ID, artifact hashes and defect references. Use PASS, FAIL, NOT RUN or BLOCKED. A wave completes only when its implementation criteria and all required evaluation criteria have PASS evidence; a merged PR or generic CI check does not substitute for that evidence. Required later live criteria remain separate, with their original scope intact.

A failure returns criterion-specific defects to the named implementation owners. Retain the failed report, fix under a new source/fixture revision, then independently rerun affected checks and the relevant regression set. Do not weaken thresholds to obtain PASS. The subsequently approved execution policy controls retry/spend limits; exhaustion stops for intervention. Downstream qualification stays blocked while its required evaluation is non-passing.

These definitions do not activate an execution policy, approve a flow gate or authorize live operations. Real runner revisions, fixtures and any environment connections must be bound before automated qualification is enabled; no existing runnable test or successful result is claimed by this planning update.

## Tooling owners

### [T0 #5793](https://github.com/aws-e/adp/issues/5793)

Publish the versioned schema/positive-invalid-legacy fixtures, coverage manifest and evaluation command/report format. Implement the V0 contract validator now; register later runtime/live commands as planned until their owning stories provide them. Resolve the exact tooling paths during T0 and keep that manifest versioned.

### [T1 #5794](https://github.com/aws-e/adp/issues/5794)

Provide concurrency, conditional/transactional write, legacy-index, TTL/retention and artifact-ownership fixtures for V1; reusable crash/recovery fixtures feed V3. Supply these with the storage implementation, before V1 runs.

### [T2 #5795](https://github.com/aws-e/adp/issues/5795)

Provide positive/negative identity, route admission, tenant/owner scoping, payload and persona-rejection fixtures for V1, plus admission-failure fixtures for V3. Supply them with the ingress implementation.

### [T3 #5796](https://github.com/aws-e/adp/issues/5796)

Provide assignment/authority/IAM boundary and publication-recovery fixtures for V1; stale-attempt/duplicate delivery and fault injection feed V2/V3. Required real IAM/storage proof must have a bounded authorized lane before a PASS is claimed.

### [T4 #5797](https://github.com/aws-e/adp/issues/5797)

Provide real-image task/legacy entrypoint, process/reporting, grant fencing, failure and cleanup fixtures for V2. Expose the frozen control hooks and reusable process fault fixtures needed by T7/V3.

### [T5 #5798](https://github.com/aws-e/adp/issues/5798)

Provide the useful task persona and real-image isolation/legacy regression fixtures for V2, including GitHub-denied observations and authored progress. Supply deterministic waiting, malformed result and failure fixtures for V3.

### [T6 #5799](https://github.com/aws-e/adp/issues/5799)

Provide read/report authorization fixtures for V1, reporting adapters for V2, and ordered-event/SSE/replay/backpressure/revocation fixtures for V3. Component-level emitters and client probes belong here; they do not wait for the final T8 example.

### [T7 #5800](https://github.com/aws-e/adp/issues/5800)

Assemble the V3 integrated command/recovery harness from T1–T6 fixtures; provide clarification delivery, redelivery, cancellation races and actual host/persona input handling. Record real-service dependencies explicitly.

### [T8 #5801](https://github.com/aws-e/adp/issues/5801)

Own the external client, environment/readiness/admission inventory, bounded live and coexistence fixtures for V4, rollout/rollback/cleanup fixtures for V5, and final evidence packaging. Reuse the V1–V3 fixtures delivered earlier; T8 is not their sole producer or a prerequisite for their component qualification.

## Acceptance scope and execution readiness

A wave label groups related work; it does not erase cross-component integration requirements or force unrelated implementation into a serial queue. The graph has one evaluation per wave. All evaluations are GitHub-native children with independent evidence requirements.

The graph stores issue references and the proposed machine-evaluation intent. No runtime evaluation specification is fabricated for a workflow or fixture that has not been implemented. T0 defines the versioned manifest and contract validator; each subsequent wave supplies its own real checks. Bind the implemented runner revision, fixture definition and permitted environment before enabling machine execution. Criteria are defined now; implementation and measured PASS evidence are later work.

The applied draft revision preserves the acceptance gate address and its unanswered decision. It assigns T0 and V0 to `component-delivery`, and the remaining five waves to `validation-v1` through `validation-v5`. Old graph addresses remain as superseded history, with nine implementation stories and six issue-backed evaluations in the current plan.

## Proposal publication status

The GitHub epic and all 15 native children carry this wave plan. The existing
ADP flow `1275a30d-a84d-4f98-9ddb-f461c5aeb2d1` (`task-api-5792`) was updated
through `adp flow draft preview` and `adp flow draft save` on 2026-09-24, using the
inert revision support merged in [#5829](https://github.com/aws-e/adp/pull/5829).
The authored document is [flow-proposal.json](flow-proposal.json). Its new epic
and wave display metadata is prepared for a wording-only revision; the live
publication state below still refers to version 2 until that revision is saved.

The current draft is **version 2**, hash
`40a150cd9f73865409e1a084be276ab93fc11b9701a28b764e8afc8307c6bdc9`.
Its six waves contain nine implementation stories and six issue-backed
evaluations, plus the original acceptance gate. Version 1 remains readable;
nine removed graph addresses are superseded history. The gate, T0 and V1–V5
retain their original node identities. All 84 original criteria and eight V0
criteria are unchanged.

Live readback verified that the flow remains paused, its original gate
`32acf998-58b8-4fb6-906b-fab8461712b9` remains unanswered, every attempt count is
zero, and there is no active execution policy. The scheduler remains disabled.
The CLI identifies version 2 as proposed, with no accepted version. Saving this
draft did not approve execution or launch work.

The proposed policy remains unaccepted and all runtime evaluation specifications
remain unbound. Before later execution approval, review and refresh the policy's
expiry and bind the actual implemented evaluation tooling through its human
acceptance path. Broader guided planning remains tracked in
[#5331](https://github.com/aws-e/adp/issues/5331); post-acceptance amendment
interaction is owned by [#5329](https://github.com/aws-e/adp/issues/5329).
