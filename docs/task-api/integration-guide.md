# Integrating with the ADP Task API

This guide is for application developers calling ADP through API Gateway. Your
application obtains an OAuth access token, submits a task, saves the returned
Task ID, and polls or streams progress until a result is available. No GitHub
account, repository, AWS credentials or ADP SDK is required by the client.

## 1. Connection details and onboarding

Your administrator will share these values separately:

| Setting | Value |
| --- | --- |
| API base URL | `https://<api-gateway-host>/<stage>` |
| OAuth token URL | `https://<oauth-host>/oauth2/token` |
| OAuth grant | `client_credentials` |
| API authentication | `Authorization: Bearer <access_token>` |
| Initial persona | `agent-task-investigator` |

Replace the placeholders before running the examples. Preserve any stage prefix
in the API base URL supplied by your administrator.

Before integration, ask the administrator to provision a service principal in
your tenant and securely provide its OAuth client ID and client secret. They
must also enroll the principal for the required personas, Task scopes, model
selection and budgets. A valid OAuth token alone does not grant Task access.

Use credentials issued for your application; credentials are not included in
this document. Task ownership comes from the token's registered principal and
tenant, not from fields in the request body.

| Scope | Enables |
| --- | --- |
| `adp-tasks/submit` | Submit tasks |
| `adp-tasks/read` | Read status, results and progress |
| `adp-tasks/input` | Send follow-up input |
| `adp-tasks/cancel` | Request cancellation |
| `adp-tasks/artifacts` | Upload input artifacts and download artifact content |

Request only scopes enabled for your OAuth client and Task policy. The basic
examples below request the first four. Artifact examples additionally require
`adp-tasks/artifacts` and a new token containing that scope.

## 2. Obtain an access token

The examples use Bash, Python 3, `curl` and `jq`. Load the client credentials
from your application's secret store into `ADP_CLIENT_ID` and
`ADP_CLIENT_SECRET`. Do not commit them or enable shell tracing around them.

```bash
export ADP_API_BASE='https://<api-gateway-host>/<stage>'
export ADP_TOKEN_URL='https://<oauth-host>/oauth2/token'
export ADP_SCOPES='adp-tasks/submit adp-tasks/read adp-tasks/input adp-tasks/cancel'
umask 077

# Read credentials from environment rather than putting them in curl arguments.
ADP_ACCESS_TOKEN="$(python3 - <<'PY'
import base64
import json
import os
import urllib.parse
import urllib.request

credentials = (os.environ['ADP_CLIENT_ID'] + ':' +
               os.environ['ADP_CLIENT_SECRET']).encode()
body = urllib.parse.urlencode({
    'grant_type': 'client_credentials',
    'scope': os.environ['ADP_SCOPES'],
}).encode()
request = urllib.request.Request(os.environ['ADP_TOKEN_URL'], data=body, headers={
    'Authorization': 'Basic ' + base64.b64encode(credentials).decode(),
    'Content-Type': 'application/x-www-form-urlencoded',
})
with urllib.request.urlopen(request, timeout=30) as response:
    token = json.load(response)
print(token['access_token'])
PY
)"
export ADP_ACCESS_TOKEN
```

The token response contains `access_token`, `token_type` and `expires_in`.
Production clients should cache the token until shortly before expiry and obtain
a new token with client credentials. This grant does not use a refresh token.
Use the access token, not an ID token. Keep tokens out of logs and URLs.

## 3. Submit a task

Save the request body and a unique idempotency key **before** sending. Retain
both until the outcome is known, including across application restarts.

```bash
cat > task-request.json <<'JSON'
{
  "schema_version": "1.0",
  "persona": "agent-task-investigator",
  "instructions": "Analyze these service errors using only the supplied evidence. Explain likely causes and propose next checks.",
  "inputs": {
    "service": "checkout-api",
    "logs": "14:10 connection pool exhausted; 14:11 checkout returned HTTP 503"
  },
  "external_reference": "support-case-1042",
  "acceptance_criteria": [
    "Separate observed facts from hypotheses",
    "List missing evidence needed to confirm the cause"
  ]
}
JSON

python3 -c 'import uuid; print(uuid.uuid4())' > task-idempotency-key.txt

curl --silent --show-error --fail-with-body \
  -X POST "$ADP_API_BASE/v1/tasks" \
  -H "Authorization: Bearer $ADP_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(cat task-idempotency-key.txt)" \
  --data-binary @task-request.json > task-accepted.json

ADP_TASK_ID="$(jq -er '.task_id' task-accepted.json)"
export ADP_TASK_ID
jq . task-accepted.json
```

