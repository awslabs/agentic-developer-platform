# Provisioning contract — provenance and what stays open

Issue [#5052](https://github.com/aws-e/adp/issues/5052) (U17a), EPIC
[#4910](https://github.com/aws-e/adp/issues/4910). R14, ADP half, acceptance 2
(logic half).

This note exists because of one requirement in the story, quoted:

> Operation-facade and provider fixtures must be derived from B's published
> contract shape or a captured real response — never from the adapter's own
> expected shape, or adapter and fixture agree on a contract B never publishes.

That requirement is hard to satisfy honestly here, and the honest answer is worth
writing down rather than working around: **B's operation facade does not exist,
and B has published no schema for one.** There is no captured real response to
derive from, because there is nothing running to capture from.

So the fixture is split. The parts traceable to something B actually published
are marked as such, with the file and its SHA-256. The parts B has not published
are listed as **unresolved**, with what the adapter does in the absence of an
answer. Nothing is presented as B's contract that is not.

## What B has published

Verified at ADP commit `8b94db04cad0b793a31f882382ecdb60a4691f3c`.

| Source | SHA-256 | What it establishes |
|---|---|---|
| [`modules/harness/contracts/README.md`](../../../harness/contracts/README.md) | `427cde6909e045e3e9fc183a7d618013699dd23f12be2aab150decb6456e1782` | The three-field substrate on every harness contract (`name`, `version`, `owner`), and the routing rule that long-running work outliving a single call is a `job` |
| [`contracts/hitl-ticket/v1/models.py`](../../../../contracts/hitl-ticket/v1/models.py) | `880b11981178de54ee5799432fbd0f6f7c3f4f22d35a78a38f768d0a56cb3596` | The executed-contract convention: validator + golden fixture + a CI job that runs the fixture. The one written contract of eleven |
| [`auth/superplane_auth/policy.py`](../../auth/superplane_auth/policy.py) | `07b4391b5fbd37c6d172e49689ea3127da96ab8fc2b74b2e12c0b42ab2c25cbe` | U9's authority model, including `Permission.PROVISION = "workspace:provision"` — the permission an operation binding must carry |

Two quotes carry most of the weight. From the harness contracts README:

> Three fields are present on *every* contract: `name`, `version`, `owner`.
> That's the minimum substrate the harness needs to register, dedupe, and route
> questions.

and, from its decision tree:

> Long-running work that outlives a single call? → `job.schema.json`

That second line is why U17a initiates through an operation facade at all rather
than calling a synchronous verb. Workspace provisioning outlives its request,
which is precisely the case where "who is calling this endpoint" and "who is this
operation running as" come apart.

The same README is explicit that the envelope around that substrate is not
executed:

> **The JSON-Schema envelope above is aspirational; the `name`/`version`/`owner`
> substrate is not.** No `*.schema.json` file and no JSON-Schema validator
> library exists anywhere in this repo, so the envelope has never been executed
> by anything.

So the fixture keeps the substrate and does not pretend the envelope is a
contract.

## What B has not published

`modules/harness/jobs/` does not exist. `job.schema.json` is a table row in a
README, not a file. The harness contracts README's own status line: **1 of 11
written**, and the written one is `hitl-ticket`, not a job or an operation
facade.

The fields below are ones the adapter needs semantics for and B has published
nothing about. Each is recorded in
[`tests/fixtures/operation-facade.golden.json`](../tests/fixtures/operation-facade.golden.json)
under `unresolved_in_bs_published_contract`, so the gap is visible in the
artifact a reviewer reads:

| Unresolved | What the adapter does instead |
|---|---|
| `operation_id` format | Opaque. Required non-empty, never parsed. A format assumption breaks on B's first real identifier |
| Expiry semantics | `expires_at` optional; enforced when present, and `unbounded_authority` reports its absence rather than treating unbounded as safe |
| Progress state vocabulary | `OperationState` is *this* contract's vocabulary under `superplane_contracts` v1, not a claim about B's wire values. `UNKNOWN` exists so an unmappable value need not be rounded to a failure |
| Cancellation surface | Not modelled. `operation_id` is carried as the handle a cancellation would use, and nothing more |
| Principal resolution mechanism | The adapter requires a `ResolvedPrincipal` and refuses caller-supplied identity. *How* the facade resolves it is unobservable from here — a reason the live criterion stays open, not something a mock settles |

The pattern in that right-hand column is deliberate: in every case the adapter's
choice is the one that is safe to be wrong about. It refuses, or it reports the
absence. None of them assumes a guarantee B has not offered.

## Why the fixture is not derived from the adapter

The failure mode the story is guarding against is subtle and worth naming. If the
fixture were written by reading `provisioning_adapter.py` and transcribing what it
expects, then the test suite would prove the adapter agrees with the adapter. It
would go green, stay green through a redesign, and establish nothing about
interoperability — the fixture and the code would be two expressions of the same
assumption.

What is done instead:

* The envelope (`substrate`) is transcribed from the README's substrate rule, which
  B published and marked as not aspirational.
* The routing decision (`job`, not `tool`) quotes the README's decision tree.
* Everything operation-specific is in `unresolved_in_bs_published_contract`, which
  is the opposite of a derived shape: it is a list of things this side does **not**
  claim to know.
* `rejected_variants` are derived from the story's own prohibitions and from
  `repo-path-allocation.md`'s substitution table — not from the adapter. Each entry
  is a shape that would compile and read plausibly in review while silently
  weakening a named guarantee.

The `rejected_variants` form follows `contracts/hitl-ticket/v1/hitl-ticket.golden.json`,
which states the reasoning for it: those variants "are not hypotheticals. Each one
is a shape that would compile, read plausibly in review, and silently weaken the
fail-closed guarantee."

## What must not be substituted

From
[`repo-path-allocation.md`](https://github.com/aws-e/adp/blob/agent/issue-4910/aidlc/spaces/issue-4910/inception/delivery-planning/repo-path-allocation.md),
binding for this unit:

| A needs | Correct source | Must **not** be substituted |
|---|---|---|
| Authority to perform a provider operation, bound to a workspace operation | B's trusted-operation contract / operation facade (U17a, U10) | A direct read of the stored secret value, or an executor-minted long-lived credential |
| A credential scoped to one active run, time-bound and revocable | B's scoped trusted-delivery contract | The vault's credential-management endpoints (`POST`/`DELETE /auth/credentials`) — no run binding, no expiry, no run-tied revocation |
| Identity of the principal the operation runs as | Server-resolved, from the run binding | Any body-supplied user/org ID (design §6 lines 398–407) |

The three forbidden substitutions share a shape: each is a broader authority that
is *available*, where the correct one is *not built*. That is exactly when
substitution is tempting, and each one loses the property that made the real thing
safe — a run binding, an expiry, or a revocation path that reaches the credential.

Vault reuse is permitted as a **boundary** reference only. There is no
gateway-internal import in either module, and
`TestNoCredentialSurfaceIsTouched` asserts the absence over the modules' source
text rather than behaviourally, because an absence claim needs to cover paths no
test exercises.

## Status, stated the way the acceptance split requires

> **Adapter implemented and contract-tested; live execution not verified.**

That phrasing is
[`acceptance-split.md`](https://github.com/aws-e/adp/blob/agent/issue-4910/aidlc/spaces/issue-4910/inception/delivery-planning/acceptance-split.md)
rule 3's, and rules 2 and 5 are why it cannot be strengthened:

* Rule 2 — *mock success closes no live criterion*.
* Rule 5 — *an unimplemented dependency is not satisfied by mocking it. The mock
  unblocks construction and is recorded as a mock in the story.*

The mock is recorded as a mock in three places that a reader cannot miss: the class
name `MockOperationFacade`, an `is_mock` field asserted by `TestTheFacadeIsAMock`,
and `test_no_real_operation_facade_exists_to_integrate_against`, which fails if
`modules/harness/jobs/` ever appears. That last one is the trigger to revisit this
note — a mock whose premise silently expires is how a unit stays mocked long after
it needed to be.

### Not closed by this unit

**R14 acceptance 2 (live)** — provisioning through the real authorized operation.
Gated on all four of: a named account/environment, B's facade actually built,
spend authorization, and a named cleanup owner. None is resolved; the story's own
Deployment section records **Environment coverage: Unresolved**, with no AWS
account ID and no `adp-cred` label.

**R14 acceptance 1** — the absence of the `workflow_dispatch` call and its
foreign-repo PAT. That is U17b's, upstream, and a static offline check. The two
dispatch call sites are untouched here; `src/` does not exist in this repo, so
they are not present to touch.


## Provider execution context

The provider's `provision` and `teardown` methods receive the validated
`OperationBinding` as a separate keyword argument. Its server-resolved principal,
workspace and operation identity must govern the target; caller parameters contain
only provisioning options. Missing principals, unknown contract versions and
mutable or duplicate parameter pairs are refused before execution. This is the
ADP adapter port, not a claim that B has published or implemented its wire protocol.
