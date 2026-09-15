# Issue #5037 — Superplane domain-app skeleton, default-off gate, UI + deploy/undeploy registration

EPIC #4910, unit U1. Wave 1. Skeleton + fail-closed feature gate + registration in
the central deploy/undeploy lists + an offline CI lane.

## The issue's registration inventory was incomplete — by two entries

The story listed four deploy/undeploy edit points: `deploy-all.sh`, `undeploy.sh`,
`.github/workflows/undeploy.yml`, `deployment-manifest.md`. Two more were required
and neither is discoverable from the module directory:

**1. `platform/scripts/undeploy-phases.sh`.** `undeploy.sh` does not define its
phase functions. `_run_phase` builds the name dynamically:

```bash
local phase_fn="phase_${phase}"
```

and calls it; the bodies live in a separately-sourced file. So a name in
`PHASE_ORDER` with no matching function is **not** a lint error, not a shellcheck
finding, and not a `bash -n` failure. It is a teardown that fails on every single
run, retries twice, and reports FAILED. This is the sharpest edge in the whole
story and the issue does not mention the file.

**2. `modules/gateway/frontend/src/components/next/journeys.ts`.** Found only
because the full frontend suite failed. A #5123 coverage guard at
`journeys.test.tsx:410` scrapes `Navigation.tsx` for `to: '...'` paths and requires
each one to appear in the `/next` journey model. Adding a sidebar entry without a
journey entry fails that guard. So a "UI registration" for this repo is *four*
files (App.tsx, Navigation.tsx, features.ts, journeys.ts), not three.

**Lesson:** when an issue hands you a list of central registration points, treat it
as a lower bound. Grep for how the list is *consumed*, not just where it is
declared — dynamic dispatch and test-enforced mirrors are invisible to a reader
who only greps for the declaration.

## Three hazards here fail silently rather than loudly

Ranked by how long they'd survive undetected:

| Hazard | Failure mode |
|---|---|
| `PHASE_ESTIMATED_TIME` paired positionally with `PHASE_ORDER` | A missing entry doesn't error — it shifts every later phase's estimate by one, so each phase reports its neighbour's duration. Indefinitely wrong, never noticed. |
| `Step N/M` denominators in `deploy-all.sh` (28 of them) | Adding a 12th phase without renumbering prints "Step 12/11". Cosmetic, but it tells an operator the script is confused about its own phase count. |
| `Phase N/5` labels in `undeploy.yml` | Same class. Renumber **descending** (5→6, 4→5, …) or earlier edits collide with labels not yet rewritten. |

All three are now pinned by tests that parse the arrays/labels and assert internal
consistency, rather than asserting a hardcoded expected count that the next unit
would have to remember to update.

## Fail-closed needs the *strict* reader, and the reason is the frontend

`routes.py` has two readers with opposite defaults:

- `_is_enabled()` → True unless explicitly `"false"` (fail-open)
- `_is_enabled_strict()` → True only for a literal `"true"` (fail-closed)

Used strict. The decisive detail is not backend-side: `useFeatures` returns
`data ?? ALL_FEATURES_ENABLED`, so the frontend fallback object is what renders
**while `/features` is in flight** and **whenever it errors**. A `true` there
reveals the route on every cold load and keeps it visible through a backend
outage. Fail-closed therefore has to be asserted in two places — the backend
payload and the TS fallback constant — and both are now tested.

## Verify test *output*, not exit codes

Two separate incidents in one session:

1. `npx tsc --noEmit` installed a decoy package (`tsc@2.0.4`), printed "This is not
   the tsc command you are looking for", and **exited 0**. Root cause:
   `NODE_ENV=production` was set in the environment, so `npm ci` honoured
   `omit=dev` and installed 159 packages but only 1 binary. Fix:
   `NODE_ENV=development npm ci --include=dev` → 25 binaries. Then
   `./node_modules/.bin/tsc --noEmit` (explicit path, not `npx`) exited 0 honestly.
2. A backgrounded `vitest run` was reported as "exit code 0" while actually having
   1 failure — the `journeys.ts` guard above. Had I trusted the code, I'd have
   shipped a red required check.

**Lesson:** for background/wrapped commands, read the summary line. `npx` silently
installing a name-squatted package that exits 0 is a real failure mode, and
`NODE_ENV=production` in an agent environment quietly breaks every dev toolchain.

## Test-suite design choices worth reusing

- **Text assertions are the only option for four of five registration points.**
  Bash arrays, Actions YAML, TypeScript and Markdown aren't importable from Python.
  Precedent already in-repo: `test_agent_control_flag_parity.py`.
- **Pin gate suites by filename in CI, never by directory glob.** "No tests
  collected" and "all tests passed" are the same exit code to a glob, so a PR
  deleting the gate tests would go green.
- **Autouse `monkeypatch.delenv` on the flag.** Without it, a leaked env var makes
  the defaults-off tests pass for the wrong reason.
- **A test that has never failed proves nothing.** 16 of the 17 registration tests
  failed initially on a real bug (`parents[3]` → `/work/repo/modules`, needed
  `parents[4]`). That failure is what establishes they aren't vacuously green.
- **`test_routes.py` asserting the exact `/features` payload is a feature.** It
  failed when I added the flag. Correct response: teach it the new flag in both its
  env-clearing list and expected dict. Wrong response: loosen the assertion.

## Path depth is not copy-pasteable between modules

`phase_superplane` needs `../../../../../environments/...` — **five** levels,
because `infra/control-plane/` is one deeper than agent-context's `terraform/`,
which uses three. Counted from the actual directory rather than copied. Also
globbed for `*.tf` rather than testing `-d`, since the directory exists from this
story onward but stays empty until U3.

## Scope discipline

Left untouched despite being adjacent and tempting: `personas.py` and
`docs/agent-catalogue.md` (U4), `modules/gateway/cli/adp` + `ALLOWED_SCRIPTS` (U6),
all Terraform (U3), pinned image builds (U2). Created the CI lane even though it
looks like release tooling, because U12 is wave 1 and declares it a required check
— a lane owned by a wave-2 unit would be a wave-1 required check produced after
the checks needing it. Creating a lane is not taking release ownership.

R1 acc. 4 (resources absent from AWS after the undeploy phase runs) is inherently
live and **deferred, not silently claimed**: it needs a named account/environment
and a named cleanup owner, neither resolved. The CI lane asserts its own
offline-ness with a final step that fails if `AWS_ROLE_ARN` /
`AWS_ACCESS_KEY_ID` / `AWS_WEB_IDENTITY_TOKEN_FILE` are present, so it can't
quietly become a credentialed job later.

## Pre-existing lint noise, correctly left alone

`ruff check src/ tests/` reports 6 `I001` import-sort errors in
`tests/migrations/test_0{29,44,47,48,49,50}*.py`. Verified pre-existing by running
ruff on a throwaway worktree at the merge base (`e5672c88`) — identical 6. Not
mine, out of scope, and my CI lane lints only the specific files it owns so it
doesn't trip on them. `pip install -e ".[dev]"` also surfaces an unrelated
dependency conflict (bedrock-agentcore 1.23.0 wants boto3>=1.43.72, 1.36.0
installed) — environment noise, not a regression.

**Lesson:** when a repo-wide lint fails, prove authorship against the merge base
before either fixing it or claiming it isn't yours. A worktree at the merge base
answers this in one command and costs nothing.
