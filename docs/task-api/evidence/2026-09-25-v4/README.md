# V4 external integration evidence

This evaluation uses the authorized ADP development deployment in AWS account
000000000101, us-east-1. AI-DLC remains paused. The external observer ran on the
operator EC2 host through the public API Gateway endpoint, outside Kubernetes.
Accepted design: `b5761a4a2502aceaa9133afef552b567a19cb46e`.

The clean completion in the adjacent `2026-09-25-clean-completion` directory
proves a useful two-turn investigation in a tenant with no GitHub installation.
This directory adds an independently held task, explicit clock bounds, native
financial settlement, authorization revocation, cancellation and coexistence
references. `v4-criterion-report.json` records every mandatory criterion.

## Held task and observer limitation

Task `tsk_37ce236b-19a2-4d7c-a36d-b3609160b999`, invocation
`8143ce69-763e-4482-92e3-1bfcff33428f`, was accepted at 05:12:43UTC.
It started after operators released capacity retained by two earlier terminal
tasks. The capacity-release defect was fixed and deployed before this task
completed. No task state or result was repaired for this held fixture.

Three distinct authored progress updates reached the external observer in
0.787–1.451 seconds. An independent worker/observer clock bracket gives conservative
upper delays 1.912–2.576 seconds, below the frozen 5-second limit. The proof was captured
at 05:27:03 while the task was waiting for input, the input gate absent and no
terminal event observed. Clock bounds assume no intervening clock jump.

The operator released the input gate at 05:29:31, with 790 seconds remaining before
the task deadline. Command `a3c44dd6-031f-484d-9484-222a31ed5db5` was accepted
at 05:29:32 and consumed at 05:29:33 with confirmed handoff. Completion occurred
at 05:29:43 with confirmed queue acknowledgement. The2656-byte artifact has SHA256
`d5518a00498ba265fcd5bf4286fe0483c15a587a74d94271b1a4e33cc0768545` and equals
the public structured report. Two model operations settled USD 0.005807; native
admission accounting matches. The terminal capacity scan found no positive rows.

The original diagnostic harness started its 600-second observation window at
submission and paused inside its event consumer while waiting for the input
gate. That window elapsed during the deliberate hold. Immediately after sending
input, the harness sampled a still nonterminal snapshot and wrote premature FAIL
checks. Those original files are preserved unchanged. The read-only resumed
observation records actual completion and artifact integrity. This is an observer
limitation, not a production Task failure. The historical runner is included for
reproduction and is not a recommended reusable long-hold harness.

The alias-revocation probe closed the actual held SSE connection 11.456 seconds
after revocation, denied subsequent snapshot/events reads with 403, and restored
read access after the alias was reactivated. No principal policy was rewritten.

## Scope of coexistence evidence

The frozen baseline uses ten webhook no-op observations before and ten during
Task traffic. Useful bounded Claude and Codex worker runs are separate evidence;
a webhook 200 is not claimed to prove worker execution. The comparison's baseline
window includes earlier Task completion and its during window includes queued and
held Task traffic rather than continuous overlapping model calls. Shared service
metrics include other traffic and missing samples are not interpreted as zero.
The held Task explains the temporary shared queue age increase; final recovery is
assessed against the original five-minute requirement. These are development
qualification observations, not production SLO measurements.

The separate cancellation fixture reached confirmed child exit and public
`cancelled` state. Its original queue acknowledgement needed a guarded native
redrive after the acknowledgement defect was fixed; the original evidence and
recovery are both retained. This is not presented as an uninterrupted clean
cancellation delivery.
