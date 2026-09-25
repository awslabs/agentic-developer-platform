# External Task API client

Python 3.11+ standard library only. No GitHub account, issue, ADP SDK, worker
credential or trigger credential is needed. An ADP administrator first registers
your existing canonical service principal, connects the Cognito `cognito_m2m`
client alias, permits `agent-task-investigator`, and grants the Task API scopes.
The Cognito client must use client credentials and resource-server scopes
`adp-tasks/submit`, `read`, `input`, `cancel`, `artifacts` (each with the
`adp-tasks/` prefix). This sample requests all five; production clients should
request only their required operations. The administrator also sets existing
budget, model, deployment capability and expiry policy. OAuth authentication
alone does not authorize task submission.

Set `ADP_TASK_API_URL` to the gateway HTTPS origin and either `ADP_TASK_TOKEN`
(a short-lived access token) or `ADP_TASK_TOKEN_URL`, `ADP_TASK_CLIENT_ID`,
`ADP_TASK_CLIENT_SECRET`. Obtain secrets through your service's secret manager;
never commit them, put them in CLI arguments, or capture them in evidence.
The client refreshes its OAuth token before expiry. A pre-supplied expired token
fails with401 and must be replaced by your caller. Redirects are refused so
credentials cannot follow a moved endpoint.

```sh
python3 examples/task-api/client.py upload evidence.txt > upload.json
python3 examples/task-api/client.py submit request.json --key service-case-20260924-001 > accepted.json
python3 examples/task-api/client.py snapshot tsk_REPLACE
python3 examples/task-api/client.py events tsk_REPLACE --seconds 60
python3 examples/task-api/client.py events tsk_REPLACE --cursor tsk_REPLACE:4 --seconds 60
python3 examples/task-api/client.py input tsk_REPLACE --command-id UUID4_REPLACE --text 'Prioritize pool exhaustion.'
python3 examples/task-api/client.py cancel tsk_REPLACE --command-id UUID4_REPLACE --text 'Investigation no longer needed.'
python3 examples/task-api/client.py artifact tsk_REPLACE art_REPLACE result.txt
```

`request.json` is ordinary JSON without the `$fixture` metadata used in tests:

```json
{"schema_version":"1.0","persona":"agent-task-investigator","instructions":"Explain the errors using only this evidence.","inputs":{"logs":"14:10 pool exhausted; checkout returned503"}}
```

To supply uploaded artifacts, put their returned `artifact_id` strings in the
submit body's `artifact_ids` array; see the frozen
[`submit-request-full.json`](../../docs/task-api/contracts/v1/fixtures/valid/submit-request-full.json).
The server resolves and freezes artifact metadata; upload expiry/request ID and
storage metadata are not submit fields.
A snapshot includes terminal result/error evidence; there is no separate invented
`/result` endpoint. Artifact downloads verify the returned content SHA-256.

Persist the exact body and idempotency key before submit. Reuse them after a lost
response, including across process restarts. The sample retries transport failures
and429/502/503/504 at most three attempts with at most10 seconds delay per retry.
Commands require a persisted UUID4 `command_id` and retry the identical body.
Changed payloads under an old key/command ID conflict. Uploads lack idempotency
and are not retried automatically. A lost upload response can leave an unclaimed
upload until its24-hour expiry.

SSE output is newline-delimited JSON containing `id`, `event`, and parsed `data`.
Persist the last successfully handled ID and supply it on reconnect. The example
reconnects on EOF/network timeout for at most60 seconds by default (maximum600),
limits received events to100 by default, and bounds frames. It stops on terminal
events. HTTP410/history-expired and explicit `history.gap` require caller action:
read the current snapshot and deliberately select its retained cursor. They are
not silently skipped or rewritten as complete history. API errors expose status
and structured response body through `APIError` for application handling.

Common responses:400 invalid input;401 invalid credential;403 scope/persona or
policy denied;404 inaccessible/unknown task;409 replay mismatch or state conflict;
410 expired history/task tombstone;413 payload too large;429 rate/capacity limit;
503 unavailable prerequisite. A successful cancel receipt records intent; poll
until child-stop evidence confirms cancellation. Unknown provider or stop evidence
must not be presented as successful completion or refunded usage.

Accepted limits:64KiB submit,16000 instruction characters; four256KiB text/JSON
artifacts and1MiB total;4000-character input/1000-character cancel reason and16KiB
command;10 pending inputs plus reserved cancel,100 inputs/task;up to six-hour lifetime (configured per principal),
eight model turns,4096 output tokens/turn,USD1/task. Capacity is2 executing tasks
per principal,4 per tenant and pilot. Existing stricter policy wins. Content and
receipts remain30 days after terminal state; content-free idempotency tombstones
remain to day90. Reads require continuing principal/task ownership authorization.

Charges belong to the existing canonical principal/tenant and stored model
operation/usage records, not a GitHub identity. Correlate task ID, invocation,
generation, runtime attempt, command ID, model operation and request ID across
snapshot/events, worker logs and usage logs. Do not log access/run tokens or
private reasoning. See [rollout and evidence](../../docs/task-api/rollout-and-evidence.md).

To record actual external receive timestamps for an existing accepted task, run:

```sh
python3 examples/task-api/observe.py tsk_REPLACE --directory evidence/task-observation --seconds 120
```

The directory must be new. This observer submits no tasks and captures bounded
SSE plus a final snapshot. It records observations, not acceptance PASS. Network
or API failure propagates as a nonzero exit; preserve partial files for diagnosis.
