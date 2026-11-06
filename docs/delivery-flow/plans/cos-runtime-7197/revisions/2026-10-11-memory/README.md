# Chief-of-Staff delivery revision: gateway memory and Crew recovery

This revises the existing `cos-runtime-7197` flow for the
[merged architecture](https://github.com/aws-e/adp/blob/825583373982de3e370f121544c4a18026454077/docs/chief-of-staff/architecture.md)
from PR #7394. Gateway AgentCore memory works across unchanged assistants;
Crew recovery uses an EBS working home, direct S3 archive/manifest uploads and
transactional checkpoint metadata. No Crew fork, custom memory plugin or S3 Files
mount is required.

The revision is saved **inertly** in the same flow. See [submission.json](submission.json)
for the registered version, hashes and original acceptance gate. No gate was
approved, execution policy activated, work dispatched or infrastructure deployed.
Historical root-directory proposals/submissions remain unchanged.

## Delivery sequence

| Wave | Implementation and dependencies | Exit evidence |
|---|---|---|
| Runtime | #6930 resolves contracts; #7221 supplies stable inventory; #7198 → #7199 → #7200 → #7208. Include existing #7283 deployment contract and #7172–#7175 foundation drivers. | #7214 repository evaluation |
| Crew and memory | #7210 gateway memory proceeds after #6930 and completed #6932. Independently: #7201 native image → #7202 checkpoints/metadata → #7209 bridge/lifecycle → #7203 owner sessions. | #7215 repository evaluation |
| Dashboard | #7204 custody and #7205 edge → #7206 proxy → #7207 native dashboard entry with truthful restore/sync status; owner/inventory prerequisites remain. | #7216 repository evaluation |
| Managed hosts | Existing #7368 → #7369 → #7370 → #7371/#7372; host identity also needs #7199. #7222 integrates the existing runtime, checkpoint helper and dashboard on EC2. Host preparation can progress alongside runtime. | #7223 repository evaluation |
| Qualification | Consume accepted #6937 evidence from its existing foundation flow; resolve #6935 provider gap. Bind deployed versions, target, fixtures, budget and cleanup at live-readiness gate. | Human-accepted #7217 live EP-01–EP-06 |

Wave membership is presentation, not a global barrier: graph edges control readiness.
#7214 also gates #7201; #7215 gates dashboard integration #7206; #7216 gates the
integrated EC2 repository evaluation. The full DAG is in
[proposal.authored.json](proposal.authored.json): 33 authored nodes, 88 edges;
the server inserts the original acceptance gate (34 effective nodes, 94 edges).
There are 26 story nodes, five evaluations and three effective human gates.

## Preserve completed work and qualification ownership

Reuse completed #6931/#6932/#6933/#6934/#6936/#147/#183/#6939. Do not reopen or
create replacement implementation stories. Existing host stories #7368–#7372 are
referenced directly; #7222 remains the assistant integration owner, with no second
EC2 provisioner or installer.

Foundation flow `7e5ddb5d-0fc2-40e8-abbc-553a7db2bed8` retains its 11 passed nodes,
receipts, pending #6937 and live-readiness gate. The primary flow's new
`qualification/foundation-receipt` gate consumes compatible accepted #6937 evidence;
it does not dispatch #6937 again. The passed #6935 foundation node does not prove
its still-open installed-provider requirement. That gap must be resolved before
accepting the foundation receipt. No foundation authority or spend is inherited.

The three obsolete pending addresses (`kirocrew/memory-sink`,
`kirocrew/memory-event-store`, `targets/target-abstraction`) become superseded
history. Their issue identities remain #7202, #7210 and #7221 at new addresses.
Older superseded nodes and receipts are preserved.

## Contracts and acceptance

[issue-contracts.json](issue-contracts.json) records 16 issue body revisions,
body hashes, four changed native blocker sets and old-to-new criterion mappings.
#7202 now depends on #6930/#7221/#7201; #7209 consumes #7202. Remove the old
reverse edge before adding this dependency. #7210 is independent of native
Crew backup. #7214 now explicitly depends on the existing foundation drivers.
All included native blockers are represented by graph edges; external completed
foundation dependencies and #6937 evidence ownership are explicit above.

Repository evaluations require executable mounted-route/container scenarios from
implementation owners. Proposed selectors are not claimed to exist today. Live
qualification must prove shared memory across assistants, corrections/deletion,
owner isolation, exact-version checkpoint restore, upload/DB failure recovery,
stale-writer rejection, container → EC2 → container continuity, retained-disk
conflicts, wake bounds, costs and independently observed cleanup. Model text and
health checks alone cannot prove durability or deployed isolation.

#6930 stays open for ARC-01–07, project/catalog binding and full product
decomposition. The runtime slice does not complete lifecycle/goals, delegation,
schedules/quiet hours, terminal continuity or pilot acceptance. Those requirements
need bounded children and a later reviewed revision before execution; #7197 cannot
close on EP-01–EP-06 alone.

## Bounds and verification

[policy-intent.json](policy-intent.json) preserves the v3 proposed limits: $1,500,
seven days, three attempts, concurrency two, expiry `2026-10-22T11:30:42Z`.
Allowed actions remain develop/review/repair/merge, environment connections empty,
and every evaluation requires human acceptance. These are proposed code limits;
this revision adds no cloud or live-operation authority. Named-dev CD behavior
remains governed by the canonical deployment section.

Validation: local proposal schema/DAG; server preview of the complete proposal,
policy and node/edge changes; GitHub edit/dependency readback; saved-plan readback
confirming the original unapproved gate, paused execution and no eligible work;
public documentation scan and whitespace/link checks. This is planning validation,
not evidence that memory, checkpoint or managed-host implementation is complete.
