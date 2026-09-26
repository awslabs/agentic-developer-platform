# Task API commands

`adp task` submits work, follows progress and requests cancellation through the
existing Task API. It does not resume an AI-DLC flow or kill worker infrastructure.
Use a CLI build containing `adp-task.py` and `adp_task_client.py`; installation and
`adp update` include both files when your gateway serves this build.

## Configure a Task identity

A platform administrator must register your service principal, OAuth client,
Task scopes and persona policy. Browser `adp login` sessions are not used by these
commands: signing in alone does not grant Task API access. The backend enforces
tenant ownership and the submit/read/cancel scopes on every operation.

Select a deployment using the normal CLI selection rule. Its URL and credential
binding stay pinned for the entire command:

```sh
adp deployment add development --url https://adp.example.com/api
adp --deployment development task status TASK_ID --credentials /private/task-client.json
```

Store credentials in an owned, mode-0600 JSON file. The example below shows field
names; obtain the actual secret through your administrator's secure handoff,
rather than placing it in shell commands, history or command arguments.

```json
{
  "gateway_url": "https://adp.example.com/api",
  "token_url": "https://your-auth-domain.auth.us-east-1.amazoncognito.com/oauth2/token",
  "client_id": "YOUR_REGISTERED_TASK_CLIENT_ID",
  "client_secret": "YOUR_SECRET_FROM_SECURE_HANDOFF"
}
```

`gateway_url` must exactly match the selected deployment's canonical URL, including
`/api`. The default file is `task-credentials.json` beside that deployment's
`config.json`; `--credentials FILE` or `ADP_TASK_CREDENTIALS_FILE` selects another
private file. Optional `scopes` is a JSON list such as
`["adp-tasks/submit", "adp-tasks/read", "adp-tasks/cancel"]`. Otherwise the CLI
requests only the scopes needed by the command, including read when `--wait` is
used. OAuth tokens refresh automatically before expiry and once after HTTP 401.

For an API Gateway stage whose task routes do not use the CLI's `/api` prefix,
add `task_api_url`, for example `https://your-api.execute-api.us-east-1.amazonaws.com/dev`.
It may change the path, but must retain the selected gateway's origin. It cannot
silently send Task credentials to another host.

Alternatively, `--token-file FILE` or `ADP_TASK_TOKEN_FILE` reads a private JSON
object containing `gateway_url`, optional `task_api_url`, `access_token`, and
optional Unix `expires_at`. The token's JWT expiry is also used for local refresh
timing, never as authorization proof. An external token manager may atomically
replace this file; the CLI rereads it on expiry or HTTP 401. A fixed expired token
requires explicit replacement. Do not supply both credential modes. Credential
files and HTTP/OAuth response bodies are never printed in errors.

## Submit and inspect

Create a request using the [Task request schema and fixtures](../task-api/contracts/v1/fixtures/valid/submit-request.json).
Fixture files contain `$fixture` test metadata; omit that field from API requests:

```json
{
  "schema_version": "1.0",
  "persona": "agent-task-investigator",
  "instructions": "Investigate the incident using only the evidence supplied in this request."
}
```

Persist an idempotency key before the first attempt:

```sh
adp --deployment development task submit request.json --key SAVED_KEY --credentials /private/task-client.json --json
adp --deployment development task status TASK_ID --credentials /private/task-client.json --json
adp --deployment development task submit request.json --key SAVED_KEY --credentials /private/task-client.json --wait --timeout 300 --json
```

Submission prints both stable task and invocation handles. Transport and transient
HTTP retries serialize the body once and reuse the same key. After an ambiguous
failure, keep the request file unchanged and retry the same key; a changed body
with that key produces a conflict. A timeout does not resubmit under another key.
Status prints the complete snapshot, including terminal result/error and artifact
references. This initial command set does not upload attachments, provide input,
or download artifacts; those remain available through the existing API/client.

## Follow progress and resume

```sh
adp --deployment development task monitor TASK_ID --credentials /private/task-client.json \
  --cursor-file /private/task-cursor.json --timeout 300 --json
```

Monitoring emits snapshots and authored events, reconnects after disconnects,
and resumes using the last successfully printed/flushed event cursor. Cursor
files are written atomically with private permissions and are bound to deployment
and task. Supply `--cursor TASK_ID:SEQUENCE` to explicitly override a saved cursor.
Duplicate replayed events are suppressed. Completion during a disconnect still
requires replay through the final snapshot's latest cursor before reporting
monitored completion.

Expired retained history, an explicit `history.gap`, or a missing event sequence
exits with code 6 and retains the last handled cursor. Run `task status`, inspect
the retained bounds/result, then explicitly select a new cursor if you accept
starting from that position. The CLI never claims continuous history across a gap.
Heartbeats keep the connection alive but are not emitted as authored progress.

