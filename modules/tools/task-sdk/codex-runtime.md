# Codex Task runtime

`runCodexTask` runs the real pinned Codex CLI, with a private empty working directory,
HOME, CODEX_HOME and configuration stores. The only credentials given to that child
are two random process-local loopback tokens. The Task host retains model/provider,
repository and publication authority. Shell/unified-exec and web search are disabled;
only the four repository MCP tools have explicit runtime approval.

`responses-proxy.mjs` adapts the supported Responses text/function subset into the
existing Task-host Messages contract. The host's canonical model binding and usage
remain authoritative: running Codex does **not** imply that an OpenAI model was
used. Publish the engine as Codex and the actual backend/model separately. The
coding persona requires its own `task-codex-responses-compat-v1` readiness evidence;
Claude Messages probe evidence does not certify this transport.

Supported input includes plain messages, explicit full conversation history,
flat/namespaced function calls and textual function outputs. Unsupported reasoning,
images, previous-response IDs, stored/background sessions and hosted server tools
fail closed. Codex's built-in tools are omitted from the model request; generated
function calls are checked against the exact repository MCP allowlist before Codex
can execute them. No tool call can authorize a shell, arbitrary filesystem access,
network access or publication. Snapshot edits stay in the shared in-memory editor.

The proxy returns correlated Responses SSE from a *confirmed* host result. It does
not claim provider token streaming. Incomplete host output stays incomplete. A
repeated normalized request shares its original host operation and result; an
uncertain model operation fails the bridge and stops Codex without automatic retry.
Model request count, frame bytes, process diagnostics and wall time are bounded.
The runner returns only a report accepted by `submit_patch`, after Codex exits
successfully. It kills the process group on cancellation/deadline and removes its
private directories.

Run the transport checks with all normal test configuration isolation. The optional
`CODEX_BRIDGE_TEST_BIN` test executes the real CLI against a fake loopback model and
real MCP transport, with no inference or provider credentials. It was checked with
the worker-pinned Codex 0.157.0 binary. This is runtime/transport evidence, not live
model-quality or hosted-worker acceptance.

Contract sources:

- [Responses creation and tool/input/output contract](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [Codex provider, MCP and feature configuration](https://developers.openai.com/codex/config-reference)
