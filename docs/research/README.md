# Research & exploration — fit assessments

Written recommendations from investigations of candidate technologies for ADP.
The process is defined by **EPIC #1219** ("Research & exploration — evaluate
candidate technologies and ideas for ADP"): each investigation is a sub-issue
of that EPIC, is architect-led, produces one assessment document here, and ends
with a maintainer decision — **adopt / adapt / reject / revisit later**.
Assessments are decision records: they are not updated as the upstream project
evolves; a changed landscape warrants a new investigation.

| Candidate | Verdict | Date | Issue |
|-----------|---------|------|-------|
| [OpenClaw](openclaw-fit-assessment.md) | Adapt — parity table drives `tests/e2e/test_openclaw_parity.py` | 2026 | — |
| [DeepSeek Harness (dsh)](deepseek-harness-fit-assessment.md) | **Adapt** the architecture (event-sourced session log, fail-closed HITL contract, spill-to-file, seam discipline) + one gated experiment; do **not** adopt as the worker runtime | 2026-08-26 | #4160 |
| [Hermes agent (NousResearch)](hermes-agent-fit-assessment.md) | **Adapt** — harvest the design; **reject** as the multi-tenant chief-of-staff foundation | 2026-08-26 | #4161 |
| [ORCA (stablyai)](orca-fit-assessment.md) | **Adapt** patterns (three-value liveness verdict → #4077) + permit as BYO client; no ORCA↔ADP integration | 2026-08-26 | #4162 |

### Routed recommendations

Where an assessment's recommendation has been turned into work, it is recorded
here so the verdict index reflects what was acted on:

- **ORCA §Q2 item 1 / §Q4 item 2 — `*_unknown` state discipline and the
  never-auto-resolve gate invariant** (the assessment's highest-value single
  action) → routed into **#4077** via **#4182**; the requirement text lives in
  [`docs/design-notes/4077-orchestration-graph-state-invariants.md`](../design-notes/4077-orchestration-graph-state-invariants.md).
  The read-side half of the same vocabulary (`live` / `unverifiable` / `exited`)
  is **#4176**.

## Adding a new assessment

1. File a sub-issue under EPIC #1219 with the subject and the specific
   questions to answer (see the existing sub-issues for the shape).
2. Tag `@agent-architect` on the sub-issue.
3. The assessment lands here as `<candidate>-fit-assessment.md` via PR;
   the verdict summary is posted on the sub-issue for the maintainer's
   decision, and a row is added to the table above.
