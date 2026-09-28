# Hosted chat history and readiness

`adp chat status` reports whether the deployment has chat history configured and
whether general human turn admission is supported. History uses the existing
agent-factory conversation table; it does not create another conversation store.

```sh
adp chat status --json
adp chat list --page 1 --page-size 20 --json
adp chat show --session sess-ID --json
adp chat watch --session sess-ID --task-id EXACT_TASK_ID --timeout 60 --json
mkdir -m 700 /tmp/my-chat-export
adp chat export --session sess-ID --output /tmp/my-chat-export/transcript.json
```

The server requires the exact authenticated tenant, team and human owner recorded
by ingest. Foreign and legacy sessions without complete ownership are not found.
Expired owned sessions return `history_expired`; a readable row is not a promise
of permanent retention. Ingest normally retains sessions for 24 hours; the returned
`expires_at` is authoritative. List pages follow session ID order, filter ownership,
and may be empty with a next page. At most 100 pages can be requested. A concurrent
session change can change page boundaries.

Readback includes at most 100 user/assistant messages and 100,000 characters;
`truncated` records omitted data. Known secret patterns are redacted before length
limits, terminal controls are removed and tool/system metadata is excluded. Export
requires a new file in a private directory and refuses to overwrite any existing
file. Review user-authored text before sharing it: pattern redaction cannot identify
every secret a user may have pasted.

Watch polls bounded history for an assistant response carrying the exact requested
task ID. An idle session, another task's response, or partial websocket activity
cannot satisfy that match. A timeout returns pending and does not submit a turn or
cancel work. This snapshot polling has no partial-token stream or replay guarantee.

General hosted turns currently support the server-authorized
`agent-task-investigator` persona. It investigates supplied text and returns the
canonical structured report; it does not grant arbitrary developer, repository,
cloud or graph-approval tools. The same existing Task API owns admission, the
worker, model policy, budget reservation, tool grants and finalization.

```sh
adp chat start --persona agent-task-investigator --message-file question.txt --request-id opening-1 --dry-run --json
adp chat start --persona agent-task-investigator --message-file question.txt --request-id opening-1 --yes --json
adp chat resume chat-SESSION_ID --answer-file follow-up.txt --request-id follow-up-1 --yes --json
# If show/watch reports waiting_for_input, answer the exact question:
adp chat resume chat-SESSION_ID --answer-file answer.txt --request-id answer-1 --reply-to QUESTION_UUID --yes --json
```

Each message is limited to 4,000 characters. The existing session row holds a
conditional versioned request journal: opening IDs deterministically identify the
same session, each request freezes its Task input, and changed content with a reused
ID conflicts. A lost acknowledgement is pending; reconcile by resending the same
request ID and unchanged file. This invokes canonical Task idempotency, never a new
Task identity. An outstanding Task prevents a new turn. A clarification uses its
Task input command and retains the same task. A later completed-turn follow-up gets
a new Task with the prior task-correlated report as context in the same session.

A conversation accepts at most four request IDs (including clarification replies),
16,000 characters of prior context and a 300,000-byte stored journal. Context that
exceeds those bounds is refused explicitly. Retention expires 24 hours after opening
and is not extended by retry. Task records retain their own Task API retention.
No repository/workspace target can be changed through the message body.

Deployment requires `FEATURE_CHAT_ENABLED`, the existing session-table wiring,
`ADP_TASK_API_HUMAN_ENABLED`, `ADP_TASK_API_ADMISSION_ENABLED` and
`ADP_TASK_API_READ_ENABLED`, plus current human membership and explicit standing
human Task/model policy enrollment for the investigator persona. Capability
discovery lists only enrolled personas; actual admission rechecks policy, model,
budget and worker prerequisites. Gateway standing IAM permits session writes only
under `chat-*`; no per-task role-policy changes are used. Browser/legacy ingest
cannot attach a classifier turn to these Task-backed sessions.

E40 adds read-only readiness/history to the existing nightly evaluation. Full live
#5640 acceptance remains open until bounded multi-turn invocation through the served
EC2 CLI records worker/Task IDs, actual usage, retry/control outcomes and verified
retention/cleanup. No paid inference was used for code validation.
