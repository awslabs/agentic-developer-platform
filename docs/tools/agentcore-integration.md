# AgentCore tools: Task integration contract

**Current ownership (#6671):** `modules/tools/agentcore/` owns the general-purpose
services and infrastructure. The Cyber-specific deployment statements later in
this historical review record describe the pre-extraction setup; use
`docs/tools/shared-agentcore-tools.md` for current build, state migration,
non-Cyber grants, smoke and rollback instructions.

Issue #6635 introduces **Code Interpreter**; sibling tools must reuse the Task
attempt and operation envelope rather than define another identity format. This
is an implementation contract, not evidence of a deployed or qualified AWS
resource. The existing browser worker path remains active by default.

## Transport and authority

`POST /tools/code-interpreter` is an AWS_IAM method on the existing REST API,
backed by the separately deployed shared AgentCore tools Lambda. The worker's host-side
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


## Browser HTTP tool (#6636)

Issue #6636, parent #6633. `POST /tools/browser` is additive and disabled by
default. Existing Tasks use `local:cyber_tools.task_browser.TaskBrowser`;
Common Crawl remains HTTP. `/tools/cyber` still rejects browser operations.
The separate shared API stage owner publishes routes after review, not merge.

## Shared Task contract (1.0)

Initial callers are IAM-authenticated Task workers; a model chooses an allowed
registered tool name, never an endpoint. All `/tools` services reuse
`TaskAttemptBody` and `TaskAuthorityClient` from `modules/tools/adp_tools`.
Gateway requires AWS_IAM, passes the verified worker ARN, and the service
allowlists exact roles and forwards only the run credential/workload proof to
`tool-authorize`. The authoritative response binds Task, attempt, tenant,
principal and scope; never take tenant, principal, caller ARN or endpoint from
client JSON or headers. Grants are the intersection of current principal policy,
Task-frozen grants and current persona. Cleanup is restricted to owned sessions.

`POST /tools/browser` request (every object rejects unknown keys):

```json
{"schema_version":"1.0","attempt":{"run":{"task_id":"tsk_00000000-0000-4000-8000-000000000001","invocation_id":"00000000-0000-4000-8000-000000000002","generation":1},"runtime_attempt_id":"00000000-0000-4000-8000-000000000003"},"operation_id":"00000000-0000-4000-8000-000000000004","operation":"browser_start","payload":{"url":"https://example.org/","scope":"host","profile":"desktop"}}
```

Task IDs are `tsk_` plus lowercase UUIDv4; invocation/attempt/operation IDs
are lowercase UUIDv4 and generation is integer 1–64. The decoded request is
at most 65,536 bytes. Other AgentCore stories may use different operation and
payload definitions but must reuse these top-level fields, Task authorization,
artifact and result semantics. Unknown operations, types and fields fail before
provider calls. Browser operations and exact grants:

| Operation | Grant | Required payload | Optional payload |
| --- | --- | --- | --- |
| `browser_start` | `cyber.browser_start` | `url` | `session_key`, `profile`, `scope` |
| `browser_step` | `cyber.browser_step` | `session_id`, `view_id`, `action` | `candidate_id`, `seconds`, `url` |
| `browser_inspect` | `cyber.browser_inspect` | `session_id` | `section`, `offset` |
| `browser_close` | `cyber.browser_close` | `session_id` | none |
| `cancel_jobs` | `cyber.cancel_jobs` with `cleanup=true` | none | none |

The built Task SDK invokes these as `cyber.browser_start`,
`cyber.browser_step`, `cyber.browser_inspect`, `cyber.browser_close` and
host-only `cyber.browser_cleanup` (which sends `operation:"cancel_jobs"`).
For example, reuse the start request's **attempt** with a new UUIDv4
`operation_id` for each distinct operation and these payloads, in order:

```json
{"operation":"browser_step","payload":{"session_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","view_id":"view1","action":"screenshot"}}
{"operation":"browser_inspect","payload":{"session_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","section":"screenshot"}}
{"operation":"browser_close","payload":{"session_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}
```

The 64-hex handle and `view_id` above are placeholders from the **confirmed
start** result, not caller-selected IDs. The SDK supplies the unchanged
`schema_version`, `attempt` and generated `operation_id` in each envelope.

`url_contract.py` is the payload schema: strings at most 2048 characters;
`session_id` is 64 lowercase hex; `session_key` is 1–64 URL-safe characters;
profile is `desktop`/`mobile`; scope is `host`/`observed_external`; action is
`navigate`, `follow`, `expand`, `root`, `screenshot`, `back`, `scroll` or `wait`.
Candidate IDs are required **only** for follow/expand, wait seconds (1–15)
**only** for wait, URL **only** for navigate. Inspection section is `summary`,
`dom`, `forms`, `scripts`, `network`, `frames`, `screenshot` or `choices`, offset
is integer 0–1000000. A start URL must occur in the Task inputs; Task
`browser_scope=host` forbids widening. Each action requires fresh authority.

Responses use the existing Task consumer fields `schema_version`, `task_id`,
`operation_id`, `operation_status` (`confirmed`, `rejected`, `pending`, `unknown`),
`result` and, for confirmed evidence, `artifact` (`artifact_id`,
`content_type`, `content_sha256`, `byte_length`). Confirmed start/step results
contain `status: completed`, opaque `session_id`, `view_id`, `session_open`,
`cleanup_status`, bounded `choices`, `observations` and `evidence_artifacts`.
Inspect retains paged text or a bounded JPEG preview. Raw screenshots and DOM
use the existing Task artifact path, not inline provider tokens or remote URLs.
A lost outcome retains the **same** operation identity:

```json
{"schema_version":"1.0","task_id":"tsk_00000000-0000-4000-8000-000000000001","operation_id":"00000000-0000-4000-8000-000000000004","operation_status":"unknown","result":{"status":"unknown","reason":"browser_outcome_unavailable"}}
```

Confirmed start example (IDs here are illustrative):

```json
{"schema_version":"1.0","task_id":"tsk_00000000-0000-4000-8000-000000000001","operation_id":"00000000-0000-4000-8000-000000000004","operation_status":"confirmed","result":{"status":"completed","session_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","view_id":"view1","session_open":true,"cleanup_status":"open","choices":[],"evidence_artifacts":["art_00000000-0000-4000-8000-000000000005"],"observations":[]},"artifact":{"artifact_id":"art_00000000-0000-4000-8000-000000000006","content_type":"application/json","content_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","byte_length":123}}
```

Invalid inputs: HTTP 422 `{"code":"invalid_request","message":"Unsupported browser operation"}`
or a field-specific refusal; transport/Task authority failures: HTTP 403
`code: tool_refused`. A valid, authenticated but unowned session returns HTTP
200 with `operation_status:"rejected"` and `result.status:"refused"`. An
unavailable gateway returns HTTP 503 `code: outcome_unavailable`; a lost
provider result remains an HTTP 200 `unknown` receipt. Never expose exception text,
credentials, session tokens or URL query strings in errors. Atomically claim
operation ID + verified attempt + sorted-JSON payload digest **before** provider
calls. Start/step/close also claim a durable digest alias so a *different* ID
for the same uncertain action returns HTTP 409 rather than repeating it.
Persist pending/confirmed/rejected/unknown outcomes beyond session expiry;
replaying an uncertain start/navigation/action must only read the old outcome,
never dispatch again. Expire browser sessions separately. Send progress and
artifact receipts through the Task SDK event/artifact consumer without an extra
model turn.

Runnable **mocked-provider integration tests** (not a live AWS claim):

```bash
uv venv --system-site-packages /tmp/adp-browser-tests
uv pip install --python /tmp/adp-browser-tests/bin/python 'pytest>=8,<9' 'moto[dynamodb,sqs]>=5,<6' 'rfc8785==0.1.4' 'fastapi>=0.115' 'pydantic>=2' 'Pillow>=10'
env -u ADP_TASK_TOOL_ROUTES -u ADP_TASK_TOOL_CLEANUP \
  BG_CONFIG_DIR="$(mktemp -d)" \
  PYTHONPATH=modules/domain-apps/cyber/tools:modules/tools:modules/domain-apps/cyber/agent/skills/url-analysis:modules/agent-factory/agent-worker-image \
  /tmp/adp-browser-tests/bin/python -m pytest -q \
  modules/domain-apps/cyber/tools/tests modules/domain-apps/cyber/tools/infra/tests \
  modules/agent-factory/agent-worker-image/tests/test_task_run_client.py
terraform -chdir=modules/domain-apps/cyber/tools/infra test
terraform -chdir=modules/domain-apps/cyber/infra/platform-integration test
```

At this source revision, the local acceptance suite exercises:

| Criterion | Local result | Evidence and limit |
| --- | --- | --- |
| AC-01 | Pass, mocked AWS/provider | `test_browser_http.py` sends start, screenshot/DOM inspect, step and close through the Task host, IAM Lambda, durable queue consumer, artifact receipts and offline Task report. No live AWS receipt. |
| AC-02 | Pass for explicit guards, mocked AWS/provider | Cross-Task use, private-IP navigation, disallowed scope, unknown fields, IAM caller denial and revoked grant are refused before provider actions. Redirects, page subrequests and DNS rebinding are **not** filtered or live-tested. |
| AC-03 | Pass, mocked AWS/provider | Same-ID and fresh-ID duplicate actions, lost provider/artifact result, cancelled Task, stale claim and restart/owned stop tests prevent action replay and false cleanup success. Provider expiry after irrecoverable start is a documented operator reconciliation case. |
| AC-04 | Pass, mocked AWS/provider | Terraform route tests keep local Browser and Common Crawl as defaults; opt-in switches all Browser names at once. Existing cyber Node SDK/report and Python routing tests pass. No production switch. |

The Node baseline runs after `npm ci --include=dev` and `npm run build` in
`modules/agent-factory/task-agents/investigator` and `npm ci` / `npm run build`
in `modules/agent-factory/task-agents/cyber`, followed by `npm test` there.
The live-provider smoke below remains an operator handoff, not acceptance
evidence from this PR.

## Provider and hosting

`TaskBrowser` keeps its session/receipt map in worker memory;
`local_browser` holds Playwright and a private Unix socket **in that pod**, with
a default 600-second lease. The `/tools/cyber` Lambda has a 28-second lifetime;
copying the adapter there loses the session and ownership across requests.
The historical `url-analysis-browser-broker` is a one-shot HTTP capture service,
not an interactive replacement. Native Browser uses `BrowserClient.start`,
CDP `ConnectBrowserAutomationStream`, and `StopBrowserSession`, not a generic
runtime or `InvokeBrowser`. Provider docs describe configurable session timeouts
(default 15 minutes, maximum 8 hours), not a guarantee of Task ownership.

The additive implementation uses an AWS_IAM gateway Lambda at
`POST /tools/browser` to check the worker role and fresh Task grant before creating
an atomic DynamoDB operation claim and sending a FIFO SQS message. A single
private EKS consumer owns the native browser socket and provider adapter;
there is **no public listener or Lambda browser state**. The consumer
revalidates the Task authority with forwarded workload/run proofs before each
action and writes a bounded Task artifact receipt to the durable claim. The
worker polls the **same operation ID** for up to 210 seconds, never dispatching
another action when a gateway response is lost. Claims older than 240 seconds
return `unknown`, not a retry; queued work older than 225 seconds is dropped
before dispatch. SQS redelivery only reads the existing claim. After process
loss, previous receipts and Task/attempt session ownership survive in DynamoDB,
but browser sockets do not. Steps fail closed, and cleanup stops/verifies only
owned provider sessions. Uncertain cleanup reports pending. The native lease
defaults to 600 seconds; session continuity after pod loss is not asserted.

`browser_http_enabled=false` in the separate tools stack provisions nothing;
`browser_admission_enabled=false` provisions the route but refuses new starts;
`task_browser_http_enabled=false` in the cyber worker integration keeps **all**
existing Browser names local by default. Set the latter only for *new Tasks*,
with `browser_tools_endpoint=https://<gateway>/<stage>/tools/browser`, after
the service and stage are available. The gateway Lambda has exact Task
authority, table and FIFO send permissions; the EKS IRSA role has exact
authority, table, FIFO receive and region-bound Browser lifecycle permissions.
The protected worker gets only this route's stage-qualified invoke ARN. No
tenant ID or identity headers supplied by the model are accepted. The native
adapter validates analyst-selected URLs, candidates and scope, but does not
provide all-traffic filtering of redirects, page subrequests or DNS rebinding.
Do not claim a private-network egress safety guarantee for this endpoint.

**Network boundary:** The HTTP path refuses explicitly disallowed selected
destinations and scopes. The native Browser does not filter every page-generated
request, redirect or DNS rebinding; this endpoint is not an all-traffic private
network filter. Live account quotas, reachability and provider permissions are
not verified. A guarded custom browser/egress boundary needs its own review,
not a redesign of the currently working Task browser in this issue.

## Switch, deployment and operator handoff

Keep existing `ADP_TASK_TOOL_ROUTES` browser entries local by default, including
`cyber.browser_cleanup`; Common Crawl stays `/tools/cyber/common-crawl`.
`browser_tools_endpoint` and `task_browser_http_enabled=false` are
app-owned configuration inputs in `infra/platform-integration/outputs.tf` and
the protected worker invoke allowlist. Switch **all** browser operations,
including cleanup, atomically for *new Tasks* to the exact HTTPS
`/tools/browser` route after validation; pin the backend for each Task/attempt.
On rollback disable new HTTP starts, drain/close owned HTTP sessions, then route
new attempts local. Never fall back to local on an unknown HTTP start.

Build a separate immutable browser service image; review a separate IaC plan for
private EKS egress (the consumer has no ingress), role trust, queue, table and
API route. The cyber Lambda, Common Crawl,
shared login stores and model proxy are unchanged. The shared API owner must
publish the route in a coordinated stage release, not by replacing a concurrent
deployment. See `modules/domain-apps/cyber/tools/infra/README.md` for the build,
plan and stage-ownership pattern and `docs/adp-platform-deployment/deploy-with-agent.md`
for later **authorized** live operations. Account/region/resource IDs are
configuration, never demo constants. Isolate `BG_CONFIG_DIR` in tests.

For a
*separately authorized* one-session live smoke: confirm account, region, quota,
permissions and cost budget; start one controlled public page with a lease at
most 600 seconds; record Task receipt and provider session ID privately;
close and check TERMINATED status, watch concurrent session/seconds and
CloudWatch/CloudTrail, and drain before removing only the new resources.
Browser session seconds, bytes/artifacts and provider charges are separate from
model tokens; an estimate is not settled billing. No infrastructure or paid
session is created by this PR.

### Browser review fixes

The gateway republishes queued claims with the same FIFO deduplication identity
when initial queue publication fails. Normal browser actions remain non-replayable.
Cleanup is idempotent: pending cleanup can be processed again with the same
operation ID, stops only owned sessions, and prevents further actions for the
attempt even after a consumer restart. Unknown sessions remain pending until the
provider lease plus the 120-second startup allowance expires. Completed cleanup
releases the in-memory owner. The host continues bounded cleanup polling after
Task cancellation or deadline expiry. No existing default browser route changes.

## Web Search implementation (#6634)

The Browser section above is the unchanged design checkpoint from #6637; **Web
Search** is implemented here. There is no direct client authentication or
model-proxy change. `POST /tools/websearch` is AWS_IAM, accepts only operation
`search` under the existing Task attempt/operation envelope, and maps it to the
exact `websearch.search` grant. Current principal policy, frozen Task grant and
current persona tools must all permit that operation. The Task host routes the
SDK's `tool.request` via its protected `ADP_TASK_TOOL_ROUTES` registry. No
client-supplied tenant, principal or URL is authoritative. Browser stays
`local:cyber_tools.task_browser.TaskBrowser` and Common Crawl stays on HTTP.

A runnable request body (same UUID/Task rules as above):

```json
{"schema_version":"1.0","attempt":{"run":{"task_id":"tsk_00000000-0000-4000-8000-000000000001","invocation_id":"00000000-0000-4000-8000-000000000002","generation":1},"runtime_attempt_id":"00000000-0000-4000-8000-000000000003"},"operation_id":"00000000-0000-4000-8000-000000000004","operation":"search","payload":{"query":"example.org current security report","maxResults":2,"filters":{"domainFilter":{"include":["example.org"],"exclude":["ads.example.org"]},"publishedDateFilter":{"from":"2026-01-01T00:00:00Z"}}}}
```

`payload` is a strict object: required `query` (1–200 characters), optional
`maxResults` (integer 1–25, default 10), optional `filters` with
`domainFilter.include`/`exclude` (up to 100 lowercase domains each) and/or
`publishedDateFilter.from`/`to` (ISO-8601 UTC seconds, inclusive; from <= to).
Unknown fields, operations, paths, types and invalid limits fail with HTTP 422
`{"code":"invalid_request","message":"Invalid Web Search tool request"}` or
404 `tool_refused`, before `tools/list` or `tools/call`. Missing/revoked grant,
wrong IAM role, different attempt/Task or mismatched scope fail before the paid
call. For a confirmed operation the response is:

```json
{"schema_version":"1.0","task_id":"tsk_00000000-0000-4000-8000-000000000001","operation_id":"00000000-0000-4000-8000-000000000004","operation_status":"confirmed","result":{"status":"completed","results":[{"url":"https://example.org/report","title":"Report","publishedDate":"2026-09-01","text":"Bounded excerpt"}],"query_count":1,"estimated_search_usd":0.007,"pricing_source":"https://aws.amazon.com/bedrock/agentcore/pricing/"},"artifact":{"artifact_id":"art_00000000-0000-4000-8000-000000000005","content_type":"application/json","content_sha256":"<64 lowercase hex digits>","byte_length":420}}
```

`results=[]` has `status="empty"` (still confirmed and charged); a timeout,
throttle or transport loss returns HTTP 503 `outcome_unavailable` with no
query/error/credential details. A subsequent request with the same
`operation_id`/payload reads the durable claim and returns `operation_status:
"unknown"`, `result.status:"unknown"`, `result.potential_query_count:1`,
`result.max_estimated_search_usd:0.007`, `error_code:"cyber_outcome_unknown"`;
never automatically repeat an uncertain paid search. A changed payload under
the same ID returns HTTP 409. Deduplication also uses the canonical payload
plus attempt digest, so identical queries do not incur duplicate searches.
Claims are fenced by the current Task version, attempt, state and the existing
128-operation/Task cap. No search sessions, bulk index or new cancellation API
are created; Task cancellation/revocation stops new starts, but cannot undo an
already dispatched paid call. The artifact keeps bounded snippets (1200 chars),
URL (2048), title (300), date (40), total result JSON (20 KiB);
a `results_truncated` flag marks sources dropped at this bound; the SDK publishes sanitized start and
completion/failure/uncertainty progress without the query, and the HTML report
retains clickable source URLs/titles/dates and artifact citations. Provider
charges are estimates/counts, not settled cost or token usage. The AWS pricing
page listed $7 per 1,000 searches on 2026-09-28, plus Gateway and model charges;
recheck before rollout.

### Provider ownership and deployment

`modules/tools/agentcore/infra/websearch.tf` owns the IAM route,
stage-qualified invoke permissions and (when `websearch_create_gateway=true`)
a dedicated AWS_IAM AgentCore Gateway + target `connector_id=web-search`,
**version 1.2.0**, target-level include/exclude lists and a Gateway service role
restricted to `InvokeGateway` on its exact ARN and `InvokeWebSearch` on
`arn:aws:bedrock-agentcore:<region>:aws:tool/web-search.v1`. Alternatively,
use a **qualified existing** same-account/region IAM Gateway and target; provide
its exact `websearch_gateway_arn`, `websearch_gateway_url` and
`websearch_target`, pinned to 1.2.0 with its target-level policy and service
role verified by the operator. Do not attach this service to an unqualified
JWT-only gateway. The Lambda IAM role receives `InvokeGateway` on that gateway
only. Discovery calls `tools/list` and checks the target-qualified
`<target>___WebSearch` schema includes `query/maxResults/filters`; `tools/call`
uses that discovered name, not the bare `WebSearch`. Target-level filters are
always applied by AWS and cannot be widened by Task request filters.

Both `enabled` (infrastructure) and `websearch_enabled` (paid-operation admission)
default false. `websearch_target_includes` and `websearch_target_excludes`
default empty. Runtime reads `ADP_WEBSEARCH_ENABLED`,
`ADP_WEBSEARCH_GATEWAY_URL`, `ADP_WEBSEARCH_REGION`,
`ADP_WEBSEARCH_TARGET`, `ADP_WEBSEARCH_CONNECTOR_VERSION=1.2.0`. In the app
platform-integration module, `websearch_enabled=false` leaves the worker's
`ADP_TASK_TOOL_ROUTES` without `websearch.search`; setting it true derives
`https://<shared-api>/<stage>/tools/websearch` from `tools_endpoint`. Operator
must configure persona/principal grants explicitly (no wildcard), include
`task_tool_invoke_resources` in the protected worker invoke boundary for the exact stage-qualified route if a
separate boundary is enforced, publish the reviewed shared API deployment,
then enable app worker routing and Lambda admission. This stack does **not**
auto-publish the shared API stage or deploy on merge. Use
`modules/tools/agentcore/infra/build-image.sh` and `deploy.sh plan`
with the verified immutable image and repository's normal app-worker rollout;
read their README for exact inputs. Terraform `init -backend=false`/`validate`
and local tests need no account or AWS provisioning. Restore both enable flags
to false first to roll back new searches (keep existing browser/CC routes),
then remove exact persona/principal grants; preserve the DynamoDB operation
store and shared login state for existing receipts. Only after retention review
remove the dedicated target/Gateway/IAM resources through the owning Terraform
state; never destroy a reused/shared gateway.

Later **authorized** live smoke (operator only): follow
`docs/adp-platform-deployment/deploy-with-agent.md`, confirm `aws sts
get-caller-identity` for the chosen `AWS_PROFILE` and get account approval
before deployment. Verify account, region (`us-east-1`, `eu-west-1`, or
`ap-northeast-1`), quota, target version and IAM/protected invoke boundary.
Submit one Task with `websearch.search` and `maxResults=1`, request restrictive
domain/date filters, observe `tools/list`/`tools/call`, artifact citation and
HTML source link. Test a denied Task separately (zero charge-producing calls),
record receipt IDs privately and AWS billing/CloudTrail evidence without query
contents or credentials. One permitted search estimates **$0.007** plus
Gateway/model fees; set a strict one-search budget and do not retry unknown
outcomes. Disable worker route/admission, then remove only smoke-created
resources after receipts are retained. Local mocked tests are **not** live AWS
qualification.

Local verification (mocked AgentCore transport, isolated config):

```bash
uv venv --system-site-packages /tmp/adp-6634-tests
uv pip install --python /tmp/adp-6634-tests/bin/python 'pytest>=8,<9' 'moto[dynamodb]>=5,<6'
BG_CONFIG_DIR="$(mktemp -d)" PYTHONPATH=modules/agent-factory/agent-worker-image:modules/domain-apps/cyber/tools:modules/tools \
  /tmp/adp-6634-tests/bin/python -m pytest -q modules/domain-apps/cyber/tools/tests/test_websearch.py
npm --prefix modules/agent-factory/task-agents/investigator ci --include=dev --ignore-scripts
npm --prefix modules/agent-factory/task-agents/investigator run build
npm --prefix modules/agent-factory/task-agents/cyber ci --ignore-scripts
npm --prefix modules/agent-factory/task-agents/cyber run build
BG_CONFIG_DIR="$(mktemp -d)" npm --prefix modules/agent-factory/task-agents/cyber test
terraform -chdir=modules/domain-apps/cyber/tools/infra init -backend=false -input=false
terraform -chdir=modules/domain-apps/cyber/tools/infra validate
```

### Web Search review fixes

Web Search follows bounded discovery cursors, rechecks Task authority immediately
before the paid query, accepts standard MCP success envelopes and CRLF event
streams, and reads at most 256 KiB of provider response before failing closed.
Snippet truncation is disclosed even when the number of sources is unchanged.
Shared handler/SDK/configuration now preserve Code Interpreter operations as well
as the existing default browser and Common Crawl paths.