A successful submission returns **HTTP 202** with `task_id`, `invocation_id`,
`status`, `created_at`, `deadline_at`, `status_url`, `events_url` and `request_id`.
This confirms durable acceptance; the task has not necessarily started.

`status_url` and `events_url` are relative paths such as
`/v1/tasks/tsk_…`. Prefix them with the entire `ADP_API_BASE`, including its stage prefix.
Do not resolve them against just the hostname and accidentally lose the stage.

If the connection fails or the response is lost, repeat the same POST with the
**saved body and saved key**. Do not rerun the key-generation command. The server
returns the existing Task ID, with `idempotent_replay: true` on a replay. Changing
the body under the same key causes `409 idempotency_conflict`. A new key means a
new task and may incur additional cost. `external_reference` is a correlation
label, not an idempotency key.

The client may select an authorized `agent-task-*` persona. It cannot select a
model, executable, queue, tenant, owner, budget or runtime credentials in the
submission body. Unknown top-level fields are rejected. Put task-specific
content inside `inputs`.

## 4. Monitor status and read the result

```bash
curl --silent --show-error --fail-with-body \
  -H "Authorization: Bearer $ADP_ACCESS_TOKEN" \
  "$ADP_API_BASE/v1/tasks/$ADP_TASK_ID" > task-status.json

jq '{task_id, status, input_request, result, error, execution_health, recovery_required}' \
  task-status.json
```

Poll every few seconds with bounded backoff on transient errors. Stop when the
snapshot reports a terminal state:

| Status | Client action |
| --- | --- |
| `accepted`, `queued` | Keep waiting |
| `running` | Continue monitoring |
| `waiting_for_input` | Read `input_request.prompt` and send an answer |
| `cancel_requested` | Cancellation is pending; keep monitoring |
| `completed` | Read `result` and any result artifacts |
| `failed` | Read `error`; preserve the Task ID for diagnosis |
| `cancelled` | Cancellation has reached a terminal outcome |

The snapshot contains the final `result` and `error`; **there is no separate
`/result` endpoint**. A nonterminal task has `result: null` and `error: null`.
`execution_health: "unknown"` or `recovery_required: true` is not proof of
completion or failure. Escalate persistent uncertainty with the Task ID.

## 5. Stream progress with Server-Sent Events

Polling is sufficient for a complete integration. For live progress:

```bash
curl --no-buffer --silent --show-error --fail-with-body \
  -H "Authorization: Bearer $ADP_ACCESS_TOKEN" \
  -H 'Accept: text/event-stream' \
  "$ADP_API_BASE/v1/tasks/$ADP_TASK_ID/events"
```

Parse SSE `id`, `event` and `data` fields. Ignore heartbeat/comment lines.
Persist the last event ID **after** your application handles that event, and
reconnect using it:

```bash
# ADP_LAST_EVENT_ID is the exact saved SSE id, not a generated cursor.
curl --no-buffer --silent --show-error --fail-with-body \
  -H "Authorization: Bearer $ADP_ACCESS_TOKEN" \
  -H 'Accept: text/event-stream' \
  -H "Last-Event-ID: $ADP_LAST_EVENT_ID" \
  "$ADP_API_BASE/v1/tasks/$ADP_TASK_ID/events"
```

`?after=<cursor>` is also supported. Do not supply conflicting header and query
cursors. Connections are bounded and may close before the task finishes; EOF is
not a completion signal. Refresh expired tokens, reconnect with the saved cursor
and deduplicate events by ID. Browser clients need a streaming HTTP client that
can set Authorization headers; native `EventSource` cannot set custom headers.

For `410 history_expired` or a `history.gap` event, fetch the snapshot and record
the missing-history condition. Deliberately restart from available retained
history if appropriate. Do not claim to have received every event.

## 6. Provide follow-up input

When `status` is `waiting_for_input`, answer the current request. This example
uses the snapshot saved in section 4:

