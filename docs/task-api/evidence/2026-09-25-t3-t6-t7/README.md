# T3 / T6 / T7 implementation acceptance — 2026-09-25

The clean Task API run completed through the public endpoint: submission, ordered SSE progress, clarification, one consumed follow-up command, two model turns, three cited findings, result artifact download, and confirmed queue acknowledgment. Submission through public completion and queue acknowledgment required no operator repair. A later capacity audit found leaked execution slots, which were released through native settlement after the captured run; PR #6014 subsequently passed its live automatic-release check on clarify3.

Task `tsk_866578d0-a044-420a-9d76-ff4869432f33` ran on gateway image `sha256:dc18d65636a9fb7e4724053bc2fc15b4fe83af84849723ec2c077e860f883b66`. Runtime source was `a070f971a`; deterministic tests used `ee8dcdf89`, which adds only a contention regression to that runtime. The worker was the parent-qualified image `sha256:57d938b2dbf37dee4d3dae4f6a5042cfc8d96c8376e9dfd373e145993494f132`, independently rechecked against the pinned ScaledJob after the run.

The gateway suite passed **721 tests**, and actual Lambda publisher/recovery modules passed **42 tests**, with zero skipped tests. XML files record the executed test names and timing. The updated manifest registers the owned runnable selections. Contract validation passed all 369 checks across 62 criteria. Live evidence was collected by the public runner and independently checked against native model records, Redis admission settlement, and both S3 usage events.

The two verified provider charges were **$0.001947 + $0.004085 = $0.006032**. Both operations have provider request IDs, confirmed outcomes, settled reservations, and logged usage. The command handoff advanced from not started to prepared to confirmed. The downloaded artifact matches SHA-256 `e7e993ac7ce632e264b7335cfad091dd5f4a4acb16acc884b5f5d05e8372e6cc` and contains three findings citing the supplied follow-up evidence.

`criterion-report.json` distinguishes deterministic integration evidence from live observations. T7's five implementation criteria pass. T6-AC05 now includes useful completed legacy Codex and Claude runs, corroborated by exact native usage records. T3-AC05 now includes the automatic execution-capacity release proof below; all T3/T6/T7 implementation criteria pass. These records do not close independent V1–V5 evaluation gates or the EPIC.

## Related authoritative evidence

- [Real worker IAM denial](../2026-09-25-t1-storage/iam-real-probe.json): actual worker-role PutItem and TransactWriteItems denied against Task records.
- [Real owner denial](../2026-09-25-t1-storage/primary-owner-denial.json): six real public API reads refused for the wrong owner or tenant.
- [Prior implementation evidence](../2026-09-25-deterministic-review/index.json): preserved earlier tests and built-image observations, with their original source limitations.

## Historical defects and recovery

The earlier clarification run failed after consuming its reply because the child exited before the host wrote its final acknowledgment; that failure is preserved and the worker fix was deployed before the clean run. Its successful SQS-delete tombstone existed but its Task acknowledgment projection was missing. That historical projection was reconciled using the exact durable SQS 200 receipt and current-attempt/version fences. It is not automatic recovery evidence.

The earlier running-cancellation task exited without queue acknowledgment and was moved to the submit DLQ after three receives. Its exact committed envelope was identified, compared with the canonical dispatch envelope, and redriven through the existing publisher attributes. The new gateway drained it without executing the cancelled task and durably confirmed acknowledgment. Only then was that owned DLQ message deleted; unrelated messages were not deleted. `cancel-redrive.json` records the native acknowledgment and DLQ deletion request IDs. This operator action repairs a historical fixture; automatic fault/recovery behavior is established by the named deterministic tests, not by relabeling this intervention.

All evidence is scoped to AWS account 879318057152, us-east-1 dev. The engine remained paused; none of these tests or evidence updates resumed it.

## Legacy coexistence follow-up

The parent observed completed Codex invocation `3fd3d414-29f6-4cfa-b295-98b7079c64e8` and Claude invocation `d4efa9c6-5b10-4ca7-94a9-2d911490197e`; Claude produced report comment [5827214245](https://github.com/aws-e/adp/issues/5979#issuecomment-5827214245). Native usage rows independently corroborate six Codex requests and twelve Claude requests. Their recorded costs ($0.279687 and $0.942905) have estimated pricing confidence. Codex has fallback/unknown-model/tier/cache-rate reasons; Claude has a stale rate source. These values do not establish verified provider charges or satisfy independent V4 cost-efficiency thresholds. The first Claude baseline also lacked an applied hard deadline, so the parent is repeating the bounded V4 fixture. Its existing useful report is retained as implementation compatibility evidence.

## Automatic execution-capacity release — final verification

PR #6014 was deployed as image `sha256:dda452b0e4d254e37f4753ac171e5fa81f0ad2fe0113237ea0256c1a8420bb4a`, source `fe9396e67264029277a12499890a583c1d4a22e6`. Task `tsk_37ce236b-19a2-4d7c-a36d-b3609160b999` completed at 05:29:43 UTC with confirmed command handoff and queue acknowledgment. Both model operations were settled and verified; native Redis admission settlement equaled **$0.005807**. A strong full-authority scan after completion found **zero positive Task capacity rows**. No operator completion cleanup was applied to this run. The earlier client wait timed out while this task was deliberately held; the later Task outcome is completed, not expired.

The capacity patch passed 67 focused regressions, including three sequential model-backed completions and duplicate finalization/settlement without counter underflow. Recovery of historical confirmed-child-exit records also releases slots while preserving unknown financial outcomes. The source submit queue returned to zero visible, inflight, and delayed messages. The earlier 956-second oldest-message metric matches the intentionally held message published at 05:13:39; it is not silently reported as zero waiting time.
