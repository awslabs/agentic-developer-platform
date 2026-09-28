# Opus 5 route pricing investigation — 26 September 2026

Account 879318057152, us-east-1: US and global Opus 5 inference profiles are
ACTIVE. Pricing refresh published generations 158–160 at 06:00:40, 06:01:46,
and 06:04:05 UTC; each retained 264 of 1,500 variants. AWS's current pricing
page lists Opus 5 regionally but omits it from the global widget. Keeping the
global records' original verification timestamps is correct.

Live standard Opus 5 candidates: 55 total, 33 stale global records. US geo CRIS
rates for us-east-1 were verified September 26 at 06:04:02 UTC: $5.50/M input,
$27.50/M output, $0.55/M cache read, $6.875/M five-minute cache write, $11/M
one-hour cache write. Admission previously checked every route after stripping
the profile prefix, so stale global rates blocked the fresh US route too.
The fix checks the selected geography, endpoint, and standard service tier,
retaining all context tiers. Stale/missing route rates and reader failures
continue to refuse admission. No provider prices or timestamps are changed.

A real bounded US Opus 5 cyber probe passed at 2026-09-26T17:16:25.943298+00:00.
Provider request ID: 5e51fddb-3083-4e87-946d-68ad29a989fd.
It returned task_probe with value OK and stop_reason tool_use, using 509 input
and 33 output tokens, costing $0.003707 under the $0.10 cap.
Contract: task-cyber-sdk-messages-v1.
Request shape: 381205c19ec2aa7e8d3485f4d3f3ae8493ba6524baf57dde7ed11cfa89a49860.
This made no database mutations and proves the Task transport only. The CLI
shape manifest entry was generated twice against a local fake upstream using
the pinned SDK; both digests matched. It is not CLI invocation proof.

Validation: 76 focused gateway tests pass; changed Python files pass Ruff.
The live cyber preference was still Haiku 4.5 revision 1 when checked.
Activation requires a gateway release, fresh persisted exact Task evidence,
and a revision-validated preference change. Current task limits remain $1,
eight turns, 4,096 output tokens/turn, six hours. The serving gateway image has
changed since the earlier pricing release: preserve subsequent release work.
