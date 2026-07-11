# Defect Issue Template

> Filed by the operations persona when an evaluation check fails. Follows
> the repo's mandatory five-section format. The developer persona implements
> the fix — never the operations persona or the evaluation itself.
> Reference: defect protocol in #3334/#3335/#3336.

---

```markdown
## Description
[WAVE_LABEL] evaluation check [N] failed: [one-line summary of what broke].

**Failing check command**:
```bash
[EXACT_COMMAND_RUN]
```

**Expected output**: [EXPECTED]
**Actual output**: [ACTUAL]

**Owning story**: #[STORY_NUMBER] — [story title]

## Impact analysis
- **Who benefits**: unblocks [WAVE_LABEL] evaluation gate; required for
  delivery loop to advance to [NEXT_WAVE].
- **Who's impacted**: [service/component from the owning story].
- **What breaks if this ships with a bug**:
  | Bug class | Blast radius |
  |-----------|--------------|
  | Wrong fix (masks failure) | Eval passes falsely; downstream waves build on broken foundation |
  | Fix regresses other checks | Other eval checks fail on re-run |
- **Cost / quota footprint**: no new resources; fix is code-only.

## Design
Root-cause hypothesis: [brief analysis of why the check failed — what the
owning story's implementation got wrong or missed].

**Files to modify**: [list from owning story's Design section]
**Fix approach**: [1-3 sentences: what to change to make the check pass]

## Deployment
- **Automatic on merge**: [CI workflow from owning story's Deployment section]
- **Manual follow-ups**: [terraform apply / migration / none]
- **Rollback plan**: revert the fix PR; eval will still fail (pre-existing).

## Validation
- [ ] The specific failing check now passes:
  `[EXACT_COMMAND]` returns `[EXPECTED]`
- [ ] All OTHER checks in the evaluation (#[EVAL_NUMBER]) still pass
  (no regression).
- [ ] CI check `[CHECK_NAME]` passes on the fix PR.
```

---

## Emitter instructions

Defect issues are NOT generated at emission time — they are filed at RUNTIME
by the operations persona when an evaluation check fails. However, this
template is referenced by the evaluation template's defect protocol section.

The emitter's responsibility is to:
1. Include the defect protocol in every evaluation issue (referencing this
   template's structure).
2. Ensure the evaluation's checks are concrete enough that a defect issue
   can quote exact command + expected vs actual output.
