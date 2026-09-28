# Fixtures — provenance

The `ledger-*/` fixtures back `test_security_agent_ledger.py` and
`test_render_security_report.py` (issue #4441, unit U2). The `triage-3984/`
fixtures back `test_triage_grouping.py` (issue #4448, unit U9) and have their
own provenance rule — see the section at the bottom.

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

## `triage-3984/` — the grouping regression fixture

Backs `test_triage_grouping.py` (issue #4448, unit U9).

**Provenance rule — inverted from the one above.** These two files were written
**from issue #3984 and its five real child issues**, not from the schema and not
from what the grouping script emits. That is the point: #3984 is the only
real-world record of a human triaging a batch of this pipeline's findings, so it
is the *calibration data* for the grouping band. A fixture written from the
script's own output would let the band and the fixture agree on an invented
ratio while nothing was ever checked against how the work actually got grouped.

| Fixture | Provenance | What it exercises |
|---|---|---|
| `new-findings.json` | The 12 finding ids from #3984, in `dedup_security_findings.py`'s result envelope | The band's input: `ceil(12/3)..ceil(12/2)` = `4..6`, which is #4448's smoke criterion verbatim |
| `grouping-plan.json` | #3984's five real children, with their real grouping (sizes 4/2/1/3/2) | The band's answer: 5 work items, inside `4..6`. Also the one committed example of a plan that passes every structural gate |

Notes on details that matter:

- **The group sizes are #3984's, not tidied.** The largest real group holds four
  findings, above `MAX_FINDINGS_PER_GROUP`. That is why the band bounds the
  *average* rather than capping each group: a per-group cap of three would reject
  the very fixture it was calibrated against.
- **Finding ids are `f-<hex>` only, and every group's prose passes the
  banned-pattern scan.** The fixture is the worked example of what an authored
  body may say, so a pattern match in it would be a worked example of the
  opposite. `test_no_generated_body_matches_the_banned_pattern_list` asserts it.
- **If this fixture drifts, every band assert is calibrated against fiction.**
  `test_the_3984_fixture_is_the_real_shape_it_calibrates_against` pins the
  counts and the size distribution so a well-meaning edit can't quietly move
  the calibration.
