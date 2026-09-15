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

> **Correction (review repair, below):** that last sentence was true of the guard's
> *logic* and false of the job as shipped. On `arc-runner-org` those variables are
> always present, so the guard could never pass and the lane failed on 100% of runs.
> See "Two blockers, and why both were the same mistake".

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

---

# Review repair (PR #5198 revision 2)

Two blockers. Both had passing tests over them, and that is the actual lesson.

## Two blockers, and why both were the same mistake

**Blocker 1 — the offline lane's guard asserted something untrue of its own runner.**
The job ran on `arc-runner-org` and ended with a step failing if `AWS_ROLE_ARN` or
`AWS_WEB_IDENTITY_TOKEN_FILE` were set. But ARC runner pods have an IRSA service
account annotated `eks.amazonaws.com/role-arn`
(`modules/agent-factory/infra/modules/arc-runner/main.tf`), so the EKS pod-identity
webhook injects exactly those two variables into **every** container. The guard was
a working detector pointed at an impossible premise: lint passed, all 48 tests
passed, and the job failed on 100% of runs.

The non-obvious part: **omitting `configure-aws-credentials` does not make an ARC job
credential-free.** The identity is ambient, from the pod, not from a step. I only
believed this after running the guard's own loop in my environment — it failed on
`AWS_WEB_IDENTITY_TOKEN_FILE` — and printing `PASS: clean` under `env -i`.

Fixed with `runs-on: ubuntu-latest`, not by weakening the assertion. The two
reviewer-offered options are not equally good: keeping ARC leaves a
`pull_request`-triggered job holding real `adp-dev-agent-runner-role` credentials in
order to prove that a directory lints. Only a runner with no identity makes the
credential model *enforceable* rather than aspirational. `ubuntu-latest` is the
minority choice here (104 workflows use `arc-runner-org`, 1 uses hosted), so I
checked hosted runners actually work in this repo — `aidlc-gate-nudge.yml`, 8/8
recent successes — while noting that job only runs `github-script`, so it proves
availability, not that `pip install` works. That got confirmed on the real lane run.

**Blocker 2 — `--superplane-only` ran a near-full platform deploy.** `SUPERPLANE_ONLY`
was read exactly once, at the Step 12 gate. An operator running it got gateway infra,
gateway build/deploy, ALB + API-GW rewire, frontend S3 sync with CloudFront
invalidation, broker Lambda, **first-admin DB seeding**, webhook-ingress and
agent-factory — every one with `terraform apply -auto-approve` — and then reached the
one module they asked for. Now 17 references across every phase guard. Bootstrap and
platform infra still run, per the flag's name.

**What links them:** a declaration was added and the thing that *consumes* it was
not. Same defect class as the two registration points the issue's inventory missed
(above). The pattern is stable enough to check for deliberately: after adding a flag
or list entry, grep for every consumer and confirm each one reads it.

## A text assertion reproduced the bug it was meant to catch

`test_enable_and_skip_flags_exist` asserted the *string* `--superplane-only` appears
somewhere in `deploy-all.sh`. True from the argument parser alone, while the flag did
nearly the opposite of what it advertised. So the test embodied the same
"declaration without consumer" error one level up — it verified the flag was
*declared*, never that it *did* anything.

**Lesson:** for anything with runtime behaviour, a substring assertion is a
placeholder, not a test. Text assertions were genuinely right for the registration
lists (Bash arrays and Markdown aren't importable), and that correct precedent is
what made it feel acceptable to reach for one here. Reusing a technique past the
conditions that justified it is its own failure mode.

## Behavioural shell testing: five things that cost real time

The replacement suite executes `deploy-all.sh` with stubbed tooling on `PATH`.
Non-obvious mechanics:

1. **Drain stdin in no-op stubs.** The script pipes generated manifests into
   `kubectl apply -f -`. A stub that exits without reading closes the pipe, the
   writing `sed` dies of SIGPIPE, and `set -o pipefail` aborts the whole run with
   exit 141 — which presents as a scope failure with no useful message. Every stub
   is `cat >/dev/null 2>&1 || true; exit 0`.
2. **Detect phases by section, not by header line.** Steps 3 and 4 print their
   `Step N/12` header *unconditionally*, then branch and announce the skip in the
   body. A header-only check reports them as having run no matter what the guards
   say — it would have passed against the unfixed script.
3. **Distinguish "phase skipped" from "phase ran and did nothing."** Step 12 prints
   "skipping infrastructure apply" *while running*, because U3 owns the Terraform.
   A naive `/skipping/` match therefore reads a working phase as excluded. Match
   only the two real skip-announcement shapes.
4. **`PATH` stubbing cannot intercept `python3 <path>`.** An interpreter invoked with
   an explicit script path bypasses `PATH` entirely, so `pricing-rollout.py` needed
   an executable stub *at that path*.
5. **Assert the harness is offline, in the harness.** `shutil.which("aws")` must
   resolve inside the stub directory, or the suite could reach a real endpoint on
   somebody's credentials and nobody would notice. Also strip every `AWS_*` from the
   child env and pass `stdin=subprocess.DEVNULL`.

## The negative control found two things the review didn't

Ran the new suite against the pre-fix script (`git stash` + `git show <sha>:path`).
Exactly 13 of 35 failed — the 8 phase-skip cases, 4 sub-script cases, and
agent-context — while the other 22 passed, proving they aren't coupled to the fix.
A suite that has never failed against the broken code is decoration.

Writing the tests also surfaced two defects nobody had flagged:

- **Step 5 (ALB wiring) printed nothing at all when out of scope** — a bare `if` with
  no `else`. Every other phase announces its exclusion; a run jumping from Step 4 to
  Step 6 in silence reads like the script lost a phase.
- **Stale `Step 10b/11`** after the 11→12 renumbering. Invisible to the existing
  denominator test because its regex is `Step \d+/(\d+)` and `10b` contains a letter.

## A necessary comment broke an absence assertion

`test_lane_declares_no_aws_credentials` grepped the whole file for
`configure-aws-credentials` — and started failing on my own fix, because the header
now has to explain the ARC/IRSA behaviour *by name* to stop someone helpfully moving
the runner back. Deleting the explanation to satisfy the test would have been
backwards.

Made it structural instead: `yaml.safe_load`, then check `permissions` and
`job["steps"]`. A substring search cannot tell a credential step from a comment
explaining why there is no credential step, and an absence assertion that forbids
naming the thing being avoided makes the file undocumentable.

**Lesson:** absence assertions belong on the executable surface, not the file bytes.

## Lint the files your own CI lane lints

`ruff format --check` failed on two files, one of them
`test_superplane_registration.py` — which the lane lints *by name*. The required
check would have gone red on formatting after all the substantive work was correct.
Run the exact commands your workflow runs, on the files it names, before pushing.
The 6 pre-existing `I001` errors in `tests/migrations/` were re-verified as untouched
by this branch (`git diff origin/main --quiet` per file) rather than assumed.
