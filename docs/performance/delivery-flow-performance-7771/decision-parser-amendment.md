# Decision projection parser and migration for #7775

**Status: implementation of the coordinator-reviewed qualification adapter;
deployment remains separate.** The graph interprets
nullable `Text` reasons with Python `json.loads(reason or "{}")`. Contract D of the
pinned design requires selecting the latest *qualifying* event in PostgreSQL
before fetching bounded evidence. Replacing this with a JSONB cast, including
one guarded with `pg_input_is_valid`, changes which historical rows qualify:
Python accepts `NaN`, `Infinity` and `-Infinity` inside objects and permits
integer tokens far beyond PostgreSQL JSONB's numeric range (subject to the
interpreter's digit limit); JSONB cannot represent all these inputs.
Python also treats `true` as an integer for the result-observation predicate,
and the last duplicate key wins. A later object containing a nonstandard value
must not silently disappear and expose an earlier dispatch or result.

## Qualification adapter

The immutable database function in the gateway's
`src/orchestration/decision_payload_qualification.sql` computes four flags:
object, waiver keys present, Python-integer attempt (including Boolean), and
the exact admission contract. Lexical normalization replaces non-finite numeric
tokens and unrepresentable NUL/surrogate escapes only for these predicates;
PostgreSQL `json`, not a regular expression, validates the complete structure.
Ordered `string_agg` replaces repeated string concatenation, avoiding quadratic
assembly on numeric arrays. No JSONB cast or full-history Python fallback is used.
The original reasons remain unchanged and downstream attempt, policy, binding,
and plan checks still run against the original decoded values.

Qualification precedes timestamp/ID ranking. One indexed lookup per node and
category selects up to eleven event rows: eight primary categories, independent
result observation time, and two unparsed clearers. Graph and stall summaries
share this result; activity requests use only its dispatch selector. Sorting the
bounded result by `(created_at, id)` retains the existing reductions while making
timestamp ties deterministic. Full audit/history readers remain unchanged.

The integer-token limit is CPython's default 4,300 digits; deployments must not
override `PYTHONINTMAXSTRDIGITS` without a versioned parser/index change and parity
validation. Resource errors (including PostgreSQL stack exhaustion or statement
timeouts) propagate rather than being converted to false qualification. The
tests cover successful nested payloads and explicit resource-error outcomes;
they do not promise unlimited payload size or identical runtime recursion limits.
In particular, PostgreSQL can qualify an object with 10,000 nested arrays while
CPython raises `RecursionError`; selection never substitutes normalized content
for that payload, so the original Python decoder remains the final consumer.

## Reproducible verification

- Compare null/empty, object/array/scalar, duplicate keys, invalid syntax,
  extreme numbers, nonstandard numeric values, and Boolean-as-integer results
  against the Python oracle in the gateway performance tests.
- Check each setter, clearer and equal-time ID tie with independently specified
  expected state, then assert bounded selected rows and statement count on a
  real graph request through an existing isolated PostgreSQL target.
- Record the query plans and actual buffer work on the owned long-history
  fixture. Neither an SQLite test nor a small returned result proves this bound.

Use `uv sync --frozen --python 3.12 --extra dev` in `modules/gateway`.
The repository's `pg_url` fixture supplies disposable PostgreSQL 16 databases;
no deployed database is needed. Run the story 7775 selectors in
`evaluation-command-manifest.json`. The graph regression seeds 65,000 decisions,
forbids full-ledger reads, checks one projection statement, and compares indexed
and unindexed `EXPLAIN (ANALYZE, BUFFERS)` plans. Separate tests exercise all eleven
selectors on multiple nodes, Python/SQL qualification parity, and migration
recovery. SQLite reads register the runtime predicate functions on each connection
before projection queries. Fresh-process regressions cover full and dispatch-only
reads without pytest registration hooks, including replacement connections;
they do not establish PostgreSQL plan behavior. These checks do not establish
live latency or rollout acceptance, which remain with #7777.

## Migration, interruption and rollback

Run migration `104_decision_display_projection` before deploying the new reader.
It installs three versioned qualification functions and three partial expression
indexes for category, clearer, and integer-attempt result selection. Index keys
start with tenant/flow/node and end with descending timestamp and C-collated ID.
The functions are parallel-unsafe because guarded parse exceptions use PostgreSQL
subtransactions. The SQL resource is included in the gateway package.

Indexes build concurrently in Alembic's explicit autocommit block. Each build
uses a one-second lock timeout and five-minute statement timeout; normal readers
and writers need not wait for a whole-table exclusive lock. There is still CPU,
I/O, and extra index-maintenance cost. A busy or unusually large deployment can
time out; do not waive that failure or deploy the reader without all indexes.
On interruption, rerun the migration: it retains valid indexes, drops only its
invalid indexes concurrently, and rebuilds missing ones. No decisions are
rewritten or deleted. Timeouts are reset after the build attempt.

For code rollback, restore the previous reader and leave the additive indexes
and functions installed. If a schema downgrade is subsequently needed, revision
103 removes only these new indexes/functions, never ledger data. Do not modify
the semantics of a version-1 function under a populated expression index; a
future parser change requires new function/index versions and migration proof.
