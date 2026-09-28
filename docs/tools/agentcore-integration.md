# AgentCore tools: Task integration contract

Issue #6635 introduces **Code Interpreter**; sibling tools must reuse the Task
attempt and operation envelope rather than define another identity format. This
is an implementation contract, not evidence of a deployed or qualified AWS
resource. The existing browser worker path remains active by default.

## Transport and authority

`POST /tools/code-interpreter` is an AWS_IAM method on the existing REST API,
backed by the separately deployed cyber tools Lambda. The worker's host-side
`ADP_TASK_TOOL_ROUTES` must map **each** enabled permission to its HTTPS URL:

```json
{"code_interpreter.start":"https://API.execute-api.REGION.amazonaws.com/STAGE/tools/code-interpreter","code_interpreter.execute":"https://API.execute-api.REGION.amazonaws.com/STAGE/tools/code-interpreter","code_interpreter.result":"https://API.execute-api.REGION.amazonaws.com/STAGE/tools/code-interpreter","code_interpreter.file":"https://API.execute-api.REGION.amazonaws.com/STAGE/tools/code-interpreter","code_interpreter.close":"https://API.execute-api.REGION.amazonaws.com/STAGE/tools/code-interpreter"}
```

The built `agent-task-cyber` SDK exposes `code_start`, `code_execute`,
`code_result`, `code_file`, `code_close` only if the frozen Task grants contain
their respective `code_interpreter.*` permission. The Task host sends a generic
`tool.request` with its run/attempt and a UUIDv4 `operation_id`, signs the
configured URL, verifies receipts and publishes Task artifact references and
factual progress. The Lambda separately checks the worker IAM role and calls
the platform `tool-authorize` endpoint *for each operation* and at the provider
boundary. The shared gateway `tool-authorize` cleanup exception admits only
`code_interpreter.close` in addition to its existing `*.cancel_jobs` path;
future sibling tools must not widen this exception implicitly. Effective
permission is the intersection of current principal,
frozen Task grants and current persona tools. No client tenant or provider ID
is accepted. `close` and host-only `cancel_jobs` use cleanup authorization, but still checks ownership of
the already-created session and attempt. Without an explicit policy/persona
grant, operations are refused; setting the feature flag alone grants nothing.

Request JSON is strictly `{schema_version:"1.0",attempt:{run:{task_id,
invocation_id,generation},runtime_attempt_id},operation_id,operation,payload}`.
`task_id` has the `tsk_` UUIDv4 form; all IDs are lowercase UUIDv4 except the
opaque 64-hex session handle. Unknown fields or operations return HTTP 422
`{"code":"invalid_request","message":"Invalid cyber tool request"}`.
Operations and exact payload fields:

| Operation / grant suffix | Payload | Response result |
| --- | --- | --- |
| `start` | `{}` | `{session_id, status:"active"}` |
| `execute` | `{session_id, code, language:"python"}` | `{status:"pending",execution_id}` |
| `result` | `{session_id, execution_id}` | `{status:"pending"}` or provider result |
| `file` | `{session_id, path:"/tmp/FILE"}` | `{path,contents}` |
| `close` | `{session_id}` | `{status:"closed"}` |

`result` takes the **execute operation ID** as `execution_id`. Successful
responses are `{"schema_version":"1.0","task_id":"tsk_...",
"operation_id":"UUID","operation_status":"confirmed|pending|unknown",
"result":{...},"artifact":{"artifact_id":"art_...",
"content_type":"application/json","content_sha256":"64 hex","byte_length":N}}`.
Only confirmed results carry an artifact. `start`, `file`, completed `result`,
and explicit `close` publish the same immutable JSON content through the existing Task
artifact API; results from Python are untrusted evidence, not a safety verdict.
For example, after `start`, call `execute` with
`{"session_id":"HANDLE","code":"print(sum([2, 3]))","language":"python"}`;
poll `result` with `{"session_id":"HANDLE","execution_id":"EXECUTE_UUID"}`
until confirmed, then `close`. File results retain bounded MCP result blocks (including text/resource content); binary bytes are base64 encoded in JSON artifacts. File outputs must be written by code under
`/tmp/` and explicitly fetched with `file`; no direct S3 capability is exposed.

