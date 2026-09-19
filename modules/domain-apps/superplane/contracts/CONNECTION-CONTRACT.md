# Provider-connection and workspace-binding contract — `v1`

Issue #5047 (U7), requirement **R7**, EPIC #4910.

This is the normative description of the provider-connection contract: what a
credential **reference** is, the two independent authorization questions, what a
validation result reports, and what rotation and disablement mean. The Python
package in `superplane_contracts/connections.py` is the executable form; where the
two disagree, the package's tests are what gate CI, so treat a disagreement as a
bug in this file and fix it here.

The audience is two halves that must agree without sharing code: this contract plus
its vault client (U7, here) and the upstream routes and service logic (U7b) with the
schema that persists them (U13b). Neither is written yet, which is why the rules
below are stated exactly rather than left to whoever implements the server.

---

## 1. The two questions, and why they are two

Connecting a provider account to a workspace requires two answers that are
routinely conflated:

| Question | Answered by | Type it takes |
|---|---|---|
| **Who may manage this key?** | vault ownership, or an explicit delegation from the owner | `VaultOwnership` |
| **Where may this key be used?** | the workspace binding | `WorkspaceBinding` |

Same-organization possession answers **neither**. Holding a credential id because it
appeared in a list response is not ownership, and a credential legitimately owned by
its owner is not thereby usable in every workspace that owner can see.

The contract keeps them separate structurally: `authorize_delegation()` takes an
ownership record and cannot see a binding; `authorize_use()` takes a binding and
cannot see an ownership record. Neither function's argument can satisfy the other's
question, so a caller cannot pass one check and assume the other. `WorkspaceBinding`
carries exactly one `credential_id` and one `workspace_id` for the same reason: a
binding that carried a set would make "bound here" a property of a group.

### Refusals are indistinguishable

Every refusal from a given check uses **one shared string** for that check:

- delegation: `not authorized to delegate this credential`
- use: `credential is not bound to this workspace`

No branch is more informative than its siblings, so the refusal cannot be used to
enumerate which credentials exist, who owns them, or which workspaces they are bound
to. A caller learns only that it may not proceed.

### Unresolved is denied

When an ownership lookup yields nothing (`ownership=None`), that is a **denial**. An
absent record means the question was not answered, and an unanswered authorization
question is never permission. This is the fail-closed direction: a lookup that
errors, races or has not replicated yet refuses rather than admits.

---

## 2. What a credential reference is

`CredentialReference` carries the vault's own opaque credential id, a service and a
label. It is what the domain API accepts.

**A reference is never an ARN, and never a value.** Both are refused at
construction, so an illegal reference cannot exist to be persisted or logged:

| Rejected input | Why |
|---|---|
| A secret value | R7 acc. 1: values reach only ADP's vault endpoint. |
| An AWS ARN (any service) | An ARN names the account, region and secret, so the pointer plus any over-broad IAM policy completes the read. It also typically survives rotation, so a leaked ARN stays valid. |

Inbound payloads are checked by `assert_no_secret_material()`, which **refuses**
rather than scrubs. This is deliberate and is the whole of acceptance 1: a scrubbed
and accepted request returns success, so the submitter believes a value was stored
when it was not — and will either operate on a credential that does not exist or
retry through some path that does persist it. A refusal tells the submitter what they
did wrong, and it keeps the invariant checkable: the domain's store contains no
secret-shaped field because no request carrying one ever succeeded.

The refusal message names the **field path** and never the offending content, so the
error is not itself the leak.

> The outbound direction is a different module and neither replaces the other.
> `tools/superplane-mcp/superplane_mcp/redaction.py` scrubs on the way out so no tool
> result carries a secret; this contract refuses on the way in so nothing arrives.

---

## 3. Validation reports four things separately

`ValidationReport` has four independent fields, and R7 acc. 3 is that they stay
independent:

| Field | Type | Means |
|---|---|---|
| `credential_valid` | bool | The credential authenticated. |
| `permissions_sufficient` | bool | It carries the permissions the workload needs. |
| `quota_available` | bool | The account's quota permits the request. |
| `observed_capacity` | `int \| None` | GPU capacity actually observed. `None` means **not measured**. |

**A valid key must never read as available capacity.** These are four different
readings about four different things, and collapsing them is how an operator is told
"connection healthy" and then cannot launch anything. So:

- There is **no aggregate field**. No `ok`, `healthy`, `ready`, `available`, `valid`
  or `usable` attribute exists, and the tests assert their absence — an absence
  nobody watches gets filled in by the next person who wants one boolean.
- `validated` (a property, not a field) means valid **and** permitted **and** quota.
  It deliberately **excludes** capacity: a correctly configured connection with a
  full region is still correctly configured.
- `is_usable_for_admission()` is the separate question, and `observed_capacity=None`
  is **not** usable. Unmeasured is not available.
- `permissions_sufficient` cannot be true while `credential_valid` is false — a
  credential that did not authenticate cannot have had its permissions read.
- `checked_at` must be timezone-aware, and `detail` is checked for secret material.

---

## 4. Lifecycle

```
PENDING ──activate(validated report)──> ACTIVE ──disable()──> DISABLED
```

