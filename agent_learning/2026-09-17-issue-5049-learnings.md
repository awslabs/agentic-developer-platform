# Learnings — issue #5049 (U11, durable handles and reconciliation)

EPIC #4910, requirement R15, A's half. Contract-only Python in
`modules/domain-apps/superplane/contracts/`, plus the adapter bookkeeping path and
its test suite.

## The lint gate is a version pin, not the linter on your PATH

Six findings on my new `adapter.py` (BLE001 blind-except ×5, S110 try-except-pass)
and two on U8's pre-existing `auth.py` (I001). My first read was "mine need
`noqa`, U8's are pre-existing debt on `main`" — and both halves of that were
wrong.

`ruff check --verbose` said "Using Ruff default settings": no config governs this
path, so the rule set is whatever the binary defaults to. Local ruff was 0.16.7.
`modules/gateway/pyproject.toml:51` pins `ruff==0.9.6`, and CI installs its
dependencies with `cd modules/gateway && pip install -e ".[dev]"`. Under 0.9.6 all
eight findings vanish and the whole module passes.

So: **all eight were a linter-version artifact.** Had I acted on the first read I
would have added six unnecessary suppressions and put a false "pre-existing debt"
claim in the PR description. Two habits worth keeping:

- Trace the CI lint step to the version it actually installs before reacting to a
  finding. `--verbose` telling you no config applies is the signal to go looking.
- A finding on a file you did not touch is evidence about your toolchain at least
  as often as it is evidence about the file.

The same applied to `ruff format`: formatting with the newer binary would have
churned lines that 0.9.6 then reports as unformatted.

## The referenced source was not in the repo

The issue cites `onboarder.go:179-186`, `consolidator.go:419-425` and
`deprovision-gpu-node-aws.sh:275-318`. `find` across the repo returns none of
them. They live in a pinned reference snapshot on the *planning* branch:

```bash
git ls-tree -r --name-only origin/agent/issue-4910 -- modules/domain-apps/ai-super-plane/reference/
git show origin/agent/issue-4910:<path> | sed -n '170,190p'
```

U12's `spike/provenance.py` documents that the snapshot is deliberately not copied
into `main` — its own regression check is "the snapshot stays unchanged". Reading
the cited lines rather than paraphrasing the issue changed the design: the defect
is not sloppy error handling but that SkyPilot's launch identifier is returned
**only on the success path**, so a timed-out launch structurally cannot report a
provider reference. That is why the fix is an *ordering* rule — the handle is built
from identity available before the call, which upstream already has (it picks
`clusterName` at `:170`) — and why `AMBIGUOUS` had to be a first-class outcome
rather than a flag on a failure.

## A fixture rule that review cannot enforce, but a generator can

The story required provider-response fixtures to be captured real responses or
derived from the provider SDK's response models, never from the adapter's expected
shape, "or adapter and fixture become self-consistently wrong about a timeout".

A hand-written fixture cannot be checked against that rule by reading it: it looks
identical whether its author read the SDK or read `adapter.py`. So I wrote a
generator that parses the `json:` struct tags and `ClusterStatus` constants out of
the snapshot's `types.go` and reads botocore's own `DescribeInstances` output shape
including the `InstanceState.Name` enum, and exits non-zero when a model stops
declaring a field the fixture uses. No key in the output is typed by hand.

Generalizable: when a requirement is about the *provenance* of an artifact rather
than its content, encode it as a producer that fails, not as a comment claiming it.

Useful detail — botocore ships the EC2 service model offline, so deriving fixtures
from the real response shapes needs no credentials and no network.

## Assertions that pass while the bug survives

The acceptance is "a timed-out call does not launch a replacement". The obvious
test — assert one provider call — passes while a caller loop above the adapter
launches on the next cloud, which is exactly upstream's bug. The other obvious
test — assert `may_repeat_operation is False` — passes while the adapter retries
internally. Both are needed, and the reason is worth stating in the test.

More generally, for anything shaped "X must not happen", ask which layer would
actually do X. Here there were two, and each test covered only one.

## Making a boolean claim unassertable

`durable=True` as a plain flag satisfies the type system and none of the
requirement — any caller can set it. Requiring `confirmed_at` alongside it means
the constructor demands persistence's acknowledgement instant, which a caller
cannot conjure, and that instant is then the evidence the teardown report cites.

Same shape in `assess_release({})`: "I checked nothing" and "I checked everything
and found nothing" are the same value in an unguarded implementation, and the first
must not be able to zero a bill. It raises.

## POSIX steals your exit codes above 125

`TeardownReport.exit_code` returns the count of outstanding findings, matching the
shell script's `exit ${ERRORS}`. Uncapped, an adapter reporting 130 unresolved
resources exits 130 — indistinguishable from being killed by SIGINT, since shells
reserve 126 (not executable), 127 (not found) and 128+n (fatal signal n). Capped at
125. Any code that derives an exit status from a count needs this ceiling.

## Scope boundaries are structural or they are decoration

The task was explicit that A must not add a scheduler, local Jobs, or approval or
budget authority even behind a flag. Rather than only writing that down, the suite
asserts it: no `threading`/`asyncio`/`sched`/`time.sleep` anywhere in the package,
no state held between adapter calls, no balance field on an assessment, no attempt
or backoff state on a handle. U8 set this precedent with its two documented
absences, and the reasoning holds — an absence nobody is watching gets filled in by
the next person who needs somewhere to put something.

## Environment notes

- The module has no packaging of its own; CI installs the *gateway's* dependency
  set and runs pytest over this tree. Imports work via `tests/_contracts_path.py`,
  imported for its `sys.path` side effect.
- Two pre-existing tests in `infra/control-plane/` shell out to `python3` through
  bash. In a venv they fail on a missing module unless the venv's `bin` is on
  `PATH` — a local artifact, not a real failure. Worth checking before reporting
  unrelated tests as broken.
- No credentials were needed or used anywhere in this work: the contracts take
  time and authority as arguments, and the fixtures are synthetic.

## What stays deferred

R15 acceptances 1, 5, 6 and 8 have live criteria needing a named account and
environment, spend authorization, a deadline and a named cleanup owner — all
unresolved. I did not invent any of them, and the README, the suite docstring and
the PR all say so explicitly. A mock authority verifies the mock; the offline tests
are evidence for the contract, not for the live criteria.
