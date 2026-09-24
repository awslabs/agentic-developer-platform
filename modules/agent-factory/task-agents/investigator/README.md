# agent-task-investigator

The first independent task agent for the ADP Task API (issue #5798, T5).

It investigates evidence a caller supplied — logs, configuration, structured
inputs — and returns a report whose every finding cites that evidence. It reaches
a model only by asking its host, and it has no GitHub installation, token,
repository, comment, check or pull-request dependency of any kind.

Implements the accepted design at revision
`b5761a4a2502aceaa9133afef552b567a19cb46e`
([`docs/task-api/implementation-design.md`](../../../../docs/task-api/implementation-design.md)),
sections 3 and 8.

## What it is, and what it deliberately is not

This is a **separate Node.js package**, not a persona file, a prompt variant, or a
branch inside the existing Claude or Codex agents. It ships in the same worker
image with its own build and its own lockfile, and it declares **zero runtime
dependencies** — so nothing it needs can perturb the versions the existing agents
depend on, and legacy execution never initializes anything of its own.

It has no checkout, no URL fetching, no shell execution, no MCP tools, no
infrastructure mutation and no approval step. Those capabilities are *absent*
rather than disabled: there is no code path here that reads a credential, opens a
socket or spawns a process, which is asserted directly against the shipped sources
in [`src/independence.test.ts`](src/independence.test.ts).

## How the host runs it

The host launches the built entrypoint with the `--embedded` flag from the
design's command allowlist:

```
node /app/task-agents/investigator/dist/index.js --embedded
```

| | |
|---|---|
| Persona | `agent-task-investigator` |
| Command | `node /app/task-agents/investigator/dist/index.js --embedded` |
| Image path | `/app/task-agents/investigator/dist/index.js` |
| Runtime | Node 24 in the worker image (package requires >= 22) |

> **Coordination note.** Registering that persona → command mapping in
> `modules/agent-factory/agent-worker-image/entrypoint.py` is **T4's change
> (#5797)**, which owns the shared entrypoint. This story deliberately does not
> edit that file; it owns the binary the mapping points at, and the additive
> Dockerfile stage that puts it there. Until T4 lands, the agent is reachable by
> running the command above directly.

## The conversation with the host

One JSON object per line over stdin/stdout, `protocol_version: 1`. **Stdout is
protocol-only** — diagnostics go to bounded stderr, and `console.log` is rebound
to stderr at startup so a stray call cannot corrupt the stream.

```
host ──> start          task content, artifacts, limits — never a credential
     <── ready          advertises exactly: input, cancel
     <── progress       authored updates, emitted as work happens
     <── model.request  messages + token bound only; never a model or endpoint
host ──> model.result   confirmed | pending | unknown | rejected
     <── input.required a clarification question for the caller
host ──> turn           follow-up input, consumed once by command id
host ──> cancel         intentional: true
     <── result         the report  │  cancelled  │  error
```

The normative shapes are
[`process-protocol.schema.json`](../../../../docs/task-api/contracts/v1/schemas/process-protocol.schema.json);
[`src/protocol.ts`](src/protocol.ts) validates against them in both directions.

### Frames are validated on the way out, not just on the way in

The contract forbids specific fields, and those rules are the containment
boundary the whole persona rests on:

| Frame | Cannot carry | Because |
|---|---|---|
| `start` | AWS keys, `github_token`, `gateway_token`, `api_key` | the child receives task content only |
| `model.request` | `model`, `model_id`, `endpoint`, `region`, `api_key` | the host resolved the binding at admission |
| `progress` | `percent_complete`, `reasoning`, `thinking` | no fabricated progress, no private deliberation |
| `result` | `total_usd`, `turns_used` | the child reports content; the host owns the ledger |

Outbound frames are refused **before** they are written, so a coding mistake here
cannot put a forbidden field on the wire and leave the host as the only thing that
notices.

## What it does with the evidence

Four stages, per design section 3:

1. **Evidence inventory** — names what was actually supplied (how many artifacts,
   how many bytes, which inputs). Authored from real content, so the first update
   is substantive rather than a placeholder greeting.
2. **Analysis** — asks the host for a model call and grounds the result.
3. **Clarification** *(only when nothing could be grounded)* — asks the caller one
   question, and only when a turn remains in which to use the answer.
4. **Synthesis** — reports the shape of the conclusion before emitting it.

### Grounding: why an unsupported claim is demoted rather than dropped

Every finding must cite supplied evidence. When a citation does not resolve to
something the caller handed over, the claim is **moved to `uncertainties`** —
not deleted, and not fatal.

Deleting it would hide that the model believed something. Failing the whole run
would throw away the findings that *are* properly grounded. Demotion keeps the
observation visible while stripping the authority a citation confers, so what the
caller reads is "this was suggested but nothing supplied supports it" — which is
true, and actionable.

Output that is not a report at all is different: there is nothing to ground, so
the run **fails**. There is deliberately no lenient reconstruction that could
assemble a plausible report from unparseable output, because that is precisely how
a failed run gets reported as a successful one.

### Honest unknowns

An `unknown` model outcome stops the run. It is not retried, not treated as an
answer, and never recorded as zero cost — the first call may already have been
billed and executed, so resending would risk doing the work twice while reporting
it once. The host settles the ambiguity from its own records.

## Cancellation

Cancellation must assert `intentional: true`. It travels as a typed
`ControlCancelledError` carrying a structural marker, because retry wrappers
classify failures by matching error text for words like `aborted` and `timeout` —
a cancellation travelling as an ordinary `Error` would match those patterns and be
retried, so a deliberate stop would start a fresh attempt.

[`src/control.ts`](src/control.ts) is a task-local adapter compatible with the
neutral `ControlRuntimeAdapter` semantics in
`modules/agent-factory/agent/src/control-runtime.ts`. Its types are **narrowly
copied with provenance rather than imported**, as design section 8 permits:
importing would put this package on the Claude agent's dependency tree and defeat
the isolation described above. v1 advertises only `input` and `cancel`, and
reports pause as unavailable rather than pretending to honour it.

## Development

```bash
npm ci --include=dev   # --include=dev: the only dependencies are the TS toolchain
npm test               # tsc, then node --test over dist/
```

Tests consume the published contract fixtures under
`docs/task-api/contracts/v1/fixtures/` **unchanged**, from their canonical
location. That is deliberate: this package hand-writes its validation instead of
carrying a JSON Schema library, so the shared fixture corpus is the only thing
binding the validator to the contract. Editing a fixture to make a test pass would
silently remove that binding.

| Suite | Proves |
|---|---|
| [`protocol.test.ts`](src/protocol.test.ts) | every published frame fixture is accepted or refused as declared |
| [`investigator.test.ts`](src/investigator.test.ts) | stage behaviour, limits, input consumption, cancellation, grounding |
| [`independence.test.ts`](src/independence.test.ts) | no SDK dependency, no credential read, no network, no GitHub |
| [`embedded.test.ts`](src/embedded.test.ts) | the real process over real stdio, with no credentials and an active network-denial observer |
| [`test/run-image-smoke.sh`](test/run-image-smoke.sh) | built-image useful, waiting/cancelled, malformed-output and legacy-package fixture |

[`EVALUATION.md`](EVALUATION.md) maps each of T5-AC01 … T5-AC05 to the test that
proves it, and records why those commands are not yet registered in the
evaluation manifest.

`embedded.test.ts` proves progress is **incremental** rather than buffered by
delaying the host's model reply and asserting the timing gap between the first
progress frame and the result. Inspecting output after exit cannot distinguish the
two, and the fixed limits are explicit that buffered final stdout does not satisfy
the progress requirement.


## Built-image fixture

After building the worker image, run the image-specific acceptance fixture:

```bash
docker build -t adp-agent-runtime:t5 -f modules/agent-factory/agent-worker-image/Dockerfile .
modules/agent-factory/task-agents/investigator/test/run-image-smoke.sh adp-agent-runtime:t5
```

The runner uses the canonical T0 start fixture unchanged, isolates the container
with `--network none`, verifies zero observed network attempts, and records the
source SHA, fixture hash, image digest and outcomes. It also checks the packaged
Claude and Codex binaries and their unchanged legacy command mappings. Package
unit tests are not a substitute for this built-image lane.
