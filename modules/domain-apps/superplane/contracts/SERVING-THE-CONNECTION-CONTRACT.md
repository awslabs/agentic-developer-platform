# Serving the provider-connection contract over HTTP — U7b

Issue #5053 (U7b), requirement **R7** server half, EPIC #4910.

[`CONNECTION-CONTRACT.md`](CONNECTION-CONTRACT.md) is normative: it states what a
credential reference is, that there are two independent authorization questions, that
validation reports four readings separately, and what rotation and disablement mean.
This file is the **server half** — the routes that serve those rules in the maintained
Superplane API, and the places where serving a rule required a decision the contract
does not make.

Where the two disagree, the contract and `superplane_contracts`' tests win, and the
disagreement is a bug here.

Implementation: `src/superplane-api/app/routers/provider_connections.py`,
`app/services/provider_connections.py`, `app/models/provider_connection.py`, and the
migration `alembic/versions/*_add_provider_connections.py`. Acceptance tests:
`tests/test_workspaces.py`, plus the validation-error cases in `tests/test_accounts.py`.

---

## 1. The route surface

All five routes live under `/workspaces/{workspace_id}/provider-connections`. The
workspace is in the **path**, never in the body, because a workspace supplied in a body
is a caller asserting where its credential may be used — the assertion the binding
check exists to refuse.

| Method | Path | Does |
|---|---|---|
| `POST` | `/workspaces/{ws}/provider-connections` | Register a connection from a credential **reference** and bind it to this workspace |
| `GET` | `/workspaces/{ws}/provider-connections/{id}` | Read connection state |
| `POST` | `.../{id}/validation` | Record a validation report — four readings, no aggregate |
| `POST` | `.../{id}/rotation` | Atomically move the connection onto an already-validated replacement |
| `DELETE` | `.../{id}` | Disable admission and renewal; **does not** revoke delivered credentials |

Every one is registered in `app/endpoint_inventory.py` under `Scope.WORKSPACE`, and a
test asserts the inventory and the router agree — an unlisted route is an unguarded
route, so the inventory is enforced rather than descriptive. The four mutating routes
require `Permission.RENEW_CREDENTIAL`; `GET` requires `Permission.READ`, because it
returns the four validation readings and no credential material, and requiring the
renewal permission to *read* state would push callers toward holding more authority
than they need.

### Why `Scope.WORKSPACE` and not `Scope.ORGANIZATION`

`app/domain_guard.py` publishes `request.state.grant` **only** for `WORKSPACE`-scoped
entries. An organization-scoped credential route would therefore deny every caller,
including a correctly authorized one. This is worth stating because "a credential
belongs to the org, so scope it to the org" is the plausible reading, and it produces a
route that is uniformly broken rather than obviously broken.

### Why `DELETE` rather than `POST .../disablement`

The other two state changes are `POST` to a sub-resource because they *record a new
thing* — a validation reading, a rotation event. Disablement records nothing; it flips
the connection's own state. `DELETE` on the connection is the honest verb for that,
with the caveat in §5 that it is not deletion of anything the provider holds.

---

## 2. A reference is accepted; a secret is refused, whole

The contract says a connection carries a credential **pointer**. Serving that raised a
question the contract does not answer: what to do with a request that carries a pointer
*and* a secret.

The answer is to **refuse the whole request** with `400`, not to strip the secret and
proceed. Stripping is worse than refusing in a way that is easy to get backwards:

- The secret was already transmitted, already in the request body, and plausibly
  already in an access log upstream. Proceeding tells the caller the transmission was
  fine.
- A caller whose request *succeeds* has no reason to stop sending it. Refusal is the
  only response that changes the client's behaviour.

So `POST` with any of a key, token, password, secret value or secret ARN alongside the
reference is rejected, and the refusal names the offending **key** without echoing the
**value**. Detection is `superplane_contracts.secrets.find_secret_material`, which
checks key names (including compact/hyphen variants), secret-shaped values, and ARN
shape — the same function the contract half uses, so the two halves cannot drift on
what "a secret" means.

