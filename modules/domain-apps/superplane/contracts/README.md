# `contracts/` — Superplane domain contracts

Two units share this package, both under EPIC #4910:

| Unit | Issue | Requirement | What it adds |
|---|---|---|---|
| **U8** | #5043 | R11 | observation contracts: versioning, auth, scoping, leases |
| **U11** | #5049 | R15 (A's half) | durable handles, reconciliation, provider-truth reporting |

They sit together because U11's release reporting is the same kind of thing as
U8's observation reporting: a statement about the world that must not be able to
claim more than was observed. `WIRE-SCHEMA.md` is the normative field-by-field
description of U8's wire form; this file is the orientation for both.

# U8 — observation contracts

The versioned contracts through which controllers and monitors submit fleet-health
and budget observations.

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
    handles.py                <- U11: provider-operation identity, recorded pre-call
    reconciliation.py         <- U11: what an ambiguous outcome actually was
    accounting.py             <- U11: no release/cost clearance while unresolved
    provider_truth.py         <- U11: teardown reports with a non-zero result
    adapter.py                <- U11: the record-then-call ordering, in one place
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

# U8's smoke check: per-workspace scoping, both directions
python3 -m pytest modules/domain-apps/superplane/tests/test_observation_scoping.py -q

# U11's smoke check: handles, reconciliation and provider-truth reporting
python3 -m pytest modules/domain-apps/superplane/tests/test_handle_reconciliation.py -q
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

# U11 — durable handles, reconciliation and provider truth

