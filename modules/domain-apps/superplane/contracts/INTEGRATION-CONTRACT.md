# Integration contract — production ports, admission ordering and startup composition

Issue [#5524](https://github.com/aws-e/adp/issues/5524) (w6-01), EPIC
[#4910](https://github.com/aws-e/adp/issues/4910), Wave 6. Reconciles
[#4912](https://github.com/aws-e/adp/issues/4912) (EPIC B — shared jobs,
approvals, credentials) and [#5400](https://github.com/aws-e/adp/issues/5400)
(EPIC A1 — managed workspace EKS lifecycle) into this wave.

**Source baseline.** ADP `f30bb66f299276a6cfe3b4a05150e63bbbda7e42`
(2026-09-19 20:08:48 +0100), the baseline every Wave 6 story records. This branch
is based on `b49d6cf334f72bc397b0f378aa6584512dbcfe9b`, a descendant of it. Every
`file:line` citation below resolves at that head. Line numbers shift; the tests in
`../tests/test_integration_contract.py` re-resolve each `declared_at` citation on
every run, so a stale one fails rather than misleads.

The machine-readable half of this document is
[`superplane_contracts/integration.py`](superplane_contracts/integration.py).
Where the two could disagree, **the module is normative and this prose explains
it** — the module is what tests check.

---

## Why this document exists

Wave 6 has sixteen sibling stories that between them supply the production
implementations behind ports the domain app already declares. The ports exist; the
implementations do not. Eleven ports are declared across this module, and every
one of them is satisfied today only by a test double or by a module global holding
`None`.

That is a safe state and an unspecifiable one. Nothing written down says what a
correct implementation must carry, demand or refuse — so sixteen developers
working from sixteen readings of the same prose produce sixteen subtly different
adapters, and the failures that follows are the expensive ones the story names:
duplicated spend, leaked resources, an operation admitted without the approval it
claimed.

Three things had to be true for this to be worth writing:

1. It has to be **checkable**. Prose agrees with code exactly until the first
   change nobody propagated, and that divergence is silent. So the port map is
   data in `integration.py`, every entry cites the line that declares the port,
   and `../tests/test_integration_contract.py` opens each citation.
2. It has to say what happens when the truth is **unknown**. The most damaging
   mistake at these boundaries is not a crash, it is a confident wrong answer.
3. It has to name a **live verifier** for each capability, so no criterion ends at
   a mocked adapter. That mapping is
   [`REQUIREMENTS-MATRIX.md`](REQUIREMENTS-MATRIX.md).

---

## 1. The production port map

Eleven ports, five owners. "Owner" is who supplies the production implementation
— **not** who calls it. The distinction that matters is `domain` versus everything
else: a port owned elsewhere cannot be satisfied by code this wave writes. If the
domain app supplied its own `operation_facade` it would be authorizing its own
operations, which is the separation the facade exists to create.

`acts_as` is the principal the implementation must act as. It is spelled out per
port because the recurring integration defect is an adapter that acts as *the
request body's* claimed identity rather than as its own resolved one — which is
how a forged `org_id` becomes real authority.

`unknown answer` is the shape the port must produce when it cannot establish the
truth. Mandatory on every entry; see §2.

### 1.1 Shared durable execution — owner `harness_jobs` (#4912)

| Port | Declared at | Binds | Permission | Acts as | Unknown answer |
|---|---|---|---|---|---|
| `operation_facade` | [`src/superplane-api/app/services/provisioning.py:226`](../src/superplane-api/app/services/provisioning.py) | `operation_id`, `workspace_id`, `org_id` | `workspace:provision` | the facade's own resolved principal, never the request body's `org_id` | raise unavailable |
| `operation_authority` | [`superplane_contracts/adapter.py:107`](superplane_contracts/adapter.py) | `allocation_id` | `workspace:provision` | the operation's holder under B's active lease and fence | `None` means unverified |
| `provider_authority` | [`src/superplane-api/app/services/provider_authority.py:35`](../src/superplane-api/app/services/provider_authority.py) | `operation_id`, `run_id`, `attempt_id`, `submitter_id` | `workspace:provision` | B's verified operation record, not the presented handle's scope | `None` means unverified |
| `allocation_inventory` | [`src/superplane-api/app/services/provider_inventory.py:38`](../src/superplane-api/app/services/provider_inventory.py) | `allocation_id`, `workspace`, `operation_authority` | `workspace:provision` | B's fenced cleanup authority for this executor | `None` means unverified |
| `handle_store` | [`superplane_contracts/adapter.py:88`](superplane_contracts/adapter.py) | `idempotency_key`, `allocation_id`, `workspace` | — | the domain persistence owner, acknowledging its own write | `None` means unverified |

`handle_store` carries no permission because it is **not an authorization
boundary** — it is persistence acknowledging its own write. A permission here
would suggest a caller could be authorized *into* durability, and durability is a
fact about storage, not a grant.

### 1.2 Gateway credential authority — owner `gateway_vault` (#4912)

| Port | Declared at | Binds | Permission | Acts as | Unknown answer |
|---|---|---|---|---|---|
| `credential_evidence` | [`src/superplane-api/app/services/credential_evidence.py:26`](../src/superplane-api/app/services/credential_evidence.py) | `org_id`, `workspace_id`, `credential_reference` | `workspace:renew_credential` | the vault's own ownership record, never the request's digest claim | `None` means unverified |
| `trusted_delivery` | [`superplane_contracts/delivery.py:412`](superplane_contracts/delivery.py) | `lease_id`, `operation_id`, `workspace_id`, `recipient` | `workspace:provision` | the lease's bound principal, re-checked at the operation | raise unavailable |

"Never the request's digest claim" is the whole of `credential_evidence`'s value.
A caller can compute any digest it likes; the port's job is to report a binding
**the vault independently verified** between a validation report and this exact
credential and workspace. An implementation that echoes the submitted digest back
as proof satisfies the type and establishes nothing.

### 1.3 The provider — owner `provider`

| Port | Declared at | Binds | Permission | Acts as | Unknown answer |
|---|---|---|---|---|---|
| `provider_client` | [`superplane_contracts/adapter.py:120`](superplane_contracts/adapter.py) | `idempotency_key`, `resource_name`, `allocation_id` | — | the provider credential delivered for this operation | unresolved value |
| `provider_operation` | [`superplane_contracts/delivery.py:446`](superplane_contracts/delivery.py) | `provider`, `provider_account_id`, `operation` | `workspace:provision` | the leased credential, for the single bound action | unresolved value |

The provider is the **only** source of provider truth, which is why these two are
the ports whose unknown answer is a typed unresolved *value* rather than a
refusal: "I could not reach the provider" is information the caller must be able
to hold and act on, and an exception forces it to be either swallowed or fatal.

### 1.4 Managed workspace infrastructure — owner `workspace_infra` (#5400)

| Port | Declared at | Binds | Permission | Acts as | Unknown answer |
|---|---|---|---|---|---|
| `provisioning_provider` | [`superplane_contracts/provisioning_adapter.py:122`](superplane_contracts/provisioning_adapter.py) | `operation_id`, `workspace_id`, `org_id` | `workspace:provision` | the operation binding's principal, for the bound action only | raise unavailable |

"For the bound action only" is #5400's requirement restated as a port obligation:
a teardown running under a binding authorized for provision, or the reverse, is a
destructive mismatch. The local adapter already refuses it
([`provisioning_adapter.py:173-180`](superplane_contracts/provisioning_adapter.py));
the provider must not restore it.

### 1.5 The domain app — owner `domain`

| Port | Declared at | Binds | Permission | Acts as | Unknown answer |
|---|---|---|---|---|---|
| `submitter_resolver` | [`superplane_contracts/auth.py:93`](superplane_contracts/auth.py) | `credential`, `workspaces` | — | the configured submitter grant, compared in constant time | `None` means unverified |

The only locally-owned port, and the only one already implemented:
`ConfiguredSubmitterResolver`
([`src/superplane-api/app/services/observations.py:102`](../src/superplane-api/app/services/observations.py)).
Its registry entry names no live verifier, deliberately — naming one would claim
an obligation nobody owes.

### 1.6 Actual current callers

The story requires "every production port and **actual current caller**". Naming
the caller is what makes an abstract obligation a concrete outage: a port with a
live caller is one whose absence a tenant already experiences.

| Port | Actual current caller | Endpoint reached | Behaviour with the port absent |
|---|---|---|---|
| `credential_evidence` | [`routers/provider_connections.py:136`](../src/superplane-api/app/routers/provider_connections.py) via `_vault_evidence`, called from `_authorize` (`:343`), `register_connection` (`:527`), `record_validation` (`:606`), `rotate_connection` (`:691`) | `POST/GET/DELETE /workspaces/{id}/provider-connections[/{cid}][/validation|/rotation]` | HTTP 503 "ADP vault evidence is unavailable" |
| `provider_authority` | [`services/provider_handles.py:252`](../src/superplane-api/app/services/provider_handles.py) (`_verify_authority`) | `POST /internal/provider-operations`, `POST /internal/provider-operations/{key}/conclude` | HTTP 503 "B operation authority is unavailable" |
| `allocation_inventory` | [`services/provider_handles.py:780`](../src/superplane-api/app/services/provider_handles.py) | `POST /internal/provider-operations/allocations/{id}/release-assessment` | `unresolved(...)` — exposure stays on the books |
| `operation_facade` | [`services/provisioning.py:317`](../src/superplane-api/app/services/provisioning.py) and `:389` via `_require_facade`, reached from [`routers/workspaces.py:153`](../src/superplane-api/app/routers/workspaces.py) and `:288` | `POST /workspaces`, `DELETE /workspaces/{id}` | `ProvisioningRefused` — workspace create/destroy refuses |
| `operation_authority`, `handle_store`, `provider_client` | `ProviderAdapter` ([`superplane_contracts/adapter.py:180`](superplane_contracts/adapter.py)) — constructor parameters; no production construction site | — | not constructible |
| `trusted_delivery`, `provider_operation` | `ProviderExecutor` ([`superplane_contracts/delivery_executor.py:240`](superplane_contracts/delivery_executor.py)) — constructor parameters; no production construction site | — | not constructible |
| `provisioning_provider` | `ProvisioningAdapter` ([`superplane_contracts/provisioning_adapter.py:139`](superplane_contracts/provisioning_adapter.py)) — constructor parameter; no production construction site | — | not constructible |
| `submitter_resolver` | `load_submitters` ([`services/observations.py:147`](../src/superplane-api/app/services/observations.py)) | `POST /internal/observations/...` | implemented |

Two classes of caller, and the difference is worth stating because it changes what
"compose this port" means:

- **Four ports have live HTTP callers.** They are module globals holding `None`,
  each fetched through a getter at its call site, each already producing a
  specific refusal. Composing them means installing an adapter into an existing
  singleton — which is why exactly these four are the capability readout's subject
  (§4) and why `API_CAPABILITY_PORTS` is a four-element subset rather than all
  eleven.
- **Seven are constructor parameters of aggregates nothing constructs in
  production.** Composing them means someone writing a construction site that does
  not exist yet. A capability readout naming these would claim the API server
  checks things it never touches.

### 1.7 Identifiers, leases, fences, approval, digests

The story enumerates what the map must cover. Where each lives:

| Concept | Where it is bound | Note |
|---|---|---|
| `operation_id` | `operation_facade`, `provider_authority`, `trusted_delivery`, `provisioning_provider` | |
| `attempt_id` / `run_id` | `provider_authority` | Minted by B. `authority_attempt_id` is *persisted* at [`models/provider_handle.py:164`](../src/superplane-api/app/models/provider_handle.py) — stored, never minted here. [`handles.py:58-64`](superplane_contracts/handles.py): "No retry counter, no backoff state, no attempt sequencing." |
| `allocation_id` | `operation_authority`, `allocation_inventory`, `handle_store`, `provider_client` | |
| `idempotency_key` | `handle_store`, `provider_client` | Uniqueness is PK `(workspace, idempotency_key)`; see §3.2 |
| Leases and fences | `operation_authority` ("under B's active lease and fence"), `allocation_inventory` ("current recovery fence"), `trusted_delivery` (`lease_id`) | Shape only: [`superplane_contracts/leases.py`](superplane_contracts/leases.py). **No lease, fencing or attempt implementation exists in ADP today** ([`reconciliation.py:55-72`](superplane_contracts/reconciliation.py)) |
| Current approval | **Not a port here.** `#5526` (w6-03) owns approval binding and recheck at decision *and* admission | §3 specifies the ordering it must fit into |
| Report digests | `credential_evidence` (`report_digest`), `allocation_inventory` (`report_digest`) | Both ports' `acts_as` forbids treating the submitted digest as proof |
| Unknown states | every entry's `unknown_outcome` | §2 |
| Version compatibility | every entry's `contract_version`, checked by `check_port_version` | §2.2 |

---

## 2. Unknown is a required answer

`unknown_outcome` is mandatory on every registry entry, and
`PortContract.__post_init__` refuses an entry without one. There is no validator a
registry author can forget to call, because the constructor is the validator.

Three shapes, because the safe answer differs by port:

| Shape | Meaning | Ports |
|---|---|---|
| `unresolved_value` | Return a typed value whose state is explicitly unresolved | `provider_client`, `provider_operation` |
| `none_means_unverified` | Return `None`; the caller must treat it as a denial | `operation_authority`, `provider_authority`, `allocation_inventory`, `handle_store`, `credential_evidence`, `submitter_resolver` |
| `raise_unavailable` | Raise; there is no value that would be honest | `operation_facade`, `trusted_delivery`, `provisioning_provider` |

**`None` is a denial, never a default.** An unanswered question is not permission.
The existing contracts already model this well in places —
`ReconcileResult.UNRESOLVED` ([`reconciliation.py:165`](superplane_contracts/reconciliation.py)),
`CostExposure.UNRESOLVED` ([`accounting.py:71`](superplane_contracts/accounting.py)),
`CheckStatus.NOT_CHECKED`, `OperationProgress` with `state="unknown"`. The
registry's job is to make it uniform across sixteen implementations instead of
leaving each to re-derive it, because the ones that forget will not fail loudly.

### 2.1 Why `raise` is right for exactly three ports

`operation_facade`, `trusted_delivery` and `provisioning_provider` are the ports
where **there is no value that would be honest.** A facade that cannot open an
operation has not opened one; returning any `OperationProgress` would be
manufacturing a record of an operation that does not exist. A delivery channel
that cannot deliver must not return a handle to nothing. A provisioning provider
that cannot provision must not report progress on infrastructure it never touched.

For the other eight, an unresolved value is strictly more useful than an
exception, because the caller has real work to do with the answer — record the
exposure, keep the allocation on the books, refuse the admission — and an
exception pushes that decision into a `try` block where it gets flattened.

Those three ports additionally declare **`refusal_exceptions`**: the exception type
names that count as the refusal. Mandatory on `raise_unavailable` entries and
forbidden on the rest, enforced in `PortContract.__post_init__`.

| Port | Declared refusals |
|---|---|
| `operation_facade` | `ProvisioningUnavailable`, `ProvisioningRefused` |
| `trusted_delivery` | `DeliveryRefused` |
| `provisioning_provider` | `ProvisioningRefused` |

Without this, "raise" means "raise anything", and these ports become **unfailable**:
an `OSError` from a broken client, an `ArithmeticError` from a bug, a timeout —
every one reads as correct behaviour. The conformance layer matches a raised
exception against this allowlist through `type(raised).__mro__`, so a more specific
subclass still counts while an unrelated crash is reported as `FAILED` (§4.4).

They are **names, not types**, because the real types live in `app.services.*` and
this package must not import `app.*` (§2.3). The tradeoff is accepted knowingly: a
rename that misses this registry turns a declared refusal into a `FAILED` verdict,
which fails closed and is visible, rather than silently widening what passes.

### 2.2 Version compatibility

Every entry carries `contract_version` (currently `v1`, from
[`version.py:33`](superplane_contracts/version.py)) and `check_port_version(name,
declared)` refuses a mismatch rather than interpreting the parts it recognizes. It
delegates to `version.check_version`, so the missing/blank/mismatched/unsupported
rules are stated once. Absent, blank, mismatched and unsupported **all refuse**;
whitespace is stripped, but `"V1"` and `"1"` are rejected.

A second nearly-identical comparison in this module would drift, and the drift
would be invisible because each copy would still pass its own tests.

Separately, every entry carries **`carries_contract_version`** — whether a call
*through* that port exchanges a version at all. It is `False` on all eleven,
because `grep -n contract_version` across `adapter.py`, `delivery.py`,
`provisioning_adapter.py` and `auth.py` finds nothing: the version discipline above
governs the contract *package*, and no port's method signature carries a version
argument today.

The flag exists because the two are easy to conflate, and conflating them produced
a false pass. `check_port_version` is a function a caller invokes with a version it
already has; it is not evidence that any adapter was ever *sent* one. An earlier
revision of the conformance layer assumed the latter and probed all eleven ports
with a stale version, reporting each as having correctly refused it — when in fact
the adapter ignored an argument it never received. A story that later adds a
version to its port's call sets this to `True` and the stale-version probe starts
being exercised for real. See §4.3.

### 2.3 What the registry deliberately is not

It records no implementation, no import and no factory. `integration.py` imports
nothing from `app.*`, holds no reference to any adapter, and cannot construct one.
A registry able to hand back an implementation would be a second composition root,
and a port could then be satisfied by something this package chose rather than by
something a reviewed startup composition installed.

For the same reason **there is no `implemented` or `ready` boolean.** Whether a
port is composed is a property of a running process, answered by §4 against the
adapters actually installed. A flag in a source file would be a claim about the
world that no observation supports, kept truthful only by someone remembering to
edit it.

---

## 3. Reserve → confirm → durable enqueue

#4912's design states the ordering: *"Harness orders reserve → confirm → enqueue
via an idempotent domain ledger hook keyed by job/attempt. Admission/outbox commit
within the harness store, not across databases."* #5526 (w6-03) owns approval and
admission; #5525 (w6-02) owns the store and outbox. This section specifies the
contract **between** them and the domain, and it is deliberately a specification
of obligations rather than an implementation: implementing it in the domain API
would be implementing shared jobs in the domain API, which this story is forbidden
to do.

### 3.1 The sequence, and where the single transaction boundary is

```
  (1) reserve     domain budget hook, idempotent on (job_id, attempt_id)
        |         → reservation held. NOT a spend. Compensatable.
        v
  (2) confirm     domain budget hook, idempotent on the same key
        |         → the approved aggregate envelope is now bound to this attempt
        v
  ┌───────────────────────── ONE transaction, in B's store ─────────────────────┐
  │ (3a) admission record written                                               │
  │ (3b) outbox row written                                                     │
  └─────────────────────────────────────────────────────────────────────────────┘
        |         commit is the durability point. Before it: nothing happened.
        v         After it: the operation exists and will be delivered.
  (4) deliver     outbox → executor. At-least-once, duplicate-safe, resumable.
        |
        v
  (5) provider    record handle, THEN call. See §3.3.
```

**There is exactly one transaction and it spans exactly (3a)+(3b).** That is the
whole of the no-distributed-transactions requirement: admission and outbox are two
rows in one store, so one commit makes both durable, and no two-phase protocol is
needed because there is no second participant. Steps (1), (2), (4) and (5) cross
process or service boundaries and are therefore **not** in it — they are made safe
by idempotency and reconciliation instead.

Searched for and confirmed absent, module-wide: any two-phase-commit, saga,
compensating-transaction coordinator, or XA participant. The substitutes that exist
are single-store row locks
([`services/provider_handles.py:196`](../src/superplane-api/app/services/provider_handles.py)
`_lock_allocation`), a TTL lock table
([`models/reconcile_lock.py:11`](../src/superplane-api/app/models/reconcile_lock.py)),
lease fence tokens ([`models/observation.py:88`](../src/superplane-api/app/models/observation.py)),
and refusal-based ordering (§3.3). **Do not add a coordinator.** The ordering plus
idempotency plus provider-truth reconciliation is the design, not a stopgap.

### 3.2 Idempotency: what the key is and what enforces it

An idempotent hook is one where a retry with the same key **returns the same
answer without producing a second effect**. Two properties, and only the first is
usually implemented:

1. The repeat must not create a second reservation, admission or outbox row.
2. A repeat with the **same key and a changed payload** must be **refused**, not
   honoured. Silently honouring it is how a retry becomes a budget increase: the
   caller resubmits with a larger envelope under the key that already passed
   approval.

The enforcement that exists today, and the shape to follow:

- **Uniqueness is a primary key, not application logic.** `provider_operations`
  has composite PK `(workspace, idempotency_key)`
  ([`alembic/versions/013_add_provider_operations.py:65,73-79`](../src/superplane-api/alembic/versions/013_add_provider_operations.py)).
  There is no unique *index*; the PK is the constraint.
- **The conflict IS the detection.** `record_handle`
  ([`services/provider_handles.py:283-342`](../src/superplane-api/app/services/provider_handles.py))
  lets the `IntegrityError` happen, rolls back, and raises `HandleRefused(...,
  409)`. It does not pre-check with a `SELECT`, because a pre-check has a race and
  the constraint does not.
- **The workspace is in the key deliberately.** Derived keys like
  `sp-aws-a100-1` contain no tenant, so a globally-unique key let one tenant's row
  refuse another tenant's operation — a cross-tenant denial of service through a
  name collision. Recorded at
  [`013_add_provider_operations.py:20-26`](../src/superplane-api/alembic/versions/013_add_provider_operations.py)
  and at length in
  [`models/provider_handle.py:32-56`](../src/superplane-api/app/models/provider_handle.py);
  pinned by `test_two_workspaces_may_hold_the_same_derived_idempotency_key`.
- **A retry that landed is distinguishable from a retry that wrote twice.**
  `conclude_operation` returns `(row, applied: bool, ReconcileResult)`, where
  `applied=False` means "an identical conclusion was already recorded — the retry
  landed, it did not write twice"
  ([`services/provider_handles.py:499-501`](../src/superplane-api/app/services/provider_handles.py)).

The domain budget hooks in (1) and (2) must be keyed on `(job_id, attempt_id)` per
#4912, and must satisfy both properties above. **Neither hook exists today.** See
§3.6.

### 3.3 Durable enqueue, and the record-then-call rule

Step (5) is already contracted, and it is the pattern the outbox in (3b) must
match. From [`adapter.py:24-34`](superplane_contracts/adapter.py):

> 1. Build the handle from locally-available identity … 2. Persist it and require
> the store's acknowledgement. 3. `authorize_provider_call` — refuse to proceed
> without durability. 4. Only now invoke the provider.
>
> A crash at any point after step 2 leaves a findable record, which is what makes
> step 4's lost response reconcilable instead of invisible.

Three properties make that more than a comment:

- **`HandleStore.record` must not return until the handle is durable**, and its
  returned `confirmed_at` is the acknowledgement `HandleRecord` requires
  ([`adapter.py:92-100`](superplane_contracts/adapter.py)). A store that returns
  eagerly is refusing to make the claim rather than quietly making a false one.
- **`durable=True` is unconstructible without `confirmed_at`**, and it must be
  timezone-aware ([`handles.py:183-191`](superplane_contracts/handles.py)). A
  caller cannot produce durability by setting a boolean.
- **`authorize_provider_call` has no permitting branch for "persistence was
  attempted"** ([`handles.py:212-230`](superplane_contracts/handles.py)). Not
  durable → `CallDecision(permitted=False, reason="handle is not durably recorded;
  a lost response would leave no reference to reconcile against")`.

Server-side, `record_handle` returns `durable=True, confirmed_at=recorded_at`
**only after `await db.commit()`**
([`services/provider_handles.py:329,342`](../src/superplane-api/app/services/provider_handles.py)):
"the caller cannot obtain a record that `authorize_provider_call` will accept
without a row having actually reached storage."

The closest queue-shaped precedent in this module is
[`agent/hosting/superplane_hosting/handoff.py:43-58`](../agent/hosting/superplane_hosting/handoff.py),
whose `Step`/`ORDER` encodes `durable state written → durable handoff sent → input
message deleted LAST`. **Delete-last is the rule for (4):** the outbox row is
removed or marked delivered only after delivery is itself durable, which is what
makes at-least-once delivery a duplicate problem (solvable by §3.2) rather than a
loss problem (not solvable at all).

**`AuditMiddleware` is not a usable substrate and must not be reused as one.**
[`middleware/audit.py:42-95`](../src/superplane-api/app/middleware/audit.py) writes
*after* `call_next`, skips on `status >= 400`, skips silently when `org_id` is
unextractable, uses a **separate session**, and swallows every exception with
"Audit logging should never break the request". Every one of those is correct for a
best-effort audit log and disqualifying for an outbox. The `events` table has no
`published_at`, `processed` or `attempts` column: it is an audit log, not an
outbox.

### 3.4 Storage and migration ownership

| Store | Owner | Holds | Migration path |
|---|---|---|---|
| B's harness store | #4912 / #5525 (w6-02) | job/operation/attempt identity, admission records, outbox rows, leases and fences | B's own, in `modules/harness/jobs/`. **Not this module's, and not reachable from it** |
| Superplane domain database | this module | `provider_operations` and its conflict/resource children, workspace/org records, observation receipts and leases, provider connections and bindings | [`src/superplane-api/alembic/`](../src/superplane-api/alembic/), sole head `017_add_workspace_bootstrap_reservations` |
| Gateway vault | #4912 / #5528 (w6-05) | credential material; Secrets Manager reads stay in Gateway | Gateway's own |

`src/superplane-api/alembic/` is the **only** migration directory this module owns.
It belongs to the API's own database, does not touch the gateway's migrations or
any shared schema, and nothing in the transferred source or the build lanes can
reach them
([`src/TRANSFER-MANIFEST.md`](../src/TRANSFER-MANIFEST.md) § "Migration ownership
is unchanged").

**Authoring a migration is not applying it.** `013_add_provider_operations.py:28-31`
states it plainly: *"It has not been applied anywhere. U11c authors the schema;
U23 (#5327) owns deployment, and merging this story authorizes no migration run."*
That split holds for every Wave 6 story. The single-head invariant is enforced by
`tests/test_migrations.py::test_exactly_one_head`, so two stories adding a
revision in parallel fail CI rather than producing a silent branch — **coordinate
revision numbering with current `main` before writing one.**

**The admission/outbox transaction is in B's store, not this one.** A Wave 6 story
that puts an outbox table in `src/superplane-api/alembic/` has implemented shared
jobs in the domain API. The domain's obligation at (3) is to expose the two budget
hooks and be callable; it is not to hold the transaction.

### 3.5 Failure compensation, without a coordinator

Each step's failure has exactly one safe answer. The rule throughout: **fence
before releasing, and reconcile provider truth before either.**

| Failure | Safe compensation | Basis |
|---|---|---|
| (1) reserve succeeds, (2) confirm lost | Reservation stays held. Retry `confirm` under the same key — idempotent, so a landed confirm returns its own answer. A reservation is not a spend; holding one too long costs headroom, releasing one too early costs correctness | #4912; §3.2 |
| (2) confirm succeeds, (3) commit lost | No admission, no outbox row, therefore no dispatch. The confirm is compensatable **only because nothing was dispatched** — establish that first, then release | #4912 |
| Cancellation or expiry **before** dispatch | **Fence creation, then release the reservation.** Never the reverse: releasing first leaves a window where a stale worker can still create the resource the budget no longer covers | #4912; #5526 design 3 |
| (4) delivery uncertain / duplicate | Duplicate-safe by §3.2. The duplicate is refused at the constraint; the outbox may redeliver freely | `013:65,73-79` |
| (5) provider response **lost** | `CallOutcome.AMBIGUOUS` ([`handles.py:252`](superplane_contracts/handles.py)) — "an unknown, not a negative". The pre-call record is durable, so the resource stays findable by name and idempotency key | [`adapter.py:268-291`](superplane_contracts/adapter.py) |
| Reconciliation after any of the above | `reconcile()` reaches a conclusion **only from a `ProviderObservation`** ([`reconciliation.py:235-316`](superplane_contracts/reconciliation.py)). `RETRY_PERMITTED` is the **only** value authorizing a repeat, and it requires provider-established *absence* (`:158-161`). `UNKNOWN` → `UNRESOLVED`: no retry, no release, allocation stays on the books (`:163-165`) |
| Uncertain dispatch, budget still reserved | **Reservation is retained until provider reconciliation.** `CostExposure.NONE` is unreachable without established absence for every expected resource ([`accounting.py:199-276`](superplane_contracts/accounting.py)); `may_return_reservation_unused` returns `exposure is CostExposure.NONE` (`:186-196`) |
| Cleanup incomplete | `TeardownReport.exit_code` is non-zero and capped at 125 ([`provider_truth.py:224-239`](superplane_contracts/provider_truth.py)). A released allocation cannot carry outstanding findings (`:204-207`); cleanup cannot be reported successful after a credential failure (`:195-203`) |

Two things this makes structurally impossible, and both are worth naming because
each is a defect that already shipped upstream:

- **A timeout cannot be read as a failure.** Upstream `Onboarder.Onboard`
  received the launch identifier only on the success path, so a timeout produced
  `Success: false` with no provider reference, and the caller launched on the next
  cloud — while the first machine, if it came up, billed with nothing referencing
  it. `AMBIGUOUS` plus a durable pre-call record is the fix.
- **A failed delete cannot advance a status to `Terminated`.** Upstream logged
  "failed to delete K8s node, continuing anyway" and advanced the phase anyway.
  `ReleaseState.UNRESOLVED` and a non-zero `exit_code` are the fix.

### 3.6 What does not exist, stated plainly

No `reserve()` and no `confirm()` function exists anywhere in this module —
searched for both. The only structural traces are the *absences* and one name for
the question (`may_return_reservation_unused`,
[`accounting.py:186`](superplane_contracts/accounting.py)).

The absence is **deliberate and asserted**, not an oversight:

- [`observation.py:91-96`](superplane_contracts/observation.py): *"Deliberately has
  no `budget_exceeded`, `enforce` or `limit` field. This contract reports what was
  observed; it confers no local budget authority … Admission-time enforcement is
  B's, and the story is explicit that this unit adds no local budget authority
  even behind a flag."* Pinned by
  `test_budget_payload_has_no_enforcement_field`.
- [`accounting.py:31-34`](superplane_contracts/accounting.py): *"C owns the
  reservation ledger. A does not write it, and this module contains no balance, no
  reservation arithmetic and no spend total, because a second place computing cost
  is a second answer that can disagree with the real one."*

**One asymmetry a Wave 6 implementer will hit and must not resolve by accident.**
The *transferred API* already enforces quota synchronously and mutates workspace
status on budget breach — `enforce_workspace_creation_quota`
([`services/quota.py:234`](../src/superplane-api/app/services/quota.py), called at
[`routers/workspaces.py:125`](../src/superplane-api/app/routers/workspaces.py)) and
`CostReconciler._suspend_workspace`
([`services/cost_reconciler.py:397`](../src/superplane-api/app/services/cost_reconciler.py),
setting `status = "budget_exceeded"` at `:405`). That is a **different lineage**
from the contracts package's refusal to hold budget authority: it is upstream
transferred behaviour, not this EPIC's admission gate. The domain budget hooks
#5526 needs are *not* these functions, and wiring the hooks to them would put
admission authority in the domain app. Note also that
`enforce_node_provisioning_quota` and `enforce_deployment_quota` are declared with
**no call site anywhere** — do not read their existence as enforcement.

The gateway has a real, idempotent `reserve()`
([`modules/gateway/src/budget/reservations.py:341`](../../../gateway/src/budget/reservations.py),
keyed on `request_id`), and **no superplane code references it.** Citing it means
proposing a new binding, which is #5526's call to make explicitly and not a
detail to discover during implementation.

---

## 4. Startup composition

### 4.1 The defect this replaces

`app.installation.capabilities()` answered four `is not None` tests. That asks
whether a name is bound, not whether anything is behind it. An object that exists,
implements none of its port's calls, or approves everything it is asked passed —
and three consumers treat passing as evidence the image is composed for
production. A check that cannot distinguish a real adapter from a placeholder is
worse than no check, because it manufactures confidence.

#5535 (w6-12) states the requirement directly: *"Avoid singleton-present checks
that report health without a reachable compatible authority."*

### 4.2 The real check: exercise the adapter

[`src/superplane-api/app/capability_probes.py`](../src/superplane-api/app/capability_probes.py)
establishes each capability by **calling** the configured adapter and requiring it
to refuse.

Every probe presents sentinel values from
[`superplane_contracts/conformance.py`](superplane_contracts/conformance.py) —
`__conformance_probe_workspace__`, `__conformance_probe_operation__`,
`__conformance_probe_forged_authority__` and siblings. A workspace no grant
covers, an operation nobody issued, an authority never minted. **The input is
unauthorized by construction rather than by the adapter's good behaviour**, which
is what makes the probe safe to run on every boot: no correct implementation has a
code path that acts on them.

Three safety properties, all pinned by tests:

- **All four probes are reads.** The facade is probed with `report_progress` and
  **never** `open_operation`, because opening an operation on every boot is exactly
  the mutation this check must not perform.
- **A placeholder cannot fake a refusal for the right reason.** It has no method,
  or it raises `NotImplementedError`, or it returns something. Each is detected as
  a distinct verdict.
- **Each probe is its own call, varying one field of an otherwise-constant
  baseline.** See §4.3.

Verdicts: `REFUSED` (correct), `ADMITTED` (authorized unauthorized input),
`WRONG_REFUSAL_SHAPE` (refused, but not as the port declares),
`NOT_IMPLEMENTED` (no such call — the placeholder), `TIMED_OUT` (no answer within
the caller's bound), `FAILED` (raised something the port does not declare as a
refusal), plus two that belong to the valid control alone: `ADMITTED_CONTROL`
(correct — the seeded valid request was accepted) and `REFUSED_VALID_CONTROL`
(incorrect — legitimate seeded work was refused).

### 4.3 Two tiers, because they can establish different things

A refusal is evidence about the dimension you varied **only if the rest of the
request would have been accepted.** Otherwise the call had another reason to be
refused and the attribution is invented. So isolating a rule requires a **valid
control** — a request the adapter admits — and whether one exists depends on where
the probes run. Hence two tiers, with different and explicitly stated boundaries.

| | Startup smoke check | Seeded offline isolation suite |
|---|---|---|
| Entry point | `smoke_probes_for` | `isolation_probes_for` |
| Baseline | sentinels only; unauthorized in every field | seeded, authority-owned fixture the adapter accepts |
| Probes per port | exactly one (`UNKNOWN_MUST_NOT_SUCCEED`) | `VALID_CONTROL` first, then one per dimension |
| Runs where | every boot, installer preflight (`--network=none`), post-rollout recheck | the contract test suite only |
| Establishes | an implementation is installed, implements the call, and refuses a wholly-unauthorized request in its declared shape | that the adapter accepts a valid request and refuses each single-field variation of it |
| `conformant` | always **false** | true when the control is admitted and every variation refused |
| `isolated` | always **false** | true when the control was run and admitted |
| Limitation string | `SMOKE_LIMITATION` | `CONFORMANCE_LIMITATION` |

The smoke tier reports every rule dimension in **`not_exercised`**. It is a
read-only liveness check, not conformance evidence, and it says so in the report it
returns.

#### Why the split exists: two defective revisions

1. The first invoked each adapter **once**, with every field replaced by a sentinel
   simultaneously, and applied that single outcome to every probe descriptor.
2. The second issued one call per probe — but varied one field of a baseline that
   was *already* unauthorized in every other field. Every call therefore had a
   legitimate reason to be refused for the baseline alone.

Both reported the same adapter as fully conformant: one that checks the workspace
and ignores the operation identity, the authority and the permission entirely.
Under (2) it refuses all four calls — the baseline workspace is one it was never
granted — and the report credited it with refusing an unminted authority and a
forged operation identity. **The refusals were real; the attribution was invented.**
A direct call with the accepted workspace and a forged authority was admitted.

`TestEachDimensionIsProbedInIsolation` now builds readers that omit the workspace,
operation, authority and permission checks **separately**, drives each through the
real runner against the seeded fixture, and requires the bypass to be attributed to
the missing rule alone.

#### The control also closes the opposite hole

A purely negative suite passes an adapter that refuses **everything**, including
all legitimate work — broken in a way that takes production down. `VALID_CONTROL`
makes that a `REFUSED_VALID_CONTROL` failure, and `ConformanceReport.conformant`
requires `isolated`, so a run whose control failed cannot report verified
dimensions on the strength of refusals that attribute to nothing.

The fixture identities are **seeded, not provisioned**: no real account, no
credential, no live authority. `TestTheFixtureItselfIsAValidControlAndNotSentinel\
Garbage` pins that, because the fixture adapter validates against the fixture by
reference — making it valid by construction — so without those guards F2 could be
reintroduced through the test data alone. That was verified, not assumed: reverting
the fixture to the old all-sentinel baseline once left the entire suite green.

#### A dimension the call cannot present is reported unexercised, never as passing

A port's contract can oblige a refusal the readiness call has no way to present.
`report_progress` carries an operation id and nothing else, so no permission or
authority can be varied through it. Those kinds appear in the report's
**`not_exercised`**, never among its refusals.

`PortContract.carries_contract_version` is the sharpest case: **no port's call
exchanges a contract version anywhere in this codebase** (`grep -n
contract_version` across `adapter.py`, `delivery.py`, `provisioning_adapter.py`
and `auth.py` returns nothing). So the stale-version dimension is reported
unexercised for all eleven ports. The earlier revision emitted a stale-version
probe for every port and counted each as a verified refusal — an adapter
"refusing" an argument it was never passed. `isolation_probes_for` now *raises* if
a caller claims to exercise `contract_version` on a port that carries none, so the
false pass cannot be restored by adding one string.

`UNKNOWN_MUST_NOT_SUCCEED` is delegated the same way, in the other direction: it is
the smoke tier's probe, so the isolation tier reports it in `not_exercised`. Both
varies-nothing kinds send the baseline unchanged, and the two tiers' baselines
demand opposite answers — so emitting this kind into the isolation tier would
require a refusal of the very request `VALID_CONTROL` requires be admitted. An
intermediate revision of this repair did exactly that, making the tier unpassable by
a correct adapter; `test_an_adapter_that_enforces_everything_is_conformant` caught
it. An unpassable check gets weakened rather than obeyed, which is how a gate stops
gating.

`not_exercised` does not by itself fail a capability: the dimension is absent from
the call, not broken in the adapter. It is reported so nobody reads a green
capability as "every rule in the contract was verified here".

### 4.4 The gate's threshold: `composed`, which is not `conformant`

A capability is true only when **every probe the gate ran produced a
correctly-shaped refusal**. `capability_probes.PASSING` names the single passing
verdict rather than listing the disqualifying ones, so a verdict added to
`ProbeVerdict` later fails closed instead of silently joining the passing set.

`composed` is deliberately **not** the same claim as `conformant`. The gate runs one
smoke probe per port and has no valid control, so it reports `conformant: false` and
`isolated: false` on every report it produces. An earlier revision made the two
identical by fabricating the second — the production entry point returned
`composed: true, conformant: true` with four "refused" probes and claimed the
forged-operation and unknown-authority cases had been exercised, when one
unauthorized baseline had been refused for the workspace alone.

Keeping both booleans in the readout is what preserves the distinction: a reader
must be able to tell *"a real adapter refused an unauthorized request"* from *"each
authorization rule was verified."* The installer's four-boolean contract reads
`composed` only, so the honest narrowing did not change what it consumes.

An earlier revision made this gate *narrower* than the contract suite: only
`ADMITTED` and `NOT_IMPLEMENTED` failed it, so a timeout, an `OSError`, a
`ValueError` — any unexpected exception — reported the port as **composed**. The
justification was the offline preflight: `--network=none`, so a correct adapter
whose vault is unreachable cannot establish ownership.

That justification does not hold. **Every port's contract already says what to
answer in that case** — its declared `unknown_outcome`. An adapter that lets the
connection error escape instead has not answered at all, and a call that produced
no contract-valid answer is not evidence that the adapter refuses anything.
Treating it as evidence is how an entirely broken adapter passed a gate whose only
purpose is to catch exactly that.

The offline case is genuinely covered, and pinned by a paired test: an adapter that
answers its contract while offline (returns `None`, or raises its port's declared
refusal) passes every probe, so the gate is not made unpassable for a correct
image. `test_an_offline_adapter_answering_its_contract_is_composed` fails if that
stops being true.

On a `RAISE_UNAVAILABLE` port, "raised" is not enough: the exception must be one
the registry declares in **`PortContract.refusal_exceptions`**, matched through the
MRO so a more specific subclass still counts. Accepting any exception would make
those ports unfailable — every bug would read as correct behaviour. The registry
holds exception *names* rather than types because the real types live in
`app.services.*` and this package must not import `app.*` (§1).

Cancellation and process-control exceptions (`CancelledError`, `KeyboardInterrupt`,
`SystemExit`) propagate untouched: the runner catches `Exception`, never
`BaseException`. They mean the caller is being torn down, not that an adapter
answered, and the earlier revision's `BaseException` catch turned a Ctrl-C into a
probe verdict.

### 4.5 Composition points

One composition, four readers. #5535 (w6-12) owns installing the adapters; this
story owns the check every reader performs.

| Reader | Where | Calls | On refusal |
|---|---|---|---|
| FastAPI boot gate | [`app/main.py:65-73`](../src/superplane-api/app/main.py), under `SUPERPLANE_INSTALLATION_REQUIRED=true` | `await capabilities_async()` | `RuntimeError` — the process does not serve |
| Image-local preflight | [`installation/runner.py:272-299`](../installation/runner.py) — `docker run --network=none … python -m app.installation capabilities` | CLI, exit 0/2 | `require(...)` fails: "Production image lacks trusted capabilities: …" |
| Post-rollout recheck | [`installation/runner.py:1168-1190`](../installation/runner.py) via `GET /internal/installation` | `await capabilities_async()` | `require(...)` fails, asserting all four true **and** `len(capabilities) == 4` |
| Controller preflight | [`src/superplane-controller/main.go:81-84`](../src/superplane-controller/main.go) — `--installation-preflight` | prints `governed_provisioning`, `os.Exit(2)` | installer requires `governed_provisioning is True` ([`runner.py:311-315`](../installation/runner.py)) |

**`capabilities_async()` and `capabilities()` are separate deliberately.** Two of
the four readers call from inside a running event loop, where `asyncio.run`
raises. The sync wrapper refuses with a pointed message rather than failing
confusingly deep in the call.

**Exactly four booleans, unchanged.** The recheck asserts `len(capabilities) == 4`,
so the readout's shape is a contract. Probe detail is additive and appears **only**
in the image-local CLI output (`{"capabilities": …, "probes": …}`), not in
`GET /internal/installation`: that response is authenticated but tenant-facing,
and an adapter's refusal message is the one place a provider error or another
tenant's identifier could have been interpolated.

### 4.6 The controller's executor binding

The controller's half of composition is #5536 (w6-13). Its current state:

- `--installation-preflight` prints `governed_provisioning: false` and exits 2,
  hardcoded at [`main.go:81-84`](../src/superplane-controller/main.go). The
  installer requires `true`, so **the controller image cannot pass preflight
  today** — correctly, because B's adapter does not exist.
- With `SUPERPLANE_INSTALLATION_REQUIRED=true` the controller refuses to start at
  all: *"governed controller provisioning adapter is unavailable (B/#4912)"*,
  `os.Exit(1)`, before registering any provider-mutating loop
  ([`main.go:88-96`](../src/superplane-controller/main.go)).

Two obligations on #5536, both already stated in its issue and restated here as
contract:

1. **Authentication to SkyPilot is not spending authority.** The comment at
   `main.go:90-92` says so, and the flag must not become `true` on the strength of
   a successful SkyPilot auth. `governed_provisioning: true` means *bound to an
   active B operation/attempt/allocation authority*.
2. **Never re-enable direct provisioning as a fallback.** Also at `main.go:90-92`.
   When the facade, authorization, credentials or budget controls are unavailable,
   the answer is refusal — #5400 §2 states this for the provider path too.

When #5536 makes the flag real it must be computed the way §4.2 computes the API's
— by exercising the configured adapter, not by observing that one was constructed.
A hardcoded `true` would be strictly worse than today's hardcoded `false`.

### 4.7 Deployment order and compatibility checks

Owned by #5538 (w6-15); the ordering constraints this contract imposes:

```
  1. Shared services first: B's harness store + outbox (#5525), approvals (#5526),
     executor (#5527); Gateway vault evidence + delivery (#5528).
     Nothing downstream can be composed against a service that is not serving.
  2. Authorized environment preparation (#5537) — database/schema/role/TLS/secret
     inputs. Plan-only and separately authorized; an existing RDS is not
     permission to adopt it.
  3. Domain migration → bootstrap → rollout (#5538, extending U23).
     Migration is applied here and ONLY here. Authoring ≠ applying.
  4. Public verification, then the public route.
```

Compatibility checks, in the order they fail cheapest-first:

| Check | Where | Refuses |
|---|---|---|
| Image source/digest provenance | [`runner.py:255-268`](../installation/runner.py) — OCI `image.revision` vs `lock["source_revision"]` | an image not built from the pinned source |
| Contract version | `check_port_version(name, declared)` | absent, blank, mismatched or unsupported — never best-effort interpretation |
| Schema head | `test_exactly_one_head`; installer's `migrate` action | a branched or ambiguous migration head |
| **Actual capability** | §4.2, offline, `--network=none` | an image whose adapters admit unauthorized input or implement nothing |
| Controller governance | `--installation-preflight` | `governed_provisioning != true` |

**No routine domain command may implicitly redeploy the core platform**, and source
merge executes no live migration, account or cluster operation. Both are #5538's
requirements; both are also this wave's standing authorization boundary (§6).

---

## 5. Reconciling #4912 and #5400 into this wave

The story requires both EPICs' requirements land in Wave 6 under EPIC A, with
shared code in shared modules and Account Factory infrastructure in domain-owned
paths.

| Requirement | From | Wave 6 owner | Path |
|---|---|---|---|
| Job/attempt identity, idempotency, admission, dispatch/outbox | #4912 | #5525 (w6-02) | `modules/harness/jobs/` — **shared** |
| Leases, fencing, cancellation, crash recovery | #4912 | #5527 (w6-04) | `modules/harness/jobs/` — **shared** |
| Approvals: current authority, expiry, one-time consumption | #4912 | #5526 (w6-03) | shared HITL/policy + `modules/harness/jobs/admission` |
| Credential authorization, invocation binding, trusted delivery, rotation | #4912 | #5528 (w6-05) | `modules/gateway/src/auth/` — **shared** |
| Report authority and allocation inventory | #4912 | #5529 (w6-06) | shared authority in `modules/harness/jobs/`; domain records via published APIs |
| Supported lifecycle modes; Account Factory adoption | #5400 §1 | #5530 (w6-07) | `modules/domain-apps/superplane/infra/account-factory/` — **domain-owned** |
| Governed AWS account creation and child bootstrap | #5400 §1.3 | #5531 (w6-08) | domain adapter; Organizations only via the authorized executor |
| Managed workspace VPC/EKS/IAM | #5400 §3 | #5532 (w6-09) | `modules/domain-apps/superplane/infra/workspaces/` — **domain-owned** |
| Bootstrap, BYOC validation, ADP registration | #5400 §4 | #5533 (w6-10) | domain |
| The real `ProvisioningProvider` + mode-aware retirement | #5400 §2, §5 | #5534 (w6-11) | domain |
| API adapter composition and readiness | #4912 consumer side | #5535 (w6-12) | domain `app/services/*`, `installation.py` |
| Controller executor binding | #4912 consumer side | #5536 (w6-13) | domain Go controller |
| Environment package; release/installation; harness; live evaluation | both | #5537, #5538, #5539, #5540 | domain, except narrow shared release docs |

**The boundary in one sentence:** anything that is a *job, approval, lease or
credential* is shared and lives in `modules/harness/` or `modules/gateway/`;
anything that is *Superplane's infrastructure, accounting or research policy* is
domain-owned and lives under `modules/domain-apps/superplane/`. This story's own
diff respects it: `contracts/` and `src/superplane-api/` only, with
`modules/harness/contracts/` read as an input and not written.

Three reconciliations worth naming because they are places the two EPICs could be
read as conflicting:

- **#5400 says workspace provisioning "fails closed unless a separately supplied
  real provider exists." #4912 says the facade is B's.** Both hold: the *provider*
  is domain-owned (#5534, `workspace_infra`), the *facade it runs under* is B's
  (`harness_jobs`). `provisioning_provider` and `operation_facade` are separate
  registry entries with different owners for exactly this reason.
- **#4912 says C owns the reservation ledger; #5400 says budget limits gate
  provisioning.** Both hold: the domain exposes idempotent *hooks* (§3.1) and
  holds no ledger. The hooks are called by B's admission, not by the domain's own
  routes.
- **#5400 mode 3 (new AWS account + managed cluster) versus "never a side
  effect."** Account creation is a **named mode**, never implied by a normal
  workspace request (#5531 design 3), and account closure is never implicit in
  custom-resource or workspace deletion (#5530 design 4).

---

## 6. What this contract does not authorize

Implementation and offline verification only. **No account vending, AWS or
Kubernetes apply, database mutation, feature activation, image promotion or
workload spend is authorized by this document, by the issue, or by merging the
code.** Existing ADP availability, tenant/workspace boundaries, credentials and
unrelated resources are preserved.

Specifically:

- **No port here is composed.** All eleven remain unimplemented; the four API-side
  ones are `None`, so the honest capability readout is four `False` and the boot
  gate refuses. That is the correct state until the Wave 6 adapters land.
- **Every conformance report carries its limitation**, including the passing ones:
  offline probes, no provider contacted. An operator reading a green report is
  exactly the operator about to describe the integration as working.
- **No migration is applied.** This story adds no revision at all.
- **Mocked or offline evidence never closes a live criterion.** Which verifier
  owes which live evidence is [`REQUIREMENTS-MATRIX.md`](REQUIREMENTS-MATRIX.md).

---

## 7. Tests that hold this document up

| Property | Test |
|---|---|
| Every `declared_at` citation resolves to a class declaration | `tests/test_integration_contract.py::TestRegistryMatchesTheCode` |
| Every declared `Protocol` has a registry entry (exact set, both drift directions) | `test_every_declared_protocol_has_a_registry_entry` |
| The registry's API subset equals the probe table | `test_api_capability_subset_matches_the_servers_readout` |
| The readout is not a presence check | `test_the_api_readout_is_not_a_presence_check` |
| Every port binds an identifier and declares an unknown answer | `TestStructuralRules` |
| No externally-owned port is marked locally composable | `TestStructuralRules` |
| Malformed entries are unconstructible | `TestRegistryRefusesMalformedEntries` |
| The registry confers no implementation | `TestRegistryConfersNothing` |
| Stale/unserved versions refuse | `TestLookupAndVersioning` |
| An adapter admitting a forged workspace/operation/authority fails | `tests/test_conformance_probes.py::TestAdaptersThatMustFail` |
| Reports cannot overstate what they establish | `TestReportsCannotOverstateWhatTheyEstablish` |
| Probes are safe to run against production | `TestProbesAreSafeToRunAgainstProduction` |
| A placeholder satisfying `is not None` is refused | `src/superplane-api/tests/test_capability_probes.py::TestTheDefectThisStoryFixes` |
| An offline connection error does not fail the gate | `TestCorrectAdaptersAreAccepted` |
| The facade is never asked to open an operation | `TestProbesAreSafeToRunOnEveryBoot` |
| All four existing consumers' contracts are unchanged | `TestExistingConsumerContractsAreUnchanged` |

```bash
# Module lane (no AWS, no network, no database)
python3 -m pytest modules/domain-apps/superplane/tests/test_integration_contract.py \
                 modules/domain-apps/superplane/tests/test_conformance_probes.py -q

# Transferred-API lane (needs the API package installed; see superplane-domain-ci.yml)
cd modules/domain-apps/superplane/src/superplane-api
python3 -m pytest tests/test_capability_probes.py -q
```
