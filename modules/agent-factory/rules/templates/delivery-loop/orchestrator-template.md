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
After ALL wave stories are merged + deployed, run #[EVAL_ISSUE_NUMBER].
If eval files defect issues: dispatch the developer persona on each DEFECT
issue (never re-mention merged stories); re-run eval after fixes merge + deploy.
Advance to next wave only when this wave's eval closes green.

## How to run
For each story in order:
1. Comment on the STORY with a single developer-persona mention to dispatch.
2. Wait for PR open -> CI green -> merge.
3. Post one-line status here.

After all stories merged + deployed -> trigger the evaluation issue.

## Guards
- ONE persona mention per comment; refer to other personas in prose only.
- No agent/issue-[THIS_NUMBER] work branch may exist on this issue.
- If dispatch stalls, check the submit FIFO per docs/orchestration-issue-guide.md.
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
7. **Evaluation reference**: the eval issue number is created first (Step 7b),
   then referenced here.
