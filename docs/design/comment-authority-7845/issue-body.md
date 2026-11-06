## The problem in plain terms

Some comments control privileged agent runs without repository-write checks; issue events skip account eligibility and default to human-rooted lineage. Both unauthorized influence and false attribution must stop.

**The fix in one line:** Verify repository-write authority before comment-driven effects, bind explicit approval to the current design/run, enforce actor eligibility on every GitHub dispatch family, and remove trusted-lineage defaults.

## Description

Scope: monitoring, Superpower, shared/sibling approvals, webhook issue/PR/comment admission, lineage and credential/spawn-depth bounds. Preserve authorized steering/stop/extend/retry/approval, tenant-owner controls and signed human authority.

| Lineage | Value |
|---|---|
| Kind | story |
| Parent epic | https://github.com/aws-e/adp/issues/7840 (native link verified; unchanged) |
| Project / Module / Feature IDs | unresolved / unmapped / unmapped; coordinator maps existing catalog before implementation assignment |

**Completion:** reviewed implementation plus live acceptance; developer owns code, coordinator-appointed operator owns release. Use `Refs #7845` until live acceptance. This assignment is design only: no implementation, deployment, merge, closure or developer dispatch.

## Impact analysis

HIGH is the scanner's highest covered rating. Owners/tenants gain real gates and attribution. Read/triage users and machines lose unsupported interactive authority; writers retain request-bound controls.

Risks: fail-open errors, authorization lockout/outage, stale grants, bad lineage/depth, unfenced guidance and missed sibling channels. Source defects are confirmed, not a live incident. Vault exposure is conditional: protected signed authority adds checks, and upstream identity resolution already rejects unknown users.

No new infrastructure, schema, secret convention or tenant setting. API calls/package size grow; latency/quota cost is unmeasured. Permission caches expire within sessions.

## Design

### Architect-approved design

**Status:** Architect-approved / ready for implementation. **Architect/reviewer:** native Codex SDK architect; architecture self-review, not human approval or independent code review. **UTC date:** 2026-10-16. **Reviewed main SHA:** `c958000f0829b1462c2f4ce23bfb743676b97306`. **Architecture blockers:** none. Operator, staging, App permissions and fleet state are coordinator-owned live prerequisites, unverified here.

[Design, audited sources, release map and backlog](https://github.com/aws-e/adp/blob/agent/issue-7845/docs/design/comment-authority-7845/README.md). Audit covers all 10,472 tracked artifacts and 2,259 build/deployment/script candidates; inventory is complete.

1. Reuse `comment-authority.ts` and bounded `ApprovalService`. Require admin/maintain/write on control/effect repositories before commands, including built-ins; exclude machines separately. `GITHUB_ACTOR` is not necessarily the agent. Fail closed with reason notices. Cache positive/negative/error decisions for 60/15/5 seconds; refresh for approval. Retain fixed-origin/no-redirect safety.
2. Gate PM parser/startup/reassessment/health, legacy retry lock/guidance, MCP host approval and hosted AIDLC presence. Preserve paused command polling, exact verbs, opaque IDs, pagination and dedup. No historical replay or edited-comment reuse.
3. Host binds posted plan/request to run/repo/issue, comment ID/time and digest. Only whole-comment `/approve <requestId>` from a human writer allows once; `/reject <requestId> [feedback]` rejects. Scan/poll share verification. Stale/edited/wrong/feedback/bare/bot/timeout/error cases stay closed; revision/restart needs a new request. Preserve bounded outcomes.
4. Authorize guidance and fence it in both Superpower prompts with `wrapUntrusted`; remove privileged skip/follow language. Fence summaries and retry/custom/rejection text. Guidance never grants tools, credentials or approval.
5. One admission decision precedes persona publication and engine-command persistence for supported comment/issue/PR events. Check signed sender, not content author. Preserve optional floor; actor mismatch uses bounded installation-scoped GitHub evidence, not borrowed association or a new gateway call. Package/test JWT dependencies. Invalid/unavailable evidence denies distinctly; human comments need write and engine `PLAN_APPROVE` remains additional.
6. Compute all-family correlation: only proven eligible humans start human roots; bots need server lineage. Unknown lineage is non-human and refused without depth reset. Preserve signatures, tenant/install/repo binding, credential horizon, service roots and verified-human envelope. Non-human gets no vault owner; only actual spawn increments.
7. Add helper note and source/AST consumer registry plus entrypoint tests rejecting ungated consumers/events. Preserve skill approval, controls and service dispatch. Backlog covers shared-file coordination with PR #6395 and existing #4181/#5365/#5663 mechanisms.

**Finding references:**
- `f-7ef65fa7-a730-49b5-998c-e611a8ac3830`
- `f-a7818886-f273-46d2-aa24-b65f355602ee`
- `f-df5741ab-0a7a-4082-a60a-b1e8cb072585`

Private scan detail stays in the coordinator's 2026-10-15 ledger. No AC/scenario IDs existed; this refinement introduces stable IDs and retains all findings and the native parent.

## Deployment

Developer supplies reviewed code/package checks and compatibility notes. Coordinator names authorized operator/staging fixtures. Legacy workflows compile source; hosted workers roll out an immutable image. Verify both and drain/cancel old affected runs under existing authority. Lambda code-only pushes use the current deploy path; infrastructure/mixed/manual full-stack releases remain held. Do not bypass holds or equate merge with live availability.

Operator records source/digest/Lambda hash and owns independent rollback to prior artifacts. No data migration/rollback. Ingress rollback leaves fixed runtime gates intact but reopens admission risk; prefer stopping affected dispatch. Protected environments, credential selection and canonical deployment guidance still apply.

## Validation

Authoritative acceptance table; implementation/live checks **not run** here. Backlog defines fixtures, assertions, commands and live procedure.

| ID | Action → expected result | Evidence / phase / owner |
|---|---|---|
| AC-01 | All monitoring verbs → unauthorized has zero effects; writer retains controls. | SCN-01/03 callbacks/state; pre-merge, developer |
| AC-02 | Permission faults/revocation/cache/token changes → fail closed, bounded notices, no secrets. | SCN-02/03 logs/effects; pre-merge, developer |
| AC-03 | Scan/poll approval cases → only current explicit human/writer approval opens exact plan/run once. | SCN-04/05 phase transitions; pre-merge, developer |
| AC-04 | Both guidance prompts + host → authorized intent fenced; no text releases gate. | SCN-06 shape/effects; pre-merge, developer |
| AC-05 | All sibling channels → no unauthorized mode/lock/cleanup/deploy/presence effect; writer path preserved. | SCN-07 entrypoint results; pre-merge, developer |
| AC-06 | All event/actor/floor/error cases → actual actor checked; denial has no job/authority/engine-row effect. | SCN-08 handler/queue; pre-merge, developer |
| AC-07 | Human/bot/unknown lineage → only proved human root; no fallback or unresolved publication. | SCN-09 envelopes/denials; pre-merge, developer |
| AC-08 | Authority modes/depth boundaries → no non-human vault owner, reset/double increment or service regression. | SCN-10 downstream proof; pre-merge, developer |
| AC-09 | New consumer/event + installed build → guard catches bypass; packages and controls pass. | SCN-11 CI/zip/runtime; pre-merge, integration owner |
| AC-10 | Staging on fixed artifacts → denied attempts inert, maintainers work, lineage truthful, cleanup recorded. | SCN-12 private receipts; post-deploy, release operator |