Malformed JSON, unsupported language (including JavaScript/TypeScript until
qualified), unknown payload keys, non-`/tmp/` paths and code over 8192 UTF-8
bytes return 422. Unowned session/execution returns 403. Task end or deadline
returns 409; disabled capability or missing provider configuration returns 503.
Provider ambiguity returns `unknown` or HTTP 503 `outcome_unavailable`, never
an implicit retry of the uncertain send. Task receipts are stored under the
existing encrypted, deletion-protected operation table. Execution/read claims share the Task
cyber operation counter (128); owned cleanup bypasses that quota and must survive SDK retries. `start` uses a
deterministic provider client token; only the server stores the provider session
ID. AWS automatic retries are disabled; uncertain executions are never resent. `execute` uses AgentCore `startCommandExecution` (`python -c` with escaped
code), then read-only `getTask` polls: the API does not wait for analysis to
complete. A pending/ambiguous start is **not** blindly replayed. At most one
session may start per Task attempt; byte-identical executions deduplicate even
across different operation UUIDs. Reads are
bounded to 20 KB provider responses and 24 KB Task JSON artifacts; unsupported
provider response shapes fail closed. Stop uses a stable client token. Sessions
expire at the provider after the smaller of the remaining Task deadline and
900 seconds even if the worker dies; the Task host automatically invokes `code_interpreter.cancel_jobs` through the
configured start endpoint during finalization/cancellation. This fences new work,
closes the attempt-owned session and returns pending for an ambiguous start until
provider expiry plus a 60-second request allowance. Cleanup works after revocation,
deadline and quota exhaustion. Worker death remains bounded by provider expiry;
operators may reconcile unknown claims earlier using the procedure below. Provider session `name` is `adp-` followed by
SHA-256 of `task_id:runtime_attempt_id:start_operation_id` (the same opaque
handle returned on success). A later operator with `ListCodeInterpreterSessions`
and `StopCodeInterpreterSession` permissions can match the `READY` session's
name to an owned start claim, stop it with a stable token, and mark the record
for manual settlement; never stop an unmatched session. If listing is denied
or ownership cannot be proven, allow the bounded provider expiry instead.
Do not purge operation claims while their replay window remains open.

## Deployment and operator handoff

The provider resource is **operator-owned**, not created by the Task stack:
create a dedicated AgentCore Code Interpreter using `CreateCodeInterpreter`
with `networkConfiguration.networkMode=SANDBOX`, an execution role with **no
platform IAM grants**, and no filesystem mounts or certificate injection.
Confirm its actual ID, ARN, region, access and quota via `GetCodeInterpreter`
before inserting `code_interpreter_identifier` and `code_interpreter_arn` into
the cyber tools Terraform module. The image requires boto3 >=1.43.103 for the
direct AgentCore APIs. Use an immutable image digest. Both `enabled` and
`code_interpreter_enabled` default false. This module owns only the new IAM
route, Lambda permission and scoped provider policy; the shared REST API owner
must publish the new API deployment/stage. Register the Lambda role as an
authorized Task tool service; explicitly grant only the five permissions in
current policy and persona and frozen Task grants. Follow the existing
`modules/domain-apps/cyber/tools/infra/README.md` build, state isolation,
plan/apply and rollback instructions. Confirm the target account and obtain
operator approval under `docs/adp-platform-deployment/deploy-with-agent.md`
**before** any live apply. No live AWS operations are part of this PR.

Smoke qualification (later authorized operator): use a short Task containing
a fixed evidence table, grant only the required operations, execute Python
aggregation, poll the exact execution ID, verify artifact SHA-256 against
downloaded Task artifact content, fetch one `/tmp/` file, and close the
session. In the same approved account test network egress, credential probing,
traversal, oversize output, revoke/cancel, cross-Task reuse and ambiguous
provider responses against real AgentCore. Bound cost to **one** 900-second
session and one short analysis before broader trials; review current AWS
pricing and quotas first. AgentCore CPU/memory/session metrics and actual
charges must be read from account telemetry/billing after execution; close confirms session shutdown; it does **not** settle CPU/memory usage or an AWS charge. The Task
model usage ledger does not settle provider charges. Record session/operation
IDs, time and billing receipts without code, secrets or credentials. Stop
owned sessions, wait for expiry and reconcile unknown claims before deletion.
Rollback by setting `code_interpreter_enabled=false` and withdrawing grants
and host route mapping (retain `close` for owned cleanup); do not destroy the
shared login stores, existing browser path or operation table. Terraform does
not auto-publish an API stage and merging this document does not deploy it.

## Review corrections

The SDK tracks pending/uncertain code executions and refuses a final report until
they reach a terminal result or their owned session is confirmed closed. Completed,
failed and cancelled computations publish factual progress. Existing browser and
Common Crawl transports are unchanged. The service test workflow uses the same
boto3 version as the Lambda image and validates requests against its service model;
these offline checks do not establish real-provider execution or sandbox isolation.
