# Design verification record

This records verification of the documentation, not acceptance of an implemented
recovery system. Source baseline is
`849f6163e59855c1554bc263e9ad96695730e9bd`; working directory `/work/repo`;
task branch `agent/issue-7572`. The design commit and ready PR are supplied by the
Git history/PR carrying this document, not a fabricated pre-commit identifier.

## Checks

Run from repository root with Python 3.9+, Git and the pinned source revision:

```bash
python3 docs/design/flow-self-recovery-7572/audit.py
python3 docs/design/flow-self-recovery-7572/validate.py
python3 scripts/check-public-docs.py
git diff --cached --check
```

Stage the intended design artifacts before the public-documentation check,
because that checker enumerates tracked/indexed files. The audit is offline and
reproduces all five static artifacts; the validator checks local Markdown links
and anchors, pinned source paths/line bounds, acceptance coverage, example test
paths, migration graph and complete tracked/workflow enumeration. External
issue/run links were fetched with GitHub CLI during research; the validator
does not claim an HTTP/link-health check of every external URL.

Completed on 2026-10-13:

| Check | Result and evidence boundary |
|---|---|
| Offline inventory reproduction | PASS: all five generated artifacts exactly match the pinned Git tree. |
| Document validator | PASS: pinned/local links and anchors, all 35 existing acceptance IDs with mapped scenarios, runnable example paths, 101 migration revisions, 10,139 unique tracked artifacts and all 148 workflows. External link health is outside this check. |
| Public documentation policy | PASS: 1,521 public documentation files checked, zero findings; also reviewed new prose for private identities and raw evidence. The pattern check is not a complete secret detector. |
| Staged whitespace validation | PASS after fixing empty TSV trailing fields in the generator; absent values now use `-`. |

The inventory reports 1,967 build/deploy/validation surfaces. Its unresolved
mapping/dependency rows remain visible; “PASS” means reproducible enumeration,
not that those deployment gaps are fixed. Local checks do not predict PR CI.

## Evidence not obtained

- No recovery engine, worker, UI, CLI or Terraform implementation was changed.
- No application unit/integration suite or live failure injection was run.
- No AWS identity, resources, effective IAM/RBAC, private GitHub environment
  settings or live schema/image health was verified. No infrastructure apply,
  database write, agent dispatch, deployment, merge or force push was performed.
- Recent workflow status observations are scoped in [inventory.md](inventory.md#observed-workflow-state-not-live-health);
  their success/failure does not establish current live availability.
- All child implementation/live acceptance and epic AC-01/SCN-01 remain not-run.
  Proposed permission-model decisions need the named owner's review.
- Child issue bodies are not rewritten by this PR. The coordinator can transfer
  approved designs after review while preserving native lineage and stable IDs.

## Review navigation

Start with [design.md](design.md) for decisions and per-child contracts, then
[backlog.md](backlog.md) for implementation packages and every acceptance ID.
[inventory.md](inventory.md) explains the complete TSV evidence and its limits.
Inventory scripts and generated data are documentation support only; neither is
wired into the application or deployment runtime.
