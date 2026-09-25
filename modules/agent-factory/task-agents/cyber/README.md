# Task cyber SDK driver

`node dist/index.js --embedded` runs the pinned Claude Agent SDK as a trusted
orchestration process under the Python Task host. It requires
`ADP_TASK_NETWORK=host-mediated-sdk`. It has no provider, AWS, GitHub or gateway
credential. This driver uses loopback networking; the existing investigator's
network-denied execution boundary remains independent.

The SDK receives no built-in tools, filesystem settings or persisted sessions.
Each run uses an empty temporary HOME and working directory, removed at cleanup,
so legacy worker-home credentials and project configuration are not discovered.
Only exact in-process `mcp__cyber__*` tools are approved by the SDK permission hook.
They expose allowlisted packaged skill text, authored progress, caller input,
structured reports and six Task-host cyber operations. Legacy shell, GitHub and
direct AWS skill instructions are explicitly superseded by Task broker operations.
The worker image copies only SKILL.md files from existing cyber skills.

The loopback adapter normalizes SDK Messages requests into bounded Task IPC. The
host and gateway select the model, authorize every turn and retain usage/receipts.
Cyber operations go from the host to the domain cyber tools service at the exact
HTTPS URL configured by `ADP_CYBER_TOOLS_ENDPOINT`, ending in `/tools/cyber`.
The route uses AWS_IAM authorization through shared `modules/tools` infrastructure.
The host signs calls with SigV4 and includes its workload and run credentials;
the SDK receives neither these credentials nor authority to select the endpoint.
There is no fallback to the former gateway `/task/cyber` route. Missing or invalid
service configuration fails the operation, including downstream cleanup. Model,
control and artifact requests continue to use their existing generic Task routes.
Unknown model outcomes abort the SDK; they are never automatically retried as new
paid work. Broker and model operations serialize, job submissions deduplicate,
and queued jobs require paced polling before a report is accepted. Unsupported or
unavailable backend evidence stays uncertainty. Report findings require verified
provenance references; SDK transcript/reasoning is never public progress.

Public controls initially advertise `input` and `cancel`. Cancellation aborts the
SDK and awaits its cleanup; the host owns downstream job cancellation and terminal
receipt settlement. There is no new public pause/resume API.

Run `npm ci`, `npm test` and `npm run build`. Tests include a real SDK subprocess
using a scripted local model adapter and in-process MCP tool, with no live model
or cyber backend calls. Live backend correctness requires host/gateway integration
verification separately.

The full wire harness is `python -m pytest test/test_host_sdk.py` from this
package, after building the investigator dependency and this package. It launches
the real Python Task host and SDK child with scripted TaskRunClient receipts,
checks broker artifact provenance in the durable final report, and verifies an
unknown model outcome cannot trigger another model call.

## Relationship to the existing remote-control implementation

The existing remote-control implementation was inspected for this Task adapter:
[`ClaudeControlAdapter` and `AttemptInputChannel`](../../agent/src/harnesses/claude-control.ts),
[`ControlRuntime`](../../agent/src/control-runtime.ts), and
[`PauseGate`](../../agent/src/pause-gate.ts). The cyber package does **not** import
or instantiate these classes. Describing them as reused code would be incorrect.

`AttemptInputChannel` delivers steering and annotations through an open SDK
`AsyncIterable`. Its adapter translates those messages with
`origin: {kind: 'human'}` and `shouldQuery`, and registers transport attempts with
the existing remote-control runtime. Task callers can be service principals.
Task input already has a different durable boundary: the host commits the caller
command to a logical turn, sends its assigned turn ID, and the waiting
`request_input` MCP call returns the input as a tool result. The next model
request uses that assigned turn ID. Adding the legacy channel would introduce a
second input path without supplying the required Task command/turn receipts.

`PauseGate` can prevent new tool admission, but a returned MCP receipt may leave a
remote cyber job running. The remote-control Claude hooks correctly treat opaque
MCP work as unobservable without additional evidence. Task completion and cancel
therefore use the host broker's job receipts and downstream cleanup confirmation.
There is no public Task pause/resume capability to implement here, so importing a
pause state machine would not add a supported control.

The concrete shared code is the existing
[Python Task host](../../agent-worker-image/lib/task_host.py), its
[command/turn control](../../agent-worker-image/lib/task_commands.py), and the
[investigator IPC/artifact validators](../investigator/src/protocol.ts) imported
by the embedded entry point. SDK execution uses the same pinned Claude SDK
version and its native `AbortController`/query `close()` primitives. The driver
owns the query and closes it once during cleanup. No legacy retry wrapper is
used: an unknown paid-model outcome must retain its original receipt and abort.

Tests cover Task input replay/assigned-turn binding, cancellation of pending
input, exact tool admission, cleanup, and original unknown-outcome preservation.
The full host/SDK harness verifies durable model-turn request digests, broker
artifact provenance in the final report, cleanup before finalization, and no
second model call after an unknown outcome. These are the supported Task control
guarantees; they do not claim that legacy steering or pause/resume is available.