| State | `admits_new_work()` | `allows_renewal()` |
|---|---|---|
| `PENDING` | no | **yes** |
| `ACTIVE` | yes | yes |
| `DISABLED` | no | no |

`PENDING` must stay renewable: a connection whose validation has not completed is
not a connection whose credentials may not be rotated.

`ACTIVE` requires a `validated` report — a connection cannot be activated on a
reading that failed, or on no reading at all. A `ConnectionState` also refuses a
binding for a different credential than its reference names.

---

## 5. Rotation is atomic, and does not delete first

`rotate()` requires a **validated replacement** and switches to it in one step. It
returns a `RotationResult` whose `superseded_reference` is the old reference and
whose `old_credential_still_registered` is `True`.

The old key is **not deleted**. Revocation is a separate later call
(`VaultClient.revoke_credential()`), made once traffic is confirmed on the
replacement. There is deliberately **no combined rotate-and-revoke method** anywhere
in this unit: a single call that deleted and then registered would, on failure
halfway through, leave the connection pointing at nothing — an outage produced by the
routine maintenance operation meant to prevent one.

Rotation also does **not** require free capacity. Capacity is a reading about the
provider's region, not a property of the credential, and refusing to rotate a key
because a region is full would block the security operation on an unrelated
condition.

---

## 6. Disablement is honest about what it cannot do

`disable()` blocks new admissions and credential renewals, and sets a **non-empty
limitation** — a `DISABLED` state with no limitation cannot be constructed.

The limitation is surfaced in the response body, not only in a log, because
acceptance 5 is about what the operator who just disabled the connection sees:

> Disablement blocks new admissions and credential renewals. Credentials already
> delivered to running workloads may remain usable until they are revoked at the
> provider; disablement here does not revoke them.

An operator disabling a connection during an incident will otherwise believe access
has stopped. It has not: a long-lived credential already handed to a running pod
keeps working until the provider revokes it. Stating that is the difference between
a control an operator can reason about and one that misleads them at the worst
moment.

---

## 7. Nothing carries a value or an ARN outward

R7 acc. 4 covers response bodies **and log lines**, so both are handled:

- `connection_response()` and `validation_response()` build bodies from an
  **allowlist** of named fields. Not a dict dump minus deletions — a dump-and-delete
  republishes every field added later until someone remembers to remove it.
- `SecretRedactingFilter` is attached to the logger and its configured output handlers after logging setup, including handlers reached by propagation. Reinstall after adding or replacing handlers. The
  call sites are the problem: `logger.info("state=%s", payload)` gets written by
  whoever is debugging, and no review catches all of them.
- The filter scrubs `record.msg` **and** `record.args`. Scrubbing only
  `getMessage()` is the common half-fix: it renders the message once for inspection
  while the handler renders it again from the untouched `args`, so the scrub applies
  to a copy nobody emits.
- It always returns `True`. Redaction must not drop records — losing an incident-time
  log line because it contained a long hex string trades a disclosure for a blind
  spot.

---

## 8. What is mocked, and recorded as mocked

**B's exact-credential-binding enforcement does not exist.** Verified, not assumed:
`modules/gateway/src/shared/services/credential_resolver.py` exposes `resolve()` and
nothing else — no `resolve_by_id`, no `resolve_exact`.

Its two narrowing parameters are not exact binding, and the distinction matters:

| Mechanism | What it actually does |
|---|---|
| `resolve(service, ...)` | Matches by **service**, ranks candidates, returns the first. Ask for a service and you get *a* credential for it, not the one you named. |
| `scope_hint` | Bounds how **wide** a scope may be returned. Narrowing a scope is not identifying a credential — many credentials share a scope. |
| `strict=True` | Restricts to exact-**scope** matches. Again a property of the scope, not the identity. |

The vault also serves no `GET /auth/credentials/{id}` — only the list endpoint — so
even resolution by id is client-side filtering over a list.

Therefore `VaultClient.EXACT_BINDING_IS_MOCKED` is `True` and `resolve_exact()`
returns `(credential, provenance)` where provenance always carries
`{"exact_binding": "mock"}`. The tuple shape is deliberate: a caller cannot take the
credential without also receiving the marker describing how it was resolved.

Client-side filtering is an honest **read** and is **not enforcement** — a server
that returns a list cannot stop a different caller picking differently. Per
`acceptance-split.md` rule 5 this is recorded rather than presented as working, and
the tests assert B still has no exact-binding API, so the mock cannot outlive its
justification silently. `resolve_exact()` is the single seam to replace.

---

## 9. Scope boundary

**In:** the contract types, the two authorization checks, the validation report, the
rotation and disablement rules, the inbound refusal and outbound redaction, the thin
vault HTTP client, the tests.

**Out:** the domain API's routes and service logic (**U7b**, authored only); the
schema that persists connections and bindings (**U13b**, authored only); any change
to the gateway's vault (`modules/gateway/src/auth/`); provider-side revocation.

**Not closed by this unit:** R7 acceptances **6 and 7** (the audited migration run).
Those need a named account and environment, authorized vault and KMS access, and a
named cleanup owner — none of which is resolved here. A CI lane that took a
credential to prove a contract lints would have a far larger blast radius than the
thing it verifies.
