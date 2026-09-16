# Issue #5156 — closing the three checklist gaps (target, bounds, recovery)

Continuation of `agent/issue-5156` / PR #5229; see
`2026-09-15-issue-5156-learnings.md` for the original build. This run closed the
three items on the maintainer's completion checklist. Still offline only: no
live qualification, no environment provisioned, no IAM change.

## Reporting an identity is not verifying a target

The original workflow printed `sts:GetCallerIdentity` to the step summary before
mutating anything, and I had counted that as target verification. It isn't. It
answers "which account did these credentials reach", which is a fact about the
credentials, not a check against intent. Nothing compared it to anything, so a
misconfigured role would print the wrong account in a nice table and then
provision fixtures there.

The fix is that the *config* has to declare the expectation
(`connection.expected_account_id`, `connection.expected_org`) so there is
something to compare against. **A report has no failure mode; only a comparison
does.** Generalisable smell: any "verify" step whose output is a print statement
is not verifying.

Two orderings mattered more than the comparison itself:
- the gate runs **before** `Inventory.create`/`Inventory.load`, so a refused run
  leaves no artifact behind and doesn't misreport "no inventory found" as the
  reason it stopped;
- `expected_account_id` is validated as a **string**, not an int. A 12-digit
  account id with a leading zero silently loses it in JSON number form, and the
  resulting mismatch would look like a security refusal rather than a type bug.

New exit code 7 (`refused`) rather than reusing 5 (`failed`): "we declined to
touch this account and mutated nothing" and "it ran and broke" need different
operator responses, and CI can only tell them apart if the process says so.

## Bounds must count attempts, not successes

`max_runs` was enforced by counting completed executions, so an adapter that
failed every time consumed no budget — the cap was unenforceable in precisely
the case it exists for. A repeatedly failing scenario would be invoked once per
registered adapter while the counter sat at zero.

Counting the attempt **before** invoking the adapter fixes it, and the test that
proves it is the checklist's: `max_runs=1` with three *failing* adapters must
invoke exactly one. Generalisable: **a limit on retries has to increment on the
path that can fail, which is the path before the call, not after it.**

## A resume/cleanup dispatch has an empty workspace

The subtlest gap. `--cleanup` worked perfectly in tests and would have been
useless in production, because a `workflow_dispatch` for cleanup is a *different
workflow run*: fresh checkout, no artifact directory, and the inventory — the
only record of which fixtures exist — simply isn't there. The fixtures would be
unreachable, which is the exact leak the inventory was built to prevent.

So recovery needs the whole chain, and any missing link makes the rest
decorative: a `source_run_id` input → `download-artifact` with `run-id` →
`--restore-from` → validate the archive (version, `managed_by`, qualification
id, environment) *before* installing it → refuse to overwrite an inventory this
run already wrote (overwriting could roll back deletions already recorded here).

`restore_inventory` searches with `rglob` filtered on
`path.parent.name == qualification_id` rather than assuming a layout, because
artifact unpacking nests differently than the directory that was uploaded.
Ambiguity is refused, never guessed: multiple matches raise rather than pick.

**Latent bug found on the way.** The artifact upload path was hardcoded to
`artifacts/`, but the example config writes to `artifacts/qualification`. The
upload would have silently captured nothing — `if-no-files-found: warn` — so the
inventory a later cleanup depends on would never have existed. Now the workflow
reads `artifacts.directory` out of the config (with a traversal check) and
uploads that. Generalisable: **when one step's output is another step's only
input, a hardcoded path between them is a bug waiting for the config to change.**

## A contract change should break tests, and it did

Adding the target gate failed 11 existing tests. That was the correct signal —
those 11 exercised mutating modes that now require a verified target, so their
failure was the new precondition being enforced. I fixed them by adding the
identity stub, not by weakening the gate.

Doing that edit with a script, I guarded every replacement with
`assert t.count(old) == 1` before substituting. Worth the extra line: a
scripted edit that silently matches twice, or zero times, corrupts a file in a
way the test run may not localise for you.

## Pre-submit findings, and knowing which ones are mine

- **Exit codes through a pipe.** My first CLI check piped output to `tail`, so
  `$?` was tail's status, not the CLI's — every code read as 0. Redirect to
  `/dev/null` (or read `PIPESTATUS[0]`) when the exit code *is* the assertion.
  The workflow already does this correctly; my manual check didn't.
- **Ruff at the wrong line length.** `ruff format --check` reported 11 files.
  Repo-root `tests/` has no ruff config, so ruff defaulted to line-length 88
  while this repo's convention is 150 (`modules/gateway/pyproject.toml`); no CI
  job runs ruff on this path at all (gateway-ci scopes it to
  `modules/gateway/{src,tests,pricing_policy}`). Rather than reformat a package
  to a length nothing enforces, I checked the package's *own* convention —
  signatures on one line under ~110 chars, wrapped above — and found exactly one
  line of mine that broke it. Fixed that one. **When a tool has no configured
  authority in a path, the surrounding code is the authority; match it instead of
  the tool's default.**
- The 6 `ruff check` findings (5× BLE001, 1× SIM115) are the same intentional set
  documented previously — blind catches keep a provider's arbitrary exception
  from abandoning a mid-reconcile loop and leaking a fixture, and SIM115's
  `delete=False` is required by the atomic temp-file-then-`os.replace` write.
  This run added none.

## Verifying the gate against reality without spending anything

Item 1 was demonstrable read-only: running `--preflight` resolved the actual
caller account, found it did not match the example config's declared account,
and refused with exit 7. Real credentials, real STS call, zero mutations —
the refusal path is the one place a live check is free.

## Repo facts confirmed this run

- Stale branch, misleading diff: `git diff main --stat` showed gateway/platform
  files as *deleted* because `origin/main` had moved 20+ commits past the
  merge-base. Nothing was deleted. Merge `origin/main` before reading a diff for
  meaning, or you will debug an illusion.
- `git stash` on a fully-committed tree is a no-op that returns success, so a
  "compare against baseline" built on stash/pop silently compares HEAD to
  itself. Check `git status` is actually dirty first.
- `modules/gateway` installs with `pip install -e ".[dev]"` (no
  `requirements.txt`); the named regression suite
  `PYTHONPATH=. python -m pytest tests/orchestration -q` then passes
  1689 passed / 11 skipped, and my diff touches no gateway file.
- `agent_learning/` is gitignored but tracked, so a new file there needs
  `git add -f`.

No credential, key or token appears in this file.
