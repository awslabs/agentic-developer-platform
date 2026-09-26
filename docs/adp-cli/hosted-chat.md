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

The `start --persona --message-file --request-id` and
`resume SESSION_ID --answer-file --request-id` forms currently return unavailable
before reading message files or sending work. General human Task admission and its
chat executable are still being integrated. The existing ingest supports only the
explicit `intent-refinement` pin; the CLI does not reinterpret general chat as flow
planning or borrow worker-only Task credentials. `--yes` cannot change that gate.
Issue-based hosted triggering remains independent.

E40 adds read-only readiness/history to the existing nightly evaluation. Full
#5640 acceptance remains open: durable general start/resume, concurrent/lost delivery,
real bounded multi-turn worker usage, and expiry/cleanup evidence still need the
supported chat Task executable and a live fixture.