The total local deadline defaults to 120 seconds; `--timeout` (alias `--seconds`)
accepts 1–3600 seconds. Each socket read uses the remaining deadline, including
slow partial frames and response bodies. Monitoring permits at most 10,000
handled frames and 50 connection attempts, with 64 KiB lines and 256 KiB frames.
`--max-events` lowers the handled-frame bound. JSON responses are bounded to 2 MiB.
Ctrl-C exits 130 and stops local monitoring only; it does not cancel the remote task.
The final `resume` record provides the last handled cursor even on interruption.

## Request an abort

```sh
adp --deployment development task abort TASK_ID --command-id SAVED_UUID \
  --reason 'No longer needed' --yes --credentials /private/task-client.json --json
adp --deployment development task abort TASK_ID --command-id SAVED_UUID \
  --reason 'No longer needed' --yes --credentials /private/task-client.json --wait --timeout 120 --json
```

`--yes` explicitly confirms the durable remote cancellation request. Every retry
uses the same command UUID and body. An accepted receipt exits 4 without claiming
that the worker stopped. With `--wait`, the CLI observes terminal state: confirmed
cancellation with confirmed child exit and no recovery requirement emits
`abort_confirmed` and exits 7; completion winning the race exits
5 and is explicitly not a successful abort. Timeout or Ctrl-C never sends another
cancellation or kills infrastructure.

## Output and exit codes

Human output labels each snapshot, event and receipt. `--json` emits one JSON
object per line. Data records use `{"type":"submitted|snapshot|event|resume|abort_receipt|abort_confirmed","data":...}`;
`submitted.data` is the API submission response, `snapshot.data` is the complete
snapshot, and `event.data` contains SSE `id`, `event`, and parsed `data` fields.
A `resume` record contains `task_id`, `cursor`, and `remote_abort_requested:false`
(local monitoring does not issue a remote abort). Error records use the shared
CLI envelope with `status:"failed"`, `command`, and `error.code/message`.

| Exit | Meaning |
|---|---|
| 0 | Submission accepted, nonterminal status read, or terminal completion observed |
| 1 | Invalid options or missing explicit abort confirmation |
| 2 | Missing/unsafe credentials, authentication, deployment binding or authorization failure |
| 3 | Transport/invalid response failure or closed output pipe; mutation outcome may be unknown |
| 4 | Local timeout/frame/connection limit, unconfirmed cancellation receipt, or cancelled state without confirmed child exit |
| 5 | Terminal task failure, request conflict/rejection, or abort lost to completion |
| 6 | Expired history or replay gap; explicit snapshot/resume decision required |
| 7 | Terminal cancellation confirmed (including successful `abort --wait`) |
| 130 | Ctrl-C detached the local command |

A completed task may retain failure findings in its application report; inspect
the result content as well as the transport/task exit status. Commands do not
convert a task failure or an accepted-only cancellation into success.

## Explicit human Task enrollment (#5516)

Deployments enabling `ADP_TASK_API_HUMAN_ENABLED=true` can enroll a human through
`GET|PUT /human-principals/{canonical-user-uuid}/task-policy`. Enrollment requires
an authenticated current human organization administrator and an active target
membership. The body uses the existing versioned Task policy schema, including
allowed personas/tools, task scopes, explicit model-policy revision and per-task
limits. Task submission never creates this standing policy.

`adp task submit REQUEST.json --key REQUEST_ID --human-login --wait` uses the
selected deployment's existing ADP login. The same flag works for `status`,
`monitor` and `abort`. It is mutually exclusive with service credentials/token
files. The command pins one access token and does not silently switch identities
on retry. API authority comes from current human membership and standing policy,
not caller token scope strings or a caller-supplied owner.

Durable owner IDs use `human:<canonical User.id>` so an unrelated service ID
cannot collide. The model/budget layer resolves that locator back to the real
human identity and its model preference, routing and budget hierarchy. Reads,
commands and SSE rechecks revalidate current membership and policy. Paid model
calls and new tool authority also recheck membership; stop-only cleanup remains
available. Existing service owners and their aliases remain unchanged.

This foundation supports the installed `agent-task-investigator` and
`agent-task-cyber` executables, with their existing Task protocol. It does not
qualify repository developer authority or hosted Codex/Claude CLI execution.
Those require server-bound repository/issue permissions and a compatible worker
adapter; renaming a persona or using an investigator is not that acceptance.
Existing per-task/pilot admission reservations and per-model personal/team/tenant
budget enforcement remain in effect. Live human, repository and engine acceptance
must be recorded separately before #5516 closes.
