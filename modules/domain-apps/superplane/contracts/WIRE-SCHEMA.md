# Observation wire schema — `v1`

Issue #5043 (U8), EPIC #4910.

This is the normative description of what goes over the wire when a controller or
monitor submits an observation. The Python package in `superplane_contracts/` is
the executable form of this document; where the two disagree, the package's tests
are what actually gate CI, so treat a disagreement as a bug in this file and fix
it here.

The audience is two implementations that have to agree without sharing code: the
Go controller that sends, and the upstream receiver (U15, `src/superplane-api/`)
that parses. Neither can import the other's types, which is why the canonical
serialization rules below are stated exactly rather than left to each language's
default encoder.

---

## 1. Transport

Observations are submitted over HTTP POST with a JSON body.

**The existing `/internal/heartbeat` path is not reused, and is not modified by
this unit.** It carries `Content-Type` and nothing else, and its receiver has no
auth dependency — so the only thing needed to write a cluster's state is that
cluster's UUID. A UUID appears in logs, kubeconfigs, support tickets and any API
response that lists clusters. Extending that path would mean the authenticated
contract and the forgeable one share a route, and the weaker one would keep
working. The receiver, its route and its cutover are U15's.

### Required headers

| Header | Value | Why it exists separately from the body |
|---|---|---|
| `x-superplane-contract-version` | `v1` | Lets a receiver refuse an unsupported version *before* parsing a body it cannot safely interpret. |
| `authorization` | Deployment-defined credential | Establishes **who** is calling. |
| `x-superplane-signature` | `sha256=<hex>` | Establishes that the body is the body that caller sent. |

All three are required. Header names are matched case-insensitively — HTTP header
case is not significant, and a case-sensitive receiver would authenticate the same
submitter on one client and refuse it on another.

---

## 2. Body

### `Observation`

| Field | Type | Required | Notes |
|---|---|---|---|
| `contract_version` | string | yes | Must equal the header. See §4. |
| `kind` | `"fleet_health"` \| `"budget_usage"` | yes | Unknown kinds are refused, not ignored. |
| `subject` | object | yes | See `ClusterRef`. |
| `reported_at` | RFC 3339 timestamp with offset | yes | When the submission was assembled. |
| `reporter` | string | yes | A **display label**, not an identity. See §3. |
| `status` | `CheckStatus` | yes | The aggregate of `checks`. See §5. |
| `checks` | array of `CheckResult` | `fleet_health` only | Omitted when empty. At least one required for `fleet_health`. |
| `budget` | object | `budget_usage` only | See `BudgetUsage`. |
| `labels` | object of string→string | no | Omitted entirely when empty, rather than sent as `{}`. |

A submission is exactly one kind. `fleet_health` carrying `budget`, or
`budget_usage` carrying `checks`, is refused — a single payload asserting two
different sorts of thing has no single meaning to scope or store.

### `ClusterRef` (`subject`)

| Field | Type | Required |
|---|---|---|
| `cluster_id` | string | yes |
| `workspace` | string | yes |

**Both are required, and that is the point.** A bare cluster UUID is the forgeable
shape: it names a thing without naming whose thing it is, so there is nothing for
a scoping check to compare a submitter's grant against. Requiring the workspace in
the subject is what makes §3's scoping decision possible at all.

### `CheckResult` (each element of `checks`)

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Non-empty. |
| `status` | `CheckStatus` | yes | |
| `observed_at` | RFC 3339 with offset, or `null` | conditional | Required for every status **except** `not_checked`; must be `null` for `not_checked`. |
| `detail` | string or `null` | no | Constrained — see §5. |
| `error` | string or `null` | no | Must be `null` for `not_checked`. |
| `reason` | string or `null` | conditional | **Required** for `not_checked`; explains why the check did not run. |

All six keys are always present in the serialized form, with `null` for absent
values. A receiver may therefore read them positionally without checking for key
existence.

### `BudgetUsage` (`budget`)

| Field | Type | Required | Notes |
|---|---|---|---|
| `workspace` | string | yes | Must equal `subject.workspace`. |
| `window_start` | RFC 3339 with offset | yes | |
| `window_end` | RFC 3339 with offset | yes | Must be strictly after `window_start`. |
| `observed_spend_usd` | number | yes | Must be ≥ 0. |
| `currency` | string | yes | `"USD"` in `v1`. |

`budget.workspace` disagreeing with `subject.workspace` is refused rather than
reconciled. Reconciling would make whichever field the receiver happened to prefer
the tamperable one.

**There is no `budget_exceeded`, `limit`, `enforce`, `quota` or `blocked` field,
and adding one is a breaking change.** This contract reports *observed spend*. It
confers no budget authority. Enforcement is M6's, at admission time, upstream —
and out of scope for this unit even behind a flag. A boolean verdict field here
would be the first step toward a second, local enforcement decision that could
disagree with the real one; the absence is a design decision, and
`test_budget_payload_has_no_enforcement_field` asserts it.

