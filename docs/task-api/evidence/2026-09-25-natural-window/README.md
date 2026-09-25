# Natural public SSE connection window

This read-only lane opens the actual public API Gateway SSE endpoint for the
already completed held task, starting after its terminal cursor14. The server
therefore emits an idless snapshot and comment heartbeats rather than replaying
the terminal event and closing immediately. It then reconnects using the same
cursor and observes another snapshot and heartbeat. No new Task, provider call,
command, injected event or production state write occurs.

`result.json` records actual connection timings and disposition. Raw wire frames
retain comment heartbeats, which the ordinary example client intentionally
ignores. Neither snapshots nor heartbeats advance the durable cursor or count as
authored progress. The separate held-task evidence in `../2026-09-25-v4` proves
actual authored progress before completion, exact input handoff and terminal
result integrity. This completed-task lane establishes the natural connection
window and public transport reconnect, not a new active execution.

The first exploratory parser omitted comment frames and was stopped locally;
its one retained snapshot is named `first-parser-omitted-comments.ndjson`. It is
not counted as a successful connection-window observation. `runner.py` is the
raw-frame recorder used for the completed measurement.

The initial connection's exact serving pod/image cannot be attributed from its
public response. The deployment was reconverging to the qualified gateway image
when it opened. The existing streaming implementation had the same 600-second
window in both gateway revisions. Final deployment cohort bindings are recorded
separately; no exact initial-pod claim is made. A subsequent transport write-bound
fix and its qualification remain separate from this natural-window observation.
