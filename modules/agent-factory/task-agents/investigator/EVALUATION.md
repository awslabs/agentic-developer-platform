# Evaluation commands for T5-AC01 … T5-AC05

Runnable commands proving each T5 acceptance criterion, ready for registration in
[`docs/task-api/evaluation-manifest.json`](../../../../docs/task-api/evaluation-manifest.json).

Run the package lane from the repository root. `npm ci` may download the locked
build toolchain; the tests themselves use no AWS account, GitHub credential or
external service. The useful process fixture installs an active network-denial
observer and asserts that it saw zero attempts.

```bash
cd modules/agent-factory/task-agents/investigator && npm ci --include=dev && npm test
```

Run the built-image lane against the exact image under evaluation:

```bash
docker build -t adp-agent-runtime:t5 -f modules/agent-factory/agent-worker-image/Dockerfile .
modules/agent-factory/task-agents/investigator/test/run-image-smoke.sh adp-agent-runtime:t5
```

The runner starts the container with `--network none` and a read-only root,
executes the useful, waiting/cancelled and malformed-output fixtures, verifies
that the Claude/Codex binaries and unchanged legacy command mappings remain in
the image, and prints source SHA, canonical start-fixture hash, image digest and
actual outcomes as JSON. A missing container runtime or absent image is `BLOCKED`,
not package-lane PASS evidence.

| Criterion | Evidence | Where |
|---|---|---|
| **T5-AC01** — useful task completed with GitHub credentials absent and access denied; zero GitHub requests | the real process receives only `PATH` plus the test observer, which denies network APIs and records zero attempts; the image lane additionally uses `--network none` | `embedded.test.ts` → "completes a useful task with no credentials and observed network denial"; `test/network-deny-hook.cjs`; `test/image-smoke.mjs` |
| **T5-AC02** — substantive progress reaches the host before execution finishes; no raw reasoning or secrets | the host's model reply is delayed and the timing gap between first progress and result is asserted, so buffered output cannot pass; forbidden fields are checked on every emitted progress frame | `embedded.test.ts` → "progress arrives before the result rather than buffered at exit", "at least two distinct authored progress frames reach the host"; `investigator.test.ts` → "progress carries no private reasoning and passes frame validation" |
| **T5-AC03** — image still contains unchanged Claude and Codex packages (syntax and selector smoke; live provider execution is a separate qualification); legacy execution does not initialize task-only dependencies | package tests prove zero runtime dependencies; the built-image runner checks syntax of all three compiled binaries, absence of a task-only `node_modules`, and unchanged Claude/Codex command selection | `independence.test.ts`; `test/image-smoke.mjs`; `test/run-image-smoke.sh` |
| **T5-AC04** — respects scoped model/credential/budget limits; consumes input and cancellation per the frozen contract | model/progress/synthesis race tests require follow-up consumption before completion, reject budget-exhausted completion, and fence cancellation; caller limits are clamped to fixed ceilings, follow-up commands are consumed once, and cancellation interrupts an outstanding model wait without another model outcome | `investigator.test.ts` → scoped limit/input tests; `embedded.test.ts` → "typed cancellation yields a cancelled frame and no result"; `protocol.test.ts` → published fixtures and nested schema bounds |
| **T5-AC05** — failure and malformed final output cannot be reported as successful completion | unparseable output, a missing summary, an unknown provider outcome and a rejected grant each end the run with an `error` frame, a non-zero exit and **no** `result` frame | `embedded.test.ts` → "malformed model output ends the run with an error, not a result", "an unknown model outcome ends the run as model_outcome_unknown"; `investigator.test.ts` → the T5-AC05 suite |

## Registration is blocked on the wave gate, not on this package

These commands are **not** yet registered in the evaluation manifest, and that is
deliberate rather than an oversight.

`scripts/task-api/check-contracts.py` asserts that *"only the V0 validator and T0
criteria are runnable at this wave"*: it derives the expected runnable set as
exactly the `V0-*` and `T0-*` criteria. Flipping any `T5-AC0x` entry to
`runnable` therefore fails that check — verified directly:

```
FAIL only the V0 validator and T0 criteria are runnable at this wave
     runnable: [T0-AC01..04, T5-AC03, V0-01..08]
1 of 320 checks failed.
```

Both files are owned by T0 (#5793) and are currently under V0's (#5821)
independent evaluation, so editing either to accommodate this story would mean
changing another story's deliverable and breaking a check that passes today.

**Requested coordination:** T0 or the epic owner advances the wave gate (or widens
the expected-runnable set), then the `T5-AC0x` entries are flipped from
`not_implemented` to `runnable` with the command above. Nothing in this package
needs to change for that.

## Also note

- End-to-end acceptance of the full task path belongs to **V2 (#5803)** and **V4
  (#5805)**; this story provides the agent-side half plus the deterministic
  stand-in host its tests drive.
- Durable reporting and control persistence are **T6 (#5799)** and **T7 (#5800)**.
  The frames this agent emits are validated against the frozen contract, but their
  durable persistence is not asserted here.
- Registering the persona → command mapping in `entrypoint.py` is **T4 (#5797)**;
  see the README for the exact command.
