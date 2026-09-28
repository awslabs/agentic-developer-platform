# Gateway persona admission evidence

Date: 2026-09-25. Scope: actual admission, TaskStore/DynamoDB fixture transactions,
TaskRuntime/bootstrap and shared SDK persona parser. Model readiness, IAM and
external inference remain fixtures; no live personas were enabled.

`task_harness.py` validates the shared persona shape, canonical definition digest,
composed instructions, pinned skill sources, capability requirements and Task
model/deadline/limit bindings. The server-owned catalogue path never comes from
Task input. The authoritative persona compatibility registry is still required.
The current capability ceiling permits report publication only; executable
persona definitions cannot obtain authority through a configuration file.

Tests verify:

- missing catalogue or unauthorized/unregistered persona fails before reservation;
- changed instructions, digests, skill sources, duplicate persona entries,
  boolean schema versions and unimplemented required capabilities are refused;
- admission commits the snapshot; replay reuses the task and its single reservation
  even when the catalogue subsequently becomes invalid;
- bootstrap returns the admitted snapshot without reopening the catalogue;
- removal or mutation of instructions/capabilities breaks the protected grant digest;
- a transaction checks exact snapshot equality and absence, rejecting removal or
  injection after the operation's initial read;
- combined persona/input frames exceeding 65,536 bytes are refused before handoff;
- exported contract schemas remain synchronized and the SDK-generated bootstrap
  fixture is accepted by both gateway and TypeScript validators.

The final gateway/Task regression batch passed 253 tests, including frozen
snapshot, turn admission, model, runtime, storage and model-binding coverage. The Task contract checker passes all
383 checks. The shared harness/IPC suite passes 38 tests. The actual SDK/worker
fixture passes eight scenarios in a newly created virtual environment with only
its declared dependencies. Focused Ruff and TypeScript checks pass.

CI on the previous commit exposed an unavailable `pip` after HOME isolation.
The fixture job now creates an explicit virtual environment and installs all
worker import dependencies, including cryptography. Isolation was preserved.
An unrelated frontend CI assertion in AgentModels failed; its complete 30-test
file passed locally without source changes. New-head CI still needs observation.

All local tests used temporary BG_CONFIG_DIR, ADP/HOME/XDG and credential stores.
Live authentication fingerprints remained unchanged across this increment.
Real turn/model integration additionally verifies that protected Codex runs can
commit autonomous continuations, then stop at the frozen persona limit. Missing
harness metadata, non-admitted effort and an expired persona deadline are refused
before operation claims, budget reservation or provider effects. The worker/SDK
fixture proves that a one-operation persona budget prevents even a report-repair
call, despite a broader Task limit.

Live model qualification, registry/command rollout, executable tool brokers,
full OTLP export and actual persona story acceptance remain outstanding.