```bash
ADP_REPLY_TO="$(jq -er '.input_request.input_request_id' task-status.json)"
jq -n \
  --arg command_id "$(python3 -c 'import uuid; print(uuid.uuid4())')" \
  --arg reply_to "$ADP_REPLY_TO" \
  --arg text 'The database was healthy; the application connection pool was capped at 20.' \
  '{schema_version:"1.0", command_id:$command_id, text:$text, reply_to:$reply_to}' \
  > task-input.json

curl --silent --show-error --fail-with-body \
  -X POST "$ADP_API_BASE/v1/tasks/$ADP_TASK_ID/messages" \
  -H "Authorization: Bearer $ADP_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @task-input.json
```

A 202 receipt confirms acceptance of the command, not that the model has consumed
it. Track `command_receipts` in subsequent snapshots. `reply_to` is optional, but
supplying the current request ID helps reject stale answers. Input is consumed
at supported conversation boundaries; this endpoint is not an immediate
interrupt or a permission grant. Queued input may expire with its authorizing
token before consumption.

Persist each command's UUID4 `command_id` and exact body. Retry a lost response
with the same saved file. Generate a new UUID only for a new logical command.
Reusing an ID with changed text or another command kind conflicts.

## 7. Cancel a task

```bash
jq -n \
  --arg command_id "$(python3 -c 'import uuid; print(uuid.uuid4())')" \
  '{schema_version:"1.0", command_id:$command_id, reason:"No longer required"}' \
  > task-cancel.json

curl --silent --show-error --fail-with-body \
  -X POST "$ADP_API_BASE/v1/tasks/$ADP_TASK_ID/cancel" \
  -H "Authorization: Bearer $ADP_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @task-cancel.json
```

Retry with the saved cancellation body if needed. A cancellation receipt records
intent; poll the snapshot until its terminal outcome is known. If completion won
the race, the existing completed outcome remains valid. Do not treat
`cancel_requested` as proof that execution has stopped. Public Task pause/resume
endpoints are not currently provided.

## 8. Optional text/JSON artifacts

For larger evidence, upload a `text/plain` or `application/json` file and include
its returned ID in the submit body's `artifact_ids` array. Ensure the client and
token have `adp-tasks/artifacts` enabled first.

```bash
# evidence.txt contains the input evidence to attach.
python3 - <<'PY' > artifact-metadata.json
import hashlib
import json
from pathlib import Path
content = Path('evidence.txt').read_bytes()
print(json.dumps({
    'schema_version': '1.0',
    'content_type': 'text/plain',
    'content_sha256': hashlib.sha256(content).hexdigest(),
    'content_length': len(content),
    'filename': 'evidence.txt',
}))
PY

curl --silent --show-error --fail-with-body \
  -X POST "$ADP_API_BASE/v1/task-artifacts" \
  -H "Authorization: Bearer $ADP_ACCESS_TOKEN" \
  -F 'metadata=@artifact-metadata.json;type=application/json' \
  -F 'content=@evidence.txt;type=text/plain' > artifact-upload.json

# Use this body for a NEW task, with its own persisted idempotency key.
jq --arg artifact_id "$(jq -er '.artifact_id' artifact-upload.json)" \
  '.artifact_ids = [$artifact_id]' task-request.json > task-with-artifact.json
```

Uploads are multipart requests with parts named `metadata` and `content`; do not
set the outer Content-Type manually. Uploads have no idempotency key. A lost
response can leave an unclaimed upload, which expires after 24 hours.

To download an artifact referenced by the task, set `ADP_ARTIFACT_ID` to the
returned ID:

```bash
curl --silent --show-error --fail-with-body \
  -H "Authorization: Bearer $ADP_ACCESS_TOKEN" \
  -D artifact-headers.txt \
  "$ADP_API_BASE/v1/tasks/$ADP_TASK_ID/artifacts/$ADP_ARTIFACT_ID" \
  -o downloaded-artifact
```

Verify the bytes against the `X-Adp-Content-Sha256` response header. Downloads
remain authenticated and scoped to task ownership; there is no permanent public
download URL.

### Downloadable HTML investigation reports

