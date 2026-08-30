# Ledger fixtures — provenance

These fixtures back `test_security_agent_ledger.py` and
`test_render_security_report.py` (issue #4441, unit U2).

**Provenance rule.** Every shard here was written **from
`.github/security/ledger-schema.json`** — field names, types and stage
ownership were read off `x-fields`, and each file's shape was checked against
the schema's envelope. None of it was captured from the renderer's output.

That rule exists because the alternative is circular: fixtures written from
what the renderer happens to emit make the schema and the fixtures validate
each other's invented shape, and both can be wrong together while every test
passes. When a real run produces its first shards, a captured shard is the
other acceptable source — but never the renderer.

| Fixture | Run date | Shape | What it exercises |
|---|---|---|---|
| `ledger-complete/` | 2026-08-30 | 2 concurrent workflow shards, triage, orchestration, 3 ops shards | Terminal run: every story `fixed`/`stuck`, reconciliation holds, report stamps `final`. The smoke case. |
| `ledger-in-progress/` | 2026-08-29 | 1 workflow shard, triage, 2 ops shards | One story still `in_progress` → `final` withheld, report reads *delivery in progress* (FR-C36). |
| `ledger-zero-findings/` | 2026-08-28 | 2 workflow shards only | The common night (FR-C2): findings all deduped away, no triage, no stories. Vacuously final. |

Notes on details that matter:

- **The two `workflow.*` shards are the concurrency case.** `ledger-complete`
  has `workflow.code-review` and `workflow.pentest` as separate objects,
  because those two stages run at the same time. `identified_raw` sums to 12
  across them; `run_duration_seconds` takes the max (they overlap in time, so
  summing would over-report the run).
- **`ledger-complete`'s `planned_sequence` is deliberately not sorted**
  (`[5003, 5002, 5004]`) — the orchestration stage sequences by dependency,
  and the renderer must honour that order rather than re-sorting.
- **Finding ids are `f-<hex>` only.** No titles, paths or reproduction detail
  ever appear in a fixture, matching the constraint on real ledger content.