---

## 3. Authentication and scoping are two decisions

A receiver must make both, in this order, and neither substitutes for the other.

**Authenticate** — `verify_submission()`. Requires a resolvable credential *and* a
valid body signature. Both, because either alone leaves a real gap: authenticated
but unsigned means the receiver knows who started the call but not what it now
says; signed but unauthenticated means the body is intact but its sender may have
no standing to speak for the subject.

**Scope** — `authorize_submit()` / `authorize_read()`. A submitter authenticated
for W1 cannot submit about W2's clusters and cannot read W2's observations. These
are separate functions on the same grant, deliberately: implementing either in
terms of the other is how one of them ends up unchecked, and scoping writes while
leaving reads open stops cross-tenant tampering while leaving cross-tenant
*disclosure* wide open. A fleet observation is an operational map of a tenant's
estate — which clusters exist, which are unreachable, what they cost.

Two rules that follow from this:

* **The grant never comes from the payload.** `reporter` is a display label. The
  authenticated identity is whatever the credential resolver returned. A payload
  naming itself `"platform-monitor"` proves nothing.
* **Fail closed everywhere.** An empty grant authorizes nothing. There is no branch
  that allows because something was absent — an empty grant most often means a
  resolver could not determine one, and reading "unknown" as "all" is how a
  misconfigured token becomes a tenant-boundary failure.

### Refusal reasons

Every scoping refusal is the identical string, `workspace not in submitter scope`,
whether the workspace is forbidden or does not exist. The difference between those
two is itself information about another tenant's estate, so a receiver that
distinguishes them is an enumeration oracle. Refusal reasons also never echo
caller-supplied values, which would reflect arbitrary input into the receiver's
logs and the caller's error surface.

### Signature computation

The signature is HMAC-SHA256 over the **exact transmitted UTF-8 JSON body
bytes**, hex-encoded as 64 lowercase ASCII digits, prefixed `sha256=`. Sign once
and send those same bytes; the receiver passes the raw body to `verify_submission`
before constructing domain objects. Never parse and re-encode before verification.
Go/Python number formatting, Unicode escapes, key order and timestamp rendering
may differ without breaking verification. `canonical_body` is only a convenient
Python sender serialization, not a portable canonicalization algorithm.

The receiver injects a trusted clock. `reported_at` must be timezone-aware and
within five minutes in the past / thirty seconds in the future by default. U15
must bind signing keys to resolved credentials, persist replay/idempotency or
monotonic-update state per authenticated cluster stream, and reject stale health
updates. A valid HMAC alone does not establish freshness or prevent replay inside
the time window. Body signatures never replace workspace authorization.

For writes, U15 must resolve cluster ownership independently from stored state and
pass `cluster_workspace` to `authorize_submit`. A missing record, mismatched
workspace, or claimed victim cluster with the caller's workspace is refused. Reads
must scope storage queries by authorized workspace, including cluster lookups.

After signature and freshness verification, `verify_submission` calls
`Observation.from_wire` to validate the received schema through the constructor
guards, including check honesty, budget values, and agreement of the reported
aggregate with the checks. A signed but dishonest body is refused. Success returns
the validated object in `AuthResult.observation`. U15 must use that object for
scoping and persistence; it must not substitute unvalidated JSON. Receivers in
other languages must enforce the same wire invariants before any state update.

---

## 4. Versioning

The version appears **twice**, in the header and in the body, and they must agree.

Both copies exist because each is load-bearing at a different moment. The header
lets a receiver refuse before parsing. The body copy is the one that survives
queueing, logging and replay — a body replayed from a queue has no headers, and
accepting a header-only version would make it unversioned at exactly the point
where nobody is watching.

Four refusals, each distinct so a sender is told what is actually wrong:

| Condition | Reason |
|---|---|
| Neither copy present | `missing contract version` |
| Header absent, body present | names the missing header |
| Body absent, header present | names the missing field |
| Copies disagree | `contract version mismatch` |
| Both agree, unsupported | `unsupported contract version: <v>` |

A disagreement is refused rather than resolved. Preferring either copy would let
whichever surface is easier to tamper with decide how the body is interpreted.
Absence is refused rather than defaulted — defaulting is the tempting behaviour
and the dangerous one, because it makes an unversioned sender work today and break
invisibly at the next bump.

**Version is checked before the credential is looked at.** A submitter sending a
version this receiver does not implement should be told that, rather than getting
an authentication error that sends it hunting in the wrong place; and the receiver
should not spend a credential verification on a submission it will refuse anyway.
`test_credential_is_not_consulted_when_version_is_bad` asserts the ordering, not
just the outcome.

### What a `v2` would require

Breaking changes — removing a field, narrowing a type, changing a field's meaning,
or adding a required field — need a new version string in `SUPPORTED_VERSIONS`,
with `v1` kept there for as long as any sender still speaks it. Adding an optional
field with a safe default is compatible and does not need a bump.

