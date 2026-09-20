# Requirements-to-story matrix — producer and live verifier for every missing capability

Issue [#5524](https://github.com/aws-e/adp/issues/5524) (w6-01) AC-02, EPIC
[#4910](https://github.com/aws-e/adp/issues/4910), Wave 6. Reconciles
[#4912](https://github.com/aws-e/adp/issues/4912) (EPIC B) and
[#5400](https://github.com/aws-e/adp/issues/5400) (EPIC A1) into this wave.

**Source baseline.** ADP `f30bb66f299276a6cfe3b4a05150e63bbbda7e42`
(2026-09-19 20:08:48 +0100) — the baseline all seventeen Wave 6 stories record as
inspected. This branch is based on `b49d6cf334f72bc397b0f378aa6584512dbcfe9b`
("Merge pull request #5522", 2026-09-19 20:52:56 +0100), verified to be a
descendant of that baseline. Every "current state" claim below was read at that
head.

Companion to [`INTEGRATION-CONTRACT.md`](INTEGRATION-CONTRACT.md), which specifies
*what* each capability must do. This document says *who builds it* and *who
establishes it works against something real*.

---

## AC-02's two rules, and why they are the whole point

> "A requirements-to-story matrix identifies producer and live verifier for each
> missing capability, **no criterion ending at a mocked adapter**."

**Rule 1 — producer ≠ live verifier.** The party that wrote the adapter is not the
party that establishes it works. Every row's verifier column names someone other
than its producer. The registry carries the machine-checkable half of this:
`test_every_unimplemented_port_names_a_live_verifier` fails any port whose
`live_verifier` is blank, with `submitter_resolver` the single exemption and its
reason stated in the test (already implemented, covered by the existing
observation-auth suites, so no Wave 6 verifier owes anything for it). That the
named verifier is a *different party* from the producer is asserted here, per row,
and audited in §4 — a test cannot check it, because "different party" is not a
property of the string.

**Rule 2 — no row may end at a mock.** A row whose strongest evidence is "the test
double refused" has established that the test double refuses. Each row therefore
carries a **live verifier** and the **#5540 acceptance criterion** that owns its
real-world evidence. Where a capability is *only* offline-verifiable, the row says
so explicitly rather than borrowing credibility from a live criterion.

**The single live verifier for this wave is
[#5540](https://github.com/aws-e/adp/issues/5540)** (`wave-6/eval`), executed by
the operations executor under `wave-6/live-authorized`. There is exactly one, by
design: sixteen stories each self-certifying is sixteen chances to accept a mock.
#5540 AC-01…AC-08 are reproduced in §5 so a reader can check a row's claim without
leaving this file.

---

## 1. Shared durable execution — #4912 (owner `harness_jobs`)

Shared code, shared paths. None of these may be implemented in the domain API.

| # | Missing capability | Current state at baseline | Producer | Path | Live verifier |
|---|---|---|---|---|---|
| B1 | Durable operation identity: server-resolved tenant/workspace, immutable request binding, job/operation/attempt IDs, status/version | `OperationFacade` is a `Protocol` only; `operation_facade` global is `None`; **no `modules/harness/jobs/` directory exists** | #5525 (w6-02) | `modules/harness/jobs/` | #5540 **AC-04** (durable operation IDs externally observed), **AC-07** (identity retained across restart) |
| B2 | Admission + outbox in **one** owned transaction; retries return the same operation; changed-payload reuse rejected | No outbox table, no `outbox` reference anywhere in the module; no `reserve()`/`confirm()` function. Nearest precedent is `provider_operations` PK `(workspace, idempotency_key)` in **unapplied** revision `013` | #5525 (w6-02) | `modules/harness/jobs/` + its own migrations | #5540 **AC-02** (repeated requests create no duplicate resources), **AC-07** (no duplicate provision) |
| B3 | Resumable, duplicate-safe outbox delivery with schema upgrade/rollback behaviour | Absent. `AuditMiddleware` is post-hoc, separate-session, exception-swallowing — explicitly **not** a usable substrate (`INTEGRATION-CONTRACT.md` §3.3) | #5525 (w6-02) | `modules/harness/jobs/` | #5540 **AC-07** (authorized failure injection and restarts) |
| B4 | Current approval: freshness, expiry, one-time consumption, recheck at **both** decision and admission | No approval port, no HITL binding in this module | #5526 (w6-03) | Shared HITL/policy + `modules/harness/jobs/admission` | #5540 **AC-06** (revoked/misbound authority denied) |
| B5 | Budget-bound admission against the exact aggregate envelope, via **idempotent domain hooks** keyed `(job_id, attempt_id)` | **The hooks do not exist.** Contracts package deliberately holds no budget authority (`observation.py:91-96`, `accounting.py:31-34`). Gateway has a real `reserve()` at `gateway/src/budget/reservations.py:341` that **no superplane code references** | #5526 (w6-03) — hook interface; domain side is the callee | Shared admission; documented domain ledger callback | #5540 **AC-06** (admitted operations respect the exact envelope; incurred costs reconciled) |
| B6 | Leases, fence tokens, cancellation, crash recovery | Shape only (`leases.py`); `reconciliation.py:55-72` states no lease/fencing/attempt implementation exists | #5527 (w6-04) | `modules/harness/jobs/` executor/leases/recovery | #5540 **AC-07** (fences retained; no early budget release after lost provider response) |
| B7 | Isolated executor protocol — workers see no raw database or provider secrets | `ProviderExecutor` holds a `TrustedDeliveryChannel` that is mocked today (`delivery_executor.py:240-280`); no production construction site | #5527 (w6-04) | `modules/harness/jobs/` | #5540 **AC-04** (bounded workload executes), **AC-01** (no credential disclosure) |
| B8 | Vault evidence: independently verified binding between a validation report and this credential + workspace | `CredentialEvidenceReader` `Protocol` only; global `None`; every provider-connection route already returns **503** | #5528 (w6-05) | `modules/gateway/src/auth/` | #5540 **AC-01** (spoofed identity denied), **AC-06** (revoked authority denied) |
| B9 | Operation-bound credential delivery and rotation — one lease, one bound action | `TrustedDeliveryChannel` `Protocol` only; no production construction site | #5528 (w6-05) | `modules/gateway/src/auth/` + shared trusted executor client | #5540 **AC-06** (revocation), **AC-01** (no credential disclosure) |
| B10 | Trusted report authority: which attempt/submitter may report for an operation | `ProviderAuthorityVerifier` `Protocol` only; `provider_handles.py:252` returns 503 "B operation authority is unavailable" | #5529 (w6-06) | Shared authority in `modules/harness/jobs/` | #5540 **AC-06** (misbound authority denied) |
| B11 | Trusted allocation inventory and fenced cleanup authority | `AllocationInventoryReader` `Protocol` only; `provider_handles.py:780` returns `unresolved(...)` | #5529 (w6-06) | Shared authority; domain records via published APIs | #5540 **AC-08** (provider-verified complete inventory; unknown is not empty) |

**Why B5 is the row most likely to go wrong.** Two functions in the transferred API
already enforce quota synchronously and mutate workspace status —
`enforce_workspace_creation_quota` (`services/quota.py:234`, wired at
`routers/workspaces.py:125`) and `CostReconciler._suspend_workspace`
(`services/cost_reconciler.py:397`, setting `status = "budget_exceeded"` at
`:405`). They are upstream transferred behaviour, a **different lineage** from this
EPIC's admission gate. Wiring #5526's hooks to them would move admission authority
into the domain app, which is what #4912 and this story's scope line both forbid.
Note also that `enforce_node_provisioning_quota` and `enforce_deployment_quota` are
declared with **no call site anywhere** — their existence is not enforcement.

---

## 2. Managed workspace and Account Factory — #5400 (domain-owned)

Domain-owned paths under `modules/domain-apps/superplane/`, per the story's
"Account Factory infrastructure in domain-owned paths".

| # | Missing capability | Current state at baseline | Producer | Path | Live verifier |
|---|---|---|---|---|---|
| A1 | Pinned Account Factory with **explicit** ownership modes (adopt / create / BYOC) | Reference tree only, at `modules/domain-apps/ai-super-plane/reference/infra/account-factory/`. Never built or deployed | #5530 (w6-07) | `modules/domain-apps/superplane/infra/account-factory/` | #5540 **AC-02**, **AC-03** (no accidental account creation) |
| A2 | Governed AWS account creation + child-account bootstrap, Organizations only via the authorized executor | Absent | #5531 (w6-08) | Domain Account Factory adapter + bootstrap manifests | #5540 **AC-02** (real Organizations status and account identity; existing-cluster access does not substitute) |
| A3 | Managed workspace VPC / EKS / IAM with per-workspace state and plan-safety | **`infra/workspaces/` is empty** | #5532 (w6-09) | `modules/domain-apps/superplane/infra/workspaces/` | #5540 **AC-02** (ACTIVE EKS, registered non-empty target) |
| A4 | Workspace bootstrap, BYOC validation, ADP registration | Partial registration surfaces (U16a/U16b); no bootstrap | #5533 (w6-10) | Domain bootstrap/registration + API target model | #5540 **AC-03** (actual target readiness; supplied resources not deleted) |
| A5 | The **real** `ProvisioningProvider` + mode-aware retirement | `ProvisioningProvider` `Protocol` only. `ProvisioningAdapter` is frozen and refuses a bound-action mismatch (`provisioning_adapter.py:173-180`), but nothing constructs it in production | #5534 (w6-11) | Domain provisioning provider | #5540 **AC-02**, **AC-08** (owned cleanup confirmed; account closure never implicit) |
| A6 | Production API adapter composition for all four API-side ports | All four globals are `None`; the boot gate correctly refuses | #5535 (w6-12) | Domain `app/services/*`, `app/installation.py` | #5540 **AC-01** (real ADP-origin requests succeed for the allowed workspace) |
| A7 | Governed controller provisioning + reconciliation — `governed_provisioning` computed, not hardcoded | Hardcoded `false` at `src/superplane-controller/main.go:81-84`; controller refuses to start under `SUPERPLANE_INSTALLATION_REQUIRED=true` (`main.go:87-96`) | #5536 (w6-13) | Domain Go controller + `main.go` startup | #5540 **AC-04** (provider resources join the intended EKS), **AC-05** (single owning controller) |
| A8 | Bounded environment preparation: database/schema/role/TLS/secret inputs, plan-only | Absent | #5537 (w6-14) | Domain installation environment tooling + runbooks | Wave 5 installation gate; #5540 consumes its output |
| A9 | Integrated release build + complete production installation, with compatibility checks | Installer exists (U23) with image-provenance and schema-head checks; no integrated release across shared services | #5538 (w6-15) | Domain release/installation workflows | Wave 5 installation acceptance; #5540 **AC-08** (ADP health preserved) |
| A10 | Bounded workload / Account Factory / recovery acceptance harness | Absent; #5288/#5289 and U12 scenarios to reuse | #5539 (w6-18) | Domain acceptance harness + runbooks | #5540 runs its published commands (all ACs) |

**#5400's fail-closed requirement is already satisfied and must stay satisfied.**
A5's provider is domain-owned, but the *facade it runs under* is B's — which is why
`provisioning_provider` (owner `workspace_infra`) and `operation_facade` (owner
`harness_jobs`) are separate registry entries. Until both exist, `POST /workspaces`
and `DELETE /workspaces/{id}` raise `ProvisioningRefused`, and **direct
provisioning is not a fallback** (`installation/runner.py:311-315`;
`main.go:89-91`).

---

## 3. This story's own capability — and its honest limit

| # | Capability | Producer | Evidence | Live verifier |
|---|---|---|---|---|
| W1 | The port map, obligations and unknown-answer vocabulary published as data with citations tests re-resolve | **#5524 (w6-01)** — this story | `superplane_contracts/integration.py`; `tests/test_integration_contract.py` | **None, and none is owed.** A registry is a specification; there is nothing live to observe. Its correctness claim is that it matches the code, which a test establishes exactly |
| W2 | Conformance probes an adapter must survive: forged workspace, forged operation, unminted authority, missing permission — **each executed as its own call, varying one field of a seeded valid control the adapter admits** | **#5524 (w6-01)** | `superplane_contracts/conformance.py`; `tests/test_conformance_probes.py::TestTheValidControlIsWhatMakesARefusalMeanSomething`, `::TestEachDimensionIsProbedInIsolation` | #5540 **AC-01**/**AC-06** exercise the same refusals against real boundaries. The suite is the specification; the live run is the evidence |
| W2a | The **startup smoke check** is one read-only probe per port and reports every rule dimension as unexercised — it never claims conformance | **#5524 (w6-01)** | `smoke_probes_for`; `SMOKE_LIMITATION`; `src/superplane-api/tests/test_capability_probes.py::TestCorrectAdaptersAreAccepted` | **None owed.** A claim about the report's own evidence boundary, which a test establishes fully. The per-rule claim it declines to make is W2's, and live acceptance is #5540's |
| W2b | An adapter that refuses **all legitimate work** is reported not-conformant, not silently passed by a negative-only suite | **#5524 (w6-01)** | `ProbeKind.VALID_CONTROL`; `ProbeVerdict.REFUSED_VALID_CONTROL`; `ConformanceReport.isolated` | #5540 **AC-01** — only a live run against a provisioned tenant establishes that real work is actually admitted. The offline control uses seeded identities |
| W3 | **The capability check exercises the configured adapter** instead of testing that an object exists | **#5524 (w6-01)** | `src/superplane-api/app/capability_probes.py`; `src/superplane-api/tests/test_capability_probes.py` | #5540 **AC-01** — against composed adapters. Offline today: **four `False`**, because nothing is composed |
| W4 | A dimension a call cannot present is reported **unexercised**, not as a refusal | **#5524 (w6-01)** | `ConformanceReport.not_exercised`; `not_exercised_for`; `tests/test_conformance_probes.py::TestProbeSetsCoverWhatEachPortBinds` | **None owed.** This is a claim about the report's own honesty, which a test establishes fully |

**W3 is the row that would be a lie if stated loosely.** The check is real —
it calls each configured adapter with input that is unauthorized by construction
and requires a refusal — but at this baseline there are no adapters, so it
correctly reports nothing is composed. Every report it emits carries
`CONFORMANCE_LIMITATION`: offline probes, no provider contacted. **Green here does
not mean the integration works**, and an operator reading a green report is exactly
the operator about to say it does.

That is also why W3's producer column names this story while its verifier names
#5540 with a stated caveat rather than a bare criterion: this story can establish
that the *check* is not a presence test, and cannot establish that any adapter
passes it.

**W2 and W4 exist as separate rows because the first revision of this story failed
them in two specific ways**, both caught in review, and the distinction is what a
sibling story needs to understand:

- **W2 — one call was credited to every probe.** The runner invoked each adapter
  *once*, with every field replaced by a sentinel simultaneously, then applied that
  single outcome to all of its probe descriptors. An adapter enforcing only the
  workspace was reported as correctly refusing an unminted authority, a forged
  operation identity and a missing permission — the exact bypass the probes exist to
  catch. Now each probe varies one field of a constant baseline and is issued
  separately, and `TestEachDimensionIsProbedInIsolation` proves each isolated bypass
  is detected by running deliberately partial adapters through the real runner.
- **W4 — a never-presented case was reported as verified.** A stale-version probe
  was emitted for all eleven ports and counted as a refusal, although **no port's
  call carries a contract version at all**. Now `carries_contract_version` gates it
  and the dimension is reported in `not_exercised`.

The lesson for the fifteen sibling stories: **a probe count is not a coverage
count.** Ask what request each probe actually sent, and whether a refusal could have
been produced by something other than the rule the probe names.

That question has a sharper form, and it is the one this story got wrong twice:
**a refusal is evidence about the varied dimension only if the rest of the request
would have been accepted.** Varying one field of a baseline that is already
unauthorized in every other field isolates nothing — every call can be refused for
the baseline alone. Per-rule evidence therefore requires a **valid control** the
adapter admits, verified *first*. Where no valid control exists (a boot gate,
offline, with no provisioned tenant and deliberately no credential), the honest
report is one smoke probe plus an explicit `not_exercised` list — **not** a set of
per-dimension refusals that look like coverage. See INTEGRATION-CONTRACT.md §4.3.

Corollary for sibling stories writing their own fixtures: a fixture adapter that
validates against its own fixture dict makes that fixture valid *by construction*,
so the guards on the fixture's contents are load-bearing. Ours were added only after
observing the suite stay green with the fixture reverted to sentinels.

Correspondingly, the boot gate's threshold (`composed`) is **not** the same bar as
`ConformanceReport.conformant`; conflating them was the second defect. The gate
previously failed only on `ADMITTED` and `NOT_IMPLEMENTED`, so a timeout or any
unexpected exception reported a port as composed — see INTEGRATION-CONTRACT.md §4.4
for why the offline-preflight justification for that leniency did not hold, and for
why `composed` and `conformant` are now reported separately.

---

## 4. Rule-1 and rule-2 audit

**Rule 1 (producer ≠ live verifier).** Twenty-seven rows. In all twenty-seven the
verifier column is #5540, the Wave 5 installation gate, or an explicit "none is
owed" — never the producing story. The six W-rows have this story as producer and
never itself as verifier.

**Rule 2 (no row ends at a mocked adapter).** Every row in §1 and §2 names a #5540
criterion with real provider or Organizations evidence. The four rows that do
**not** name one say why in the row itself:

- **W1** — a specification has nothing live to observe. Claiming a live verifier
  would invent an obligation nobody owes.
- **W2a** — the smoke tier's claim *is* that it establishes no rule. Naming a live
  verifier for it would invent per-rule evidence the check explicitly declines to
  produce; the per-rule obligation is W2's and its live verifier is #5540.
- **W3** — names AC-01 *and* states that today's answer is four `False`.
- **A8** — verified by the Wave 5 installation gate, which is a live gate, but not
  #5540's; saying "#5540 AC-08" would take credit for evidence a different gate
  produces.

**Offline evidence that is sufficient on its own.** Three properties are
*structural* and a live run would add nothing: illegal states being
unconstructible (`__post_init__` raising `ContractViolation`), `durable=True`
requiring a store's `confirmed_at`, and `authorize_provider_call` having no
permitting branch for "persistence was attempted". These are proven by
construction, not by observation. Every *behavioural* claim — an adapter refuses a
forged identity, an operation survives a restart, an inventory is complete — needs
#5540.

---

## 5. #5540's criteria, for reference

The single Wave 6 live gate (`ai-superplane-epic-a-4910/epic-4910/wave-6/eval`).
Reproduced so a row's verifier claim is checkable here. **"Completed code and ready
pods cannot satisfy this issue."**

| AC | Establishes | Rows it verifies |
|---|---|---|
| AC-01 | ADP login, workspace and provider binding; foreign workspace and spoofed identity denied; no credential disclosure | B7, B8, A6, W2, W3 |
| AC-02 | Account Factory and workspace lifecycle: real Organizations identity, scoped bootstrap, ACTIVE EKS, registered non-empty target; repeats create no duplicates | B2, A1, A2, A3, A5 |
| AC-03 | Existing-account and BYOC: actual target readiness, no accidental account creation, cluster takeover or deletion of supplied resources | A1, A4 |
| AC-04 | Batch provision/join/schedule/status/log/cancel with durable operation IDs, externally observed | B1, B7, A7 |
| AC-05 | Separate serving lifecycle: reachability, unauthorized denial, single owning controller, stop/cleanup | A7 |
| AC-06 | Monitoring, credential revocation and budgets: fresh observations, revoked/misbound authority denied, exact aggregate envelope respected, incurred costs reconciled | B4, B5, B8, B9, B10, W2 |
| AC-07 | Failure/restart/retry/recovery: operation identity and fences retained; no duplicate provision or early budget release after a lost provider response | B1, B2, B3, B6 |
| AC-08 | Cleanup and handoff: provider-verified complete inventory, retained resources and costs explicit, ADP health preserved, runbook usable | B11, A5, A9 |

All eight rows of §1's and §2's verifier column resolve into this table, and every
AC here has at least one producer row pointing at it. A capability with no AC would
be one nobody ever observes; an AC with no capability would be one nobody builds.

---

## 6. Ordering: what blocks what

Only the dependencies that actually constrain scheduling. Everything else can
proceed in parallel against this contract.

```
  #5524 (w6-01) reviewed schema  ─────────────────────┐   every story's stated
  "Shared contract specifics are owned by w6-01;      │   prerequisite
   its reviewed schema is required before             │
   dependent implementation."                         │
                                                      v
   B1/B2/B3 store+outbox (#5525) ──> B4/B5 admission (#5526) ──> B6/B7 executor (#5527)
   B8/B9 vault (#5528)            ──> B10/B11 authority (#5529)
   A1 factory (#5530) ──> A2 accounts (#5531)
   A3 workspace infra (#5532) ──> A4 bootstrap (#5533) ──> A5 provider (#5534)
                                                      │
   A6 API composition (#5535) needs B8, B10, B11, B1 ──┤  it composes their adapters
   A7 controller (#5536) needs B1, B6, A5 ────────────┤  it binds their authority
                                                      v
   A8 environment (#5537) ──> A9 release+install (#5538) ──> A10 harness (#5539)
                                                      v
                              #5540 live evaluation (after the explicit live gate)
```

Two constraints worth stating because violating either is silent:

- **A6 and A7 cannot be completed before the ports they compose exist.** Composing
  against a placeholder and declaring the capability true is precisely the defect
  W3 removes — and a hardcoded `governed_provisioning: true` would be strictly
  worse than today's honest `false`.
- **Migrations: authoring is not applying.** Every story's revisions land in its
  own owner's directory (`modules/harness/jobs/` for B, `src/superplane-api/alembic/`
  for the domain), and **application happens once, in A9's installation**.
  `tests/test_migrations.py::test_exactly_one_head` fails a branched head, so
  revision numbering must be coordinated with current `main` — two stories adding a
  revision in parallel is the expected collision, not a surprise.

---

## 7. What this matrix does not authorize

Implementation and offline verification only. **No account vending, AWS or
Kubernetes apply, database mutation, feature activation, image promotion or
workload spend is authorized by this document, by #5524, or by merging its code.**
Existing ADP availability, tenant/workspace boundaries, credentials and unrelated
resources are preserved.

A row's presence here means someone is named to build a capability and someone
else is named to observe it. It does not mean either has happened, and **mocked or
offline evidence never closes a live criterion** — which is the rule this entire
document exists to make checkable rather than aspirational.
