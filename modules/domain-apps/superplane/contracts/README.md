# `contracts/` — Superplane observation contracts

Unit **U8** (issue #5043), requirement **R11**, EPIC #4910.

The versioned contracts through which controllers and monitors submit fleet-health
and budget observations. `WIRE-SCHEMA.md` is the normative field-by-field
description; this file is the orientation.

## The problem this exists to fix

The only observation contract in the system today runs the *other* way and is
unauthenticated. The controller POSTs to `/internal/heartbeat` with `Content-Type`
as its only header, and the receiver has no auth dependency — so the single thing a
caller needs in order to write a cluster's state is that cluster's UUID. UUIDs
appear in logs, kubeconfigs, support tickets and any API response that lists
clusters. Knowing an identifier is not authority over the thing it identifies.

So this unit does not extend that path; it defines a replacement contract that is
authenticated, signed, versioned and workspace-scoped. `/internal/heartbeat` is
untouched here — the receiver, the route and the cutover are U15's, upstream.

## Layout

```
contracts/
  WIRE-SCHEMA.md              <- normative: fields, headers, versioning rules
  superplane_contracts/       <- import this
    version.py                <- version discipline (header + payload must agree)
    health.py                 <- CheckStatus, CheckResult, severity, aggregation
    observation.py            <- Observation, ClusterRef, BudgetUsage, to_wire()
    auth.py                   <- authentication + body signature
    scoping.py                <- per-workspace submit/read authorization
    leases.py                 <- reconcile leases with fence tokens
```

Tests live at the module level in `../tests/`, alongside the other Superplane
suites, so the credential-free CI lane picks them up without a second collection
root.

## The shape, and why this one

Frozen dataclasses, enums and functions with no framework or storage I/O.
Two consequences:

**Illegal states are unconstructible, not merely validated.** Each type's
`__post_init__` raises `ContractViolation` on combinations that assert something
the submitter did not establish. A probe author cannot forget to call the
validator, because there is no validator to call — the constructor is it. Every
guard in the package raises the same type, so a receiver catching
`ContractViolation` catches all of them. (It subclasses `ValueError`, so a caller
catching the broader type also works.)

Received JSON passes through `Observation.from_wire` and these same constructor
guards. `verify_submission` performs this validation after signature and freshness
checks, and returns the validated observation for U15 to scope and persist.

**Time comes from the receiver.** Lease expiry uses the receiver-supplied clock.
Submission freshness accepts a trusted `now` value and defaults to the receiver's
UTC clock. Tests pass a fixed clock; a submitter cannot choose the verification time.

## What each criterion is held up by

| Criterion | Mechanism |
|---|---|
| Contracts + versioning | Version appears in header **and** payload and must agree. Disagreement is refused, not resolved; absence is refused, not defaulted. Checked before the credential is looked at. |
| **R11 acc. 2** — authentication | `verify_submission()` requires a resolvable credential, a valid HMAC-SHA256 signature over the exact received UTF-8 bytes, and a timestamp within the receiver's freshness window. Re-encoding JSON before verification is forbidden. |
| Per-workspace scoping | `authorize_submit()` requires independently stored cluster ownership to match the payload workspace and submitter authority. `authorize_read()` separately checks read scope. |
| Leases without a table grant | `scope`/`holder`/`expires_at`/`fence_token` and pure functions over them. No table, connection or SQL anywhere in the shape. |
| **R11 acc. 4** — probe honesty | `not_checked` is a distinct status requiring a reason and forbidding readings; a positive claim cannot coexist with an error; `unreachable` outranks `unknown`; empty checks aggregate to `not_checked`. |

Three of those are properties that either hold by construction or decay silently,
so each is also asserted directly in the tests — including the severity ordering,
so a future edit cannot quietly restore the current backwards one.

## Two absences that are design decisions

Both are asserted by tests, because an absence nobody is watching gets filled in.

**No enforcement field on `BudgetUsage`.** No `budget_exceeded`, `limit`, `enforce`,
`quota` or `blocked`. This contract reports observed spend and confers no budget
authority; enforcement is M6's, at admission time, upstream — out of scope for this
unit even behind a flag. A boolean verdict field here would be the first step
toward a second, local enforcement decision that could disagree with the real one.

**No storage surface on `Lease`.** No table name, connection, DSN or row id. U15
withdraws the monitor's `reconcile_locks` write grant; a lease shape carrying any
of those would quietly preserve the dependency the withdrawal removes.

## Running the tests

```bash
# The whole contract suite
python3 -m pytest modules/domain-apps/superplane/tests/ -q

# The smoke check named in the story: per-workspace scoping, both directions
python3 -m pytest modules/domain-apps/superplane/tests/test_observation_scoping.py -q
```

No AWS credentials, no network, no database. That is what lets this run in
`superplane-domain-ci.yml`, which executes on `ubuntu-latest` with
`contents: read` and asserts at the end of the job that no AWS credential
environment variables are present.

## Scope boundary

**In:** the contract types, their versioning, the authentication and scoping rules,
the lease shape, the tests.

**Out:** the receiving server, its routes and persistence (U15, upstream); budget
enforcement (M6); any modification to `/internal/heartbeat`; withdrawal of the
`reconcile_locks` grant (U15's criterion).

`events/` is the other directory the module layout table assigns to U8. It stays
empty here: this unit is R11's contracts half, and no event schema is needed to
satisfy any of its criteria. Adding speculative schemas ahead of a consumer would
mean versioning something nothing reads yet.