`to_wire()` is written by hand rather than derived from the dataclass precisely so
this stays true: with `asdict`, every attribute name would implicitly be part of
the public contract, and a routine rename would silently become a breaking change.
The wire-key assertions in `test_observation_contract.py` fail on such a rename.

---

## 5. `CheckStatus` and honest reporting

R11 acceptance 4: *a probe never reports health it did not check.*

| Status | Severity rank | Meaning |
|---|---|---|
| `healthy` | 0 | Checked, and fine. |
| `not_checked` | 1 | **Not checked.** Nothing was established. |
| `degraded` | 2 | Checked, working, impaired. |
| `unknown` | 3 | Checked; the result was indeterminate. |
| `unreachable` | 4 | Checked; contact failed. |

Three properties a receiver can rely on, each of which exists because the current
implementation gets it wrong:

**`not_checked` is expressible and distinct from healthy.** Today `eks_reachability`
is served by a no-op prober that returns `Healthy` without contacting anything —
partly because there was no honest value to return. There is one now. It requires a
`reason`, so "we did not check" always says why rather than being a shrug no
operator can act on, and it must not carry `observed_at`, `detail` or `error`, all
of which would be readings it does not have. A probe that ran and errored *did*
check: that is `unknown`, not `not_checked`, and the distinction preserves real
information — an erroring probe says the path is exercised and broken, an unrun one
says it is unconfigured.

**A positive claim cannot coexist with a failure.** `status: healthy` with a
non-null `error` does not validate. Nor does a positive `detail` — `synced`,
`reachable`, `ready`, `ok`, matched case-insensitively — on a non-positive status,
because any surface rendering the detail would show the reassuring string. This is
the `checkVaultSyncStatus` shape, which returns `"synced"` on every branch
including the one where listing Secrets failed. Under this contract that
combination is unconstructible, not merely discouraged — a probe author cannot
forget to run the validation that would have caught it.

**`unreachable` outranks `unknown`.** The current severity ordering has these
backwards, so a cluster nobody could reach aggregates as *less* severe than one
whose probe was indeterminate. Unreachable is a confirmed loss of contact; unknown
is not.

### Aggregation

`status` is the worst of `checks` by the ranks above, and **an empty check set
aggregates to `not_checked`, not `healthy`.** For a worst-wins reduction the
natural identity element is the *healthiest* value, which is exactly how a cluster
with zero configured probes comes to report green. A submission that checked
nothing has established nothing. One `not_checked` among otherwise-green checks
prevents a healthy aggregate, for the same reason.

The aggregate travels on the wire so a receiver storing only the summary cannot
disagree with the submitter about what the checks reduced to.

---

## 6. Leases

For serializing reconcile work **without a `reconcile_locks` table grant** — U15
withdraws the monitor's write access to that table, and a monitor that lost it
without another way to serialize itself would not be safer, just concurrently
wrong.

| Field | Type | Notes |
|---|---|---|
| `scope` | string | Opaque name both ends agree on. Carries no schema meaning. |
| `holder` | string | Non-empty; checked exactly (not by prefix) on release. |
| `expires_at` | RFC 3339 with offset | Set from the **receiver's** clock. |
| `fence_token` | integer ≥ 1 | Monotonic. Starts at 1, so 0 is never a held token. |

Three properties, and the third is the one usually missed:

* **Expiry** — bounded, ≤ 15 minutes, so a monitor that dies mid-reconcile does not
  hold the scope until an operator intervenes. The boundary is inclusive: at the
  expiry instant the lease is gone. Expiry is decided by the receiver's clock — a
  lease having expired according to the *holder's* clock is not a fact the holder
  gets to assert.
* **Ownership on release** — only the holder releases. Freeing another monitor's
  lease would induce the exact concurrency the lease prevents, and two reconcilers
  running is not an error either of them reports.
* **Fencing** — expiry alone leaves a window where the previous holder still
  believes it holds the lease (clock skew, a network stall, a long GC pause) while a
  new holder legitimately does, and both then act. A receiver refuses work whose
  token is **strictly less than** the highest token it has seen for that scope.
  Strictly less, not less-or-equal: the current holder's token *equals* the highest
  seen and must keep working.

The token is assigned when the lease is granted, never chosen by the requester —
`LeaseRequest` has no token field. A requester-supplied token is not a fence, just
a field the requester sets to whatever gets its work accepted.

---

## 7. Scope of this unit

**In:** the contract types, their versioning, the authentication and scoping rules,
the lease shape, and the tests that hold all of it.

**Out:** the receiving server, its routes and its persistence (U15, upstream);
budget *enforcement* (M6); any change to `/internal/heartbeat`; withdrawal of the
`reconcile_locks` grant (U15's criterion, not this unit's).

Everything here is verifiable offline. The test suite needs no AWS credentials, no
network and no database, which is what lets it run in the credential-free
`superplane-domain-ci.yml` lane.
