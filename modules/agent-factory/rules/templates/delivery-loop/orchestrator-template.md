# Orchestrator Issue Template

> Per-wave mini-orchestrator for the autonomous delivery loop.
> Body MUST stay under 2KB — detail lives in child stories, not here.
> Reference: `docs/orchestration-issue-guide.md`

---

```markdown
## Description
[WAVE_LABEL] orchestration — drive these stories to merge in order, run the
wave evaluation after deploy, loop on defects until green.

## Stories (dependency order)
1. #[STORY_1] — [one-line summary] (no dependency)
2. #[STORY_2] — [one-line summary] (after #[DEP])
3. #[STORY_N] — [one-line summary] (after #[DEP])

## Deployment target
All deploys target account **[ACCOUNT_ID]** ([account-alias], [env]) via
`adp-cred assume --service aws --label [CREDENTIAL_LABEL]`.
[DEPLOY_WORKFLOW_REF — e.g. `gh workflow run <name>.yml -f account_id=...`]

## Evaluation
After ALL wave stories are merged + deployed, trigger #[EVAL_ISSUE_NUMBER] with
`adp-trigger --persona operations --issue [EVAL_ISSUE_NUMBER] --reason "run wave eval"`.
If eval files defect issues: trigger the developer persona on each DEFECT issue
with `adp-trigger` (never re-dispatch merged stories); re-run eval after fixes
merge + deploy. Advance to next wave only when this wave's eval closes green.

## How to run
For each story in order:
1. Dispatch the story to the developer persona with:
   `adp-trigger --persona developer --issue <STORY_NUMBER> --reason "wave <N> story"`.
   Do NOT post an `@agent-<persona>` comment — bot-authored mentions do not
   reliably dispatch and break correlation lineage. `adp-trigger` stamps lineage
   at ingress so the run stays connected to this orchestration chain.
2. Wait for PR open -> CI green -> merge.
3. Post one-line status here (prose only — status, not a trigger).

After all stories merged + deployed -> trigger the evaluation issue via `adp-trigger`.

## Guards
- Dispatch ONLY via `adp-trigger` (never an `@agent-<persona>` comment).
- Status comments are prose only — never contain an `@agent-<persona>` mention.
- No agent/issue-[THIS_NUMBER] work branch may exist on this issue.
- If a dispatch does not spawn a run, re-issue the `adp-trigger` call (idempotent
  by fresh message_id); check the submit FIFO per docs/orchestration-issue-guide.md.
```

---

## Emitter instructions

When generating an orchestrator issue from a delivery plan:

1. **One orchestrator per wave** (not one mega-issue for all waves).
2. **Title format**: `ORCH: [Intent-slug] Wave [N] — [scope summary]`
3. **Label**: `orchestrator` (do not add `epic` or `story`).
4. **Body must be < 2048 bytes** — if it exceeds, trim story summaries.
5. **Link to the phase EPIC** via native sub-issue (same as story linking).
6. **Deployment target**: extract account ID + credential label from the delivery
   plan's deployment section. If missing, FAIL lint and do not emit.
7. **Evaluation reference**: at draft time (Step 7c) use the placeholder
   `#[EVAL_WAVE_<K>]`; the real eval issue number is substituted at
   materialization (Step 8), after the wave's evaluation issue is created.