With the cyber report renderer enabled, a completed `agent-task-cyber` task
returns its structured JSON report and a self-contained HTML report in
`result.artifact_ids`. Download these IDs using the authenticated artifact route
above. The HTML response has `Content-Type: text/html` and an attachment filename
ending in `.html`; save it as `report.html`. Do not assume every result artifact
is JSON. Verify its SHA-256 header just like other artifacts.

The HTML contains separate Common Crawl and live-browsing findings, an
assessment with evidence-based rationale, a recorded tool-action timeline,
embedded screenshot previews, coverage limits, recommendations, and an evidence
index. It opens offline without scripts or external assets. Original evidence
stays in the authenticated Task artifact store; the HTML embeds bounded previews.

You can share the downloaded file through email or your existing file-sharing
system. ADP checks access when it is downloaded; access to copies is controlled
by the sharing system you choose. No public S3 URL or browser-sharing token is
created. Caller input uploads remain text/JSON only; HTML is an output format.

## 9. Malware-analysis persona

`agent-task-cyber` has been merged, but merge alone does not enable it in an
environment. Ask the administrator to confirm deployment, persona enrollment,
model qualification and backend readiness before using it. Do not assume the
investigator's permissions cover cyber analysis.

Once enabled, select `"persona": "agent-task-cyber"` in the same submission API.
For example, an authorized URL investigation can supply
`"inputs": {"url": "https://<url-to-analyze>"}`. Malware sample analysis requires an
administrator-provisioned versioned sample in the reserved S3 namespace; the
text/JSON artifact upload above is not a malware-binary upload mechanism.
See [Cyber Task setup and input formats](task-cyber-sdk.md).

## 10. Limits and error handling

Limits below are maxima; the administrator may apply stricter policy. Existing
principal policies keep their configured duration until explicitly updated.
A new task gets a deadline of acceptance time plus that duration. Changing policy
does not extend an already accepted task.

| Input or resource | Limit |
| --- | --- |
| Submit JSON | 64 KiB UTF-8 |
| Instructions | 16,000 characters |
| Input artifacts | 4 files, each at most 256 KiB; 1 MiB total |
| Follow-up text | 4,000 characters |
| Cancellation reason | 1,000 characters |
| Task lifetime | Up to 6 hours, configured per principal and including queue/input waits |
| Model turns | Up to 8 |
| Concurrent executions | Up to 2 per principal |
| SSE connections | Up to 2 per task, 10 per principal |

| HTTP response | What to do |
| --- | --- |
| `400` | Fix invalid fields, command shape or cursor |
| `401` | Obtain a fresh access token; verify the configured token endpoint |
| `403` | Ask the administrator to check scopes, persona enrollment and policy |
| `404` | Check the Task ID and ownership; inaccessible tasks are not disclosed |
| `409` | Check changed idempotency payload, command reuse or stale input/state |
| `410` | Handle expired history/content explicitly; consult the snapshot if available |
| `413` | Reduce request or artifact size |
| `429` | Back off; honor `retry_after_ms` and preserve request identity |
| `502`, `503`, `504`, network timeout | Use bounded retries with backoff for reads and idempotent submit/commands; preserve the same body and key/ID |

Task error JSON includes `code`, `message` and `request_id`, and may include
`retry_after_ms` and `details`. Infrastructure errors may have a different body,
so handle the HTTP status even if JSON parsing fails. Persistently unavailable
prerequisites need administrator attention. Do not automatically resubmit a
failed or uncertain Task under a new key.

Save Task IDs, external references, command IDs, cursors and request IDs for
support. Retained task content is normally available for 30 days after terminal
completion, subject to continuing authorization. Store any results your
application needs longer than that.

## Reference client and contracts

A Python standard-library client is available at
[`examples/task-api/client.py`](../../examples/task-api/client.py), with
[setup instructions](../../examples/task-api/README.md). It implements OAuth
token renewal, bounded retries, SSE reconnection and artifact digest checks.

The versioned [request/response schemas](contracts/v1/schemas/public-api.schema.json)
and [limits](contracts/v1/limits.json) are the detailed contract. The examples in
this guide were checked against the source contracts; writing this guide did not
submit live tasks or qualify the cyber deployment.

Cyber tool evidence and generated reports share a bounded 16 MiB storage allowance
per task. Each worker output artifact remains limited to 1 MiB. An aggregate
capacity refusal returns HTTP 413; it is not a retryable storage outage.
