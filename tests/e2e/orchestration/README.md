# Qualification harness — #5156 (ENGINE-Q1)

Reusable configuration and fixture lifecycle for repeating a **bounded**
qualification in another environment, without local scratch scripts.

The problem this solves: a scratch script that dies halfway leaves a fixture
nobody has a record of, and a qualification that executed no scenario can still
look like it passed. This harness makes both impossible — every fixture is
recorded before it is created, and a run that executed nothing reports
`incomplete`, never `pass`.

**What this package is not.** It runs no scenarios of its own. Scenario adapters
are supplied by [#5157](https://github.com/aws-e/adp/issues/5157) and the live
qualification run is owned by [#5158](https://github.com/aws-e/adp/issues/5158).
Until #5157 lands, `--run` exits `4` (`incomplete`) by design. The tests in this
package are **network-free** and prove the harness's safety rules; they are not
evidence about a live engine.

## Commands

```bash
# Read-only: validates the config, verifies the real target account and
# authorization, reports the resources a run WOULD create. Mutates nothing.
python -m tests.e2e.orchestration.run --config <path> --preflight

# Execute the qualification.
python -m tests.e2e.orchestration.run --config <path> --run

# Reconcile a partially provisioned qualification before retrying.
python -m tests.e2e.orchestration.run --config <path> --resume <qualification-id>

# Delete only positively verified owned fixtures; retain sanitized evidence.
python -m tests.e2e.orchestration.run --config <path> --cleanup <qualification-id>
```

The four modes are mutually exclusive and one is required. Combining them is a
usage error rather than a guessed precedence.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success (`pass`, or `ready` for preflight/resume) |
| `2` | Usage error (no mode, conflicting modes, missing `--config`) |
| `3` | Config refused |
| `4` | **Nothing ran** — `incomplete`, explicitly *not* a pass |
| `5` | Failed (scenario failure, unverifiable target, unreconcilable fixture) |
| `6` | Cleanup refused a fixture whose ownership could not be verified |

`4` is distinct from `0` on purpose: a green check that executed no scenario is
worse than no check, so CI can tell the two apart without parsing output.

## Configuration

Copy [`config.example.json`](config.example.json);
[`config.schema.json`](config.schema.json) documents every field.
**`config.py` is the authority** — it is hand-written (the repo-root test tree
has no `jsonschema` dependency) and enforces rules the schema cannot express.
`test_example_config.py` keeps all three in agreement.

A config pins the authorized repository and registered connection, the
org/team/identity fixture references, the engine/worker/harness versions, the
resource/run/spend/duration bounds, and the artifact directory.

**It never contains a credential.** Secrets are named by reference
(`secretsmanager:`, `ssm:` or `env:`) and resolved at runtime only, so
`--preflight` and the offline tests need no credentials at all. Loading refuses,
reporting every problem at once:

- an embedded credential — by key name (`password`, `token`, `api_key`, …) or by
  value shape (`AKIA…`, `ghp_…`, `-----BEGIN … PRIVATE KEY-----`, …). The
  matched value is never echoed in the error.
- an unknown key or target, and `prod`/`production` outright — this harness
  provisions fixtures and is not authorized against production
- a floating version (`latest`, `main`) or a malformed one; an exact semver or a
  40-character commit SHA is required so a qualification is reproducible
- a missing, non-positive, non-finite or over-ceiling bound. Every bound is
  required; `BOUND_CEILINGS` caps each one so a typo'd `max_usd: 10000` is
  refused before it can be spent. This does **not** replace the policy and
  budget authority in #5128, which still applies at run time.
- a `..` segment in the artifact directory

## How recovery works

The ordering is the whole design:

1. record the intent (`planned`) and **flush it to disk**
2. call the provider
3. record the observed resource id (`created`)

A crash between 1 and 2 leaves a planned entry with no resource. A crash between
2 and 3 leaves a planned entry whose resource **does** exist. Both look
identical on disk — which is why `--resume` asks the provider instead of
assuming, using an idempotency token derived from ids already recorded. A
provider that honours that token returns the original resource, so a resume
never creates a duplicate.

`--resume` settles each ambiguous entry as adopted (`created`), still retryable
(`planned`), or `reconcile_failed`. Nothing is ever silently dropped: an entry
the provider could not resolve may be a leak, so it stays visible and needs a
human.

Writes are atomic — temp file in the same directory, `fsync`, `os.replace` —
so an interrupted write leaves the previous inventory intact rather than a
truncated file that would strand real resources. The inventory is mode `0600`
because it names real resources.

### Ownership

Every fixture is stamped with `adp:qualification-id`,
`adp:qualification-environment` and `adp:managed-by`. `--cleanup` deletes only
what it can positively tie back to those tags. **Unverifiable ownership is not
ownership**: a failed tag read, an untagged resource, a tag from another
qualification or another environment, or a `planned` entry with no observed id
are all *refused and reported*, never deleted. Sanitized evidence (identities
and resource ids, minus the provider dedupe key) is retained after cleanup so a
leak stays investigable.

Foreign, mis-versioned and wrong-environment inventories are refused rather than
adopted — acting on records this run did not write is how one run deletes
another run's resources.

## Running the tests

```bash
python -m pytest tests/e2e/orchestration -q
```

No AWS call, no browser, no deployed environment, no credentials. Standard
library plus `pytest`; `boto3` is imported lazily inside the runtime
secret-resolution path, which these tests never take.

Select this package explicitly. `tests/e2e/chat/conftest.py` and
`tests/e2e/infra/conftest.py` skip **every** collected item when their opt-in
variable is unset, without filtering to their own package, so a whole-`tests/`
run would report these as skipped. This package's `conftest.py` strips that
inherited skip (following the `own_items` pattern in
`tests/e2e/new_ui/conftest.py`), and `orchestration-harness-ci.yml` asserts a
non-zero executed count so a vacuous run fails instead of reporting green.

Coverage: config rejection rules; write-ahead ordering; crash before create,
crash after create, interrupted flush and interrupted cleanup; foreign,
mis-versioned and wrong-environment inventories; path traversal; stale ownership;
bound enforcement; CLI mode exclusivity; `--preflight` making no mutation; and
the guarantee that an empty registry cannot report PASS.

## CI

| Workflow | Trigger | What it does |
|---|---|---|
| [`orchestration-harness-ci.yml`](../../../.github/workflows/orchestration-harness-ci.yml) | `pull_request`, `push` to main | The offline tests above. No credentials, no `boto3`, never invokes the CLI. |
| [`orchestration-live-tests.yml`](../../../.github/workflows/orchestration-live-tests.yml) | `workflow_dispatch` **only** | Runs a real qualification against a real environment. |

**An ordinary PR never launches a paid qualification.** The live workflow has no
`push`, `pull_request` or `schedule` trigger; it defaults to `preflight`, takes a
**committed, reviewed** config path (never a pasted credential — a dispatch input
is recorded in the run log), assumes a scoped OIDC role, verifies the config's
environment matches the dispatch, and reports the account it actually touched
before mutating anything. `test_ci_selection.py` asserts all of this against the
workflow files, so a later edit adding a paid trigger fails a test.

## Scope

This is the fixture/config/resume portion of A6-1 and shared infrastructure for
A6-2–6. It authorizes no new environment, no paid run and no IAM change. Merging
it is a code milestone; the parent (#5133) keeps its own live acceptance criteria.