Unit **U11** (issue #5049), requirement **R15**, A's half.

## The problem this exists to fix

`Onboarder.Onboard` in the pinned reference snapshot asks SkyPilot to launch a
cluster and receives the launch identifier **only on the success path**
(`provisioner/onboarder.go:179-186`). When the call times out there is no
identifier to report, so the result says `Success: false` and carries no provider
reference at all. `Provisioner.provisionNode` reads that as a failure, logs
"onboarding failed, trying next option", and launches on the next cloud
(`controllers/provisioner.go:265-272`).

A timeout is not evidence that nothing was created. So the sequence is: ask a
provider for a GPU machine, lose the response, conclude failure, ask a *different*
provider for a GPU machine — and the first one, if it came up, is running and
billing with nothing in the system holding a reference to it. Nothing reports it,
because from the controller's point of view the launch failed.

The same substitution appears on the teardown side. `consolidator.go:419-425` logs
`"failed to delete K8s node, continuing anyway"` and then advances the phase to
`Terminated`; the delete failed and the status field now says the node is gone.
Anything reading that field — a cost attribution, a reservation return, an
operator's dashboard — inherits a conclusion no observation supports.

## What each criterion is held up by

| Criterion | Mechanism |
|---|---|
| **R15 acc. 5** — durable identity before the call | `authorize_provider_call()` refuses while `HandleRecord.durable` is false, and `durable=True` additionally requires `confirmed_at` — persistence's acknowledgement instant, which a caller cannot produce by setting a boolean. |
| **R15 acc. 6** — a lost response is an unknown | `CallOutcome.AMBIGUOUS` is a third outcome alongside succeeded/failed. `reconcile()` reaches a conclusion only from a `ProviderObservation`, and `RETRY_PERMITTED` — the sole route to a repeat — requires provider-established absence. No observation yields `UNRESOLVED`, which authorizes nothing. |
| **R15 acc. 7** — no premature clearance | `assess_release()` derives `ReleaseState` and `CostExposure` from provider observations only, with `UNKNOWN` outranking `PRESENT`. `may_mark_released` and `may_return_reservation_unused` are separate gates, and `CostExposure.NONE` is unreachable without established absence for every resource. |
| **R15 acc. 3** — cleanup failure reported as failure | `TeardownReport.exit_code` counts findings and returns non-zero, matching `deprovision-gpu-node-aws.sh`'s re-check/count/`exit ${ERRORS}` standard. Capped at 125 so a count cannot collide with the shell's reserved 126/127/128+n. |
| **R15 acc. 2** — accident vs. retirement | `ReleaseIntent.recreation_expected` answers it directly, and a deliberate release must record a requester and stop **every** `RecreationDriver` — auto-repair (`health_monitor.go:279-333`) and the unschedulable-pod path (`pod_watcher.go:297-331`), both registered in `main.go`. |

## Ownership, and what is mocked

A performs adapter-side bookkeeping under **B's** authority. B owns the operation
lifecycle, cancellation ordering, leases/fencing and the recovery worker; **C**
owns the reservation ledger; **U11c** owns upstream handle persistence; **U11b**
owns the four upstream Go controllers, untouched here.

None of B's machinery exists in ADP today — there is no lease, fencing or
`attempt_id` implementation to call. So `OperationAuthority` and `HandleStore` are
`Protocol`s A calls across rather than classes A ships, the test suite supplies a
mock authority, and `tests/test_handle_reconciliation.py` records it as a mock in
its module docstring.

## Three absences that are design decisions

All three are asserted by tests, for the same reason U8's two are.

**No scheduler, timer, queue or thread.** R15 acceptance 8 — stops and cleanup
working after the agent process is gone — is met by B's independent-lifetime driver
calling `adapter.release_allocation()`, not by A acquiring a lifetime of its own. A
second lifecycle owner is what the ownership split forbids.

**No retry inside the adapter.** `perform_operation` returns a decision and lets
the caller act on `may_repeat_operation`. An adapter that looped internally would
be making retry decisions B owns.

**No balance, spend total or reservation arithmetic.** C owns the ledger. A reports
whether a clearance is permitted, so `CostExposure` is a three-way category rather
than a number — A cannot know the dollar figure for a resource it could not
observe, but it can refuse to let the exposure be recorded as zero.

## Fixtures

`tests/fixtures/provider-responses.json` is **generated** by
`generate-provider-responses.py`, from the SkyPilot client's own `json:` struct
tags in the pinned snapshot and from botocore's `DescribeInstances` output shape.
Provenance is in `tests/fixtures/provider-responses.md`.

Generated rather than written, because the rule is that fixtures must come from
the provider's response models and never from what the adapter expects — and a
hand-written file looks identical either way. A generator fails when the model
disagrees; review is the wrong instrument for that check.

## Deferred live criteria

R15 acceptances **1, 5, 6 and 8** also have live criteria: a real deletion in a
named account, a real crash mid-provision, a real lost response, and stop/cleanup
with the agent process gone. They need a named account and environment, spend
authorization, a deadline and a named cleanup owner — **all unresolved**, and none
invented here. Everything above is verified offline against recorded responses, and
that is the whole extent of the claim.

## Scope boundary

**In:** the contract types, the adapter bookkeeping path, provider-truth reporting,
the tests and their generated fixtures.

**Out:** the operation lifecycle, cancellation ordering, leases/fencing and the
recovery worker (B); the reservation ledger (C); upstream handle persistence
(U11c); the four Go controllers (U11b); any local Jobs, approval or budget
authority, or scheduler of A's own.


### Allocation membership in release evidence (U11)

A release report requires `AllocationResources(allocation_id, resource_ids)` from
B's authorized cleanup driver. The resource set comes from authoritative upstream
allocation records (persisted by U11c), independently of the provider response;
it must include compute, storage and network resources. Do not derive it from
`observe_allocation()` or its returned keys. This is an input contract, not a new
registry or ledger implemented by A; C still owns accounting changes.

`release_allocation(..., allocation_resources=inventory)` checks the inventory's
allocation ID before calling the provider. `assess_release(..., allocation=inventory)`
requires every expected resource and rejects foreign or mismatched evidence as
unresolved. Missing resources produce actionable findings and continuing exposure.
The assessment also carries its allocation ID, so a teardown report cannot relabel
another allocation's result. The caller must reconcile incomplete membership before
claiming a clean release. Empty provider results do not imply an empty allocation.

These checks enforce the supplied contract; they cannot attest to inventory provenance
or live provider completeness. The real upstream registry and B's independent-lifetime
cleanup driver remain required for R15 live acceptance.
