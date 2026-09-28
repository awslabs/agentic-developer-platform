# Accepted implementation-story handoff

**Status:** Design accepted and plan refresh authorized on 2026-09-24. Publication is recorded in [waves.md](waves.md#plan-publication-status).
**Design:** [implementation-design.md](implementation-design.md).

Apply the following scope changes with the immutable accepted design revision.
Preserve native issue relationships, original acceptance IDs and the existing
unanswered flow gate. This file is publication input, not execution approval.

## Replacement T0 title

Task API T0: Implement schemas, fixtures and conformance checks

## Replacement T0 body

Part of #5792. Implement the Task API design accepted before engine execution.
Use the immutable design revision bound in the execution handoff, including
`docs/task-api/implementation-design.md`, `README.md` and `validation.md`.

### Outcome and ownership

Implement machine-readable schemas, reusable fixtures and runnable conformance
checks for the agreed Task API contract. Own the versioned evaluation manifest
and coordinate shared contract-code changes. This is code development against an
existing design; it does not choose architecture, model transport, authentication,
storage layout, command semantics, limits or rollout strategy.

### Dependencies

The complete design has been reviewed and its source revision pinned before this
story is dispatched. A conflict or missing requirement returns to that design
review with the affected O/D/acceptance ID and evidence. Do not initiate an AI-DLC
inception/design workflow, silently amend the architecture or relax a threshold.

### Deliverables

- Implement JSON Schemas and positive/invalid/legacy fixtures under
  `docs/task-api/contracts/v1/` for the public API, errors, envelope, bootstrap,
  process protocol, events, turns, commands, model receipts and terminal results.
- Implement `scripts/task-api/check-contracts.py`; reject invalid fixtures and
  contradictory cross-component identity/state contracts. Exercise a complete
  submit-to-result trace, input/reconnect/cancel traces and the crash matrix.
- Implement `docs/task-api/evaluation-manifest.json`, mapping every T0–T8 and
  V0–V5 criterion to its owner, wave, evidence lane, fixture responsibility and
  command status. Only the V0 validator is runnable at this stage; register later
  commands as unimplemented until their component owners supply them.
- Carry the accepted O1–O8 answers, source evidence and downstream owners from
  the design into the conformance baseline. Encode the fixed numeric thresholds
  before measuring implementations. Preserve all legacy compatibility criteria.

### Original acceptance criteria (unchanged)

- [ ] **T0-AC01:** Each O1-O8 entry has a concrete answer, evidence and downstream owner; no placeholder or fake GitHub dependency remains.
- [ ] **T0-AC02:** Versioned valid/invalid fixtures exercise one full submit-to-terminal flow including input, reconnect and cancellation, and are usable by independent implementation agents.
- [ ] **T0-AC03:** Legacy compatibility, task state integrity and same-queue rollout have executable proof plans; numeric acceptance limits are fixed before measuring implementations.
- [ ] **T0-AC04:** Any departure from D01-D18 is called out as a proposed amendment for owner decision rather than silently implemented.

### Evaluation and completion

V0 #5821 independently executes the validator against this exact source revision
and checks conformance to the accepted design. Record design/tested source SHAs,
fixture and runner revisions, collected test counts, actual commands and results
for every required criterion. Missing tests or evidence remain BLOCKED/NOT RUN.
Implementation defects return to T0; design contradictions return to the design
session. V0 cannot approve an architecture amendment.

Wave 1 is **Contract implementation**. Its purpose is to implement the agreed
design as schemas, fixtures and runnable checks before component development.
This story deploys nothing and does not enable task traffic or engine execution.

## Required additions to T1–T8 handoffs

Every assignment must carry this instruction with the real immutable design
revision from the accepted proposal (not a floating main link):

> Implement the Task API contract at the accepted design revision. Architecture
> and acceptance decisions have already been made outside the engine. Follow the
> assigned code ownership and unchanged acceptance criteria. Material conflicts
> or missing design decisions return for review with evidence; do not redesign
> through this story or start AI-DLC plan authoring. Local code/test structure is
> an implementation choice. Report actual code, evaluation and deployment states
> separately.

### Scope clarifications to attach to affected issues

| Issue | Implementation obligations from the accepted design |
|---|---|
| T1 #5794 | Exact task record namespaces, atomic metadata/events/turns, sparse recovery index, retention and protected transaction fences. |
| T2 #5795 | Cognito scopes and canonical alias validation through gateway admission, producer-bound forwarding and isolated Lambda/API integration. |
| T3 #5796 | Task service policy/admin adapter, GitHub-free bootstrap/model authority, task IAM boundary, recoverable publication and separate task-recovery schedule. |
| T4 #5797 | Incremental host/child protocol, separated runtime identities, scoped gateway calls, typed cancellation and independent task finalization. |
| T5 #5798 | Independent TypeScript investigator, supplied-evidence workflow, host-mediated models, useful progress/results and neutral runtime adapter conformance. |
| T6 #5799 | Owner-authorized snapshots/SSE/artifact routes, ordered persistent reports and turn/model-operation storage with T1/T3. |
| T7 #5800 | Once-per-command transcript/turn consumption, honest model-handoff receipts, crash reconciliation and cancellation integration. |
| T8 #5801 | External example, real readiness inventory, bounded live/regression tooling and deployment/rollback handoff; no assumption of working automatic deployment. |

## V0 and epic wording

V0 retains all eight existing criteria and evaluates implementation conformance
to the design reviewed here. It must not select or approve O1–O8 itself. The epic
and first wave should explain that design precedes engine execution; the six
waves then implement and evaluate it. Deployment requires supported bindings,
working runners and separately accepted target authority.

Use the actual CLI preview/save response when publishing the proposal. Do not
invent a version, execution hash, evaluator runner or accepted policy. The source
proposal alone does not authorize engine execution.