FastAPI's own `422` validation errors were a second leak of the same kind: the default
body echoes the rejected `input`. `app/main.py` registers `_scrubbed_validation_error`
app-wide, which drops `input`, runs `msg` through `redact_spans`, puts `loc` parts
through `scrub`, and pops `ctx`. That handler is app-wide rather than router-local on
purpose: the leak is a property of the framework's default, not of these five routes.

---

## 3. Two checks, both required, neither substituting for the other

| Check | Function | Question |
|---|---|---|
| ownership/delegation | `authorize_delegation` (contract) | may this principal manage this credential? |
| workspace binding | `check_binding` (service) | may this credential be used in *this* workspace? |

Both must pass. Organization membership satisfies neither, and the routes never consult
it for these decisions.

### The `authorize_use` trap

The contract offers `authorize_use`, which bundles the binding comparison **with**
`admits_new_work()`. That pairing is correct for *admitting work* and wrong for
*managing a connection*, because the two differ precisely on the `PENDING` and
`DISABLED` states:

| State | `admits_new_work()` | `allows_renewal()` |
|---|---|---|
| `PENDING` | no | **yes** |
| `DISABLED` | no | no |

A `PENDING` connection must still be renewable — that is what pending means — so
gating a management route on `authorize_use` would refuse renewal on exactly the
connections that need it. The service therefore uses `check_binding`, the binding
comparison alone, and pairs it with the lifecycle predicate each route actually needs.

There is deliberately **no wrapper** for `authorize_use` in the service. U7b has no
route that admits work, and an unused helper wrapping an authorization primitive is the
thing a later caller reaches for without reading why. The story that adds a spend route
should call `authorize_use` against the reasoning it needs then, rather than inherit a
binding this story guessed at. (An earlier draft did add such a wrapper,
`check_admission`; it was removed as dead code.)

---

## 4. Four readings, and `None` is not `0`

`POST .../validation` records `credential_valid`, `permissions_sufficient`,
`quota_available` and `observed_capacity` **separately**. There is no aggregate boolean
in the response, because an aggregate is the thing a caller reads instead of the reading
it needs — "healthy" hides a credential that is valid but out of quota.

`observed_capacity` is `int | None`, and `None` ("not measured") is distinct from `0`
("measured as zero"). Those two justify opposite actions: not-measured means go and
measure, measured-zero means stop. The storage and the response both preserve the
distinction, and the round trip is tested — see §7, where mutation testing showed that
the obvious tests do not actually cover it.

---

## 5. Disablement is honest in the response body

`DELETE` blocks new admissions and renewals. It does **not** revoke credentials already
delivered to running work, because this service cannot — revocation is the provider's,
and nothing here holds the authority to perform it.

The contract requires that limit be stated. Serving it raised the question of *where*,
and the answer is the **response body**, not only the docs: an operator who disables a
connection during an incident reads the response, not this file. The body says
disablement does not revoke already-delivered credentials, so the caller cannot come
away believing the key is dead. The handler checks that the `limitation` field is
actually present and fails with `500` if it is not — so a later change to the response
allowlist cannot silently drop the one field acceptance 5 is about.

Disabling an already-disabled connection is **allowed** and returns the same body. This
is the one place a stricter rule would be worse: refusing the second call makes
containment depend on the caller knowing the current status, and an operator retrying
because they are unsure whether the first attempt landed would get an error that reads
as a failure to disable.

Rotation is the related case. It moves the connection onto an **already-validated**
replacement atomically, and reports the old credential as **superseded** — never
deleted. A rotation that deleted first would, on failure, leave a workspace with no
usable credential; keeping the old one reported-but-superseded means a failed rotation
is recoverable.

---

## 6. Nothing outward carries a value or an ARN

Responses are built by `connection_response()` / `validation_response()` from an
**allowlist** of named fields. Not a dict dump minus deletions: a dump-and-delete
republishes every field added later until someone remembers to remove it. A test adds a
leaking field to the model and asserts it does not appear in any response.

Logs use `SecretRedactingFilter` from the contract package. Note the distinction
between the two redaction strategies, because using the wrong one silently weakens it:

