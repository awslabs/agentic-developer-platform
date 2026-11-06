# Responses request reservations and settlement recovery

The run cap covers recorded charges and outstanding reservations. Before the
next model call, the gateway also reserves that call's estimated cost. A budget
denial therefore does not mean the run has already spent its entire allowance.
The error includes `current_total_usd` and `request_estimate_usd`; the invocation
detail view separates recorded spend from outstanding reservations. Missing or
expired counters display as unavailable, never as zero.

## Request-sized estimates

By default, Responses text calls reserve a conservative estimate computed from
the actual server-received request: one token per UTF-8 byte of normalized JSON,
4096 tokens of framing allowance, and 64 tokens per structured element. All
fields count, including instructions, tool definitions/results, structured output
schemas and inline encrypted reasoning. The result is capped at the published
context limit. Output reserves the entire requested maximum, including reasoning.
Input/cache-write and output prices still use the highest published rates, with
no assumed cache hits. Inline media keeps the full-context reservation.

This is an estimate, **not a guarantee of zero overshoot**. AWS does not publish
a token-counting capability for these Responses models. The full-context cost
ceiling remains separately attached to the quote for receipt validation and
unknown outcomes. Trusted actual usage above an estimate is charged in full;
it is not discarded or clipped to the estimate. A legacy proxy finalizer with
unknown usage promotes the reservation to the provider ceiling, without extending
its deadline. A process crash before finalization can leave the original estimate;
it is never evidence of measured spend. Durable metering scopes block further
admission while an attempt has unresolved usage, retaining the original quote.
Set `BG_RESPONSES_REQUEST_ESTIMATES_ENABLED=false` to use full-context reservations
for every call. Budgets and concurrency limits do not change with this setting.

## Recovering usage after a database failure

Both Responses and Claude retry transient usage-write failures in fresh SQL
sessions, using the same request ID and pricing decision. There are four attempts
with short jittered backoff, bounded by the existing finalizer deadline. The
settlement receipt prevents double charges after a lost commit acknowledgement.
Permanent SQL errors and invalid receipts are not repeatedly retried. Logs include
SQLSTATE and connection status without SQL text, parameters or credentials.

If a Responses SQL write still fails, the gateway awaits a usage-only S3 record
through the existing budget-tracker channel, even if optional conversation
logging is disabled. No prompt or generated text is included. The tracker
reuses the original pricing decision and transactionally restores the usage row
and its exact run, chain and flow reservation links. Replays share the same
receipt lock and cannot double-debit the ledger or duplicate the usage row.

The existing flow reconciliation path applies those receipts to the run and
chain counters before clearing the flow marker. This preserves original
deadlines and never recreates lost counters. An unavailable receipt or storage
failure retains the reservation. Historical receipts without scope evidence
still require reconciliation from the original usage event and server-owned
request-to-run records; they are not inferred to be free.

The gateway deployment workflow ships the tracker code before the gateway
producer. There is no new queue, database migration or infrastructure resource.