- `scrub()` withholds a **whole value** — right for anything caller-supplied.
- `redact_spans()` / `redact_secret_spans()` replace only secret-**shaped spans**
  inside known-safe prose — right for a framework message that must stay readable.

---

## 7. Trust integration and validation

These routes require strict ADP identity verification and a current workspace grant.
The legacy organization-only token supplies neither an individual owner nor credential
permission and cannot use this surface. Each mutation also consults the startup-only
`CredentialEvidenceReader` in `app/services/credential_evidence.py`. Its adapter must
retrieve ADP vault-owned metadata for the exact organization, workspace and credential
reference. Registration cannot appoint the caller as owner; rotation checks ownership
of both the current and replacement credentials. The domain registry is only an
existence check, never proof of ownership.

Validation and rotation require an independent attestation of the canonical SHA-256
report digest. The digest covers the normalized readings/detail; `checked_at` is
supplied by the trusted evidence source, not the caller or local receipt clock. The
adapter must verify freshness and binding through the vault/provider, not echo the
untrusted request digest. Evidence expiry is checked after resolution and again before commit, including after
flush waits. A successful commit is returned as success even if the evidence expires
immediately afterward. Missing integration returns503; mismatched or stale
evidence returns403. No production adapter is installed by default. U23 must report
this missing capability explicitly rather than treating healthy pods as usable
credential management.

Connection writes take a row lock before retrieving authority and refresh both the
connection and binding. Before and after flush, the service re-reads the exact
organization/workspace/principal grant from protected storage, checks its current
renewal permission, and holds the grant through commit. Registry rows are also locked
through commit, preventing deregistration from invalidating an in-flight binding.
The requested service, connection provider and active registry provider must agree.
Deregistration refuses enabled connections and cluster assignments; permitted removal
retains a `Deregistered` registry tombstone and an attributed audit event. Tombstones
are excluded from active listings and cannot authorize new connection operations. Rotation updates both in one transaction. Failed revalidation
moves ACTIVE back to PENDING, preserving all four readings while stopping admission.
Unknown readings never become measured capacity or truthy booleans. Requests are
bounded to64KiB and secret material in unknown or nested fields is refused.

The HTTP tests use signed ADP-shaped tokens, persisted explicit workspace grants and
a named fake vault adapter. They cover valid lifecycle transitions, foreign ownership,
replacement ownership, missing/misbound/expired evidence, missing report attestation,
legacy token refusal, validation types, failed revalidation, redaction and request
bounds. These are boundary tests, not evidence of a live vault/provider integration.
The CI coverage gate remains85% and includes the route, service and evidence port.

---

## 8. Build-context note for the release owner

`app/main.py` imports `superplane_contracts.emission` at module scope. That package is
maintained at `modules/domain-apps/superplane/contracts/`, outside the Docker build
context that `releases/build-image.sh` pins to `src/superplane-api`, and it appears in no
dependency list. It is staged into the context by `scripts/stage-domain-auth.sh` (
git-ignored scratch, refreshed per run) and the Dockerfile fails the build by name if it
is absent.

This is the second sibling package to need that treatment, after `superplane_auth`
(#5055). The per-package workaround does not scale; **widening the build context to the
module root is the durable fix and is release-owned (#5327).**

---

## 9. What this unit does not close

R7 acceptances **6 and 7** — the audited migration run — remain **open**. They need a
named account and environment, authorized vault and KMS access, and a named cleanup
owner; none is resolved, so no live run was performed. Nothing here was deployed: no
Terraform, no migration against a live database, no image build, no feature enabled.
U23 (#5327) owns release.

The migration `014_add_provider_connections` extends U11c's
`013_add_provider_operations` (PR #5460). The combined chain has one head and 20
revision files, preserving the earlier revision IDs.

The local PostgreSQL full-chain smoke exposed inherited descriptive revision IDs
longer than Alembic's default 32-character version column. The domain environment
uses Alembic's public version-table hook (minimum 1.14) to create a 128-character
column and widens an existing version column before upgrades. Revision IDs and
migration history remain unchanged. This is domain migration metadata only.
