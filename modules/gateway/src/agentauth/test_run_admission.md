# Preparatory test-run admission primitive

This is the internal **model-only** admission primitive for tools accounting
(#8078). Its protected application composition requires explicit registry,
deployment, owner, source and native-login authority. Unsupported tool quotes and
model routes refuse. No live qualification result is implied by its tests.

The adjacent enrollment and provider-handoff implementation is described
in [the native-test contract](../../../../docs/design-notes/8078-native-test-admission.md).
That contract describes the mandatory application mount, authenticated control
routes and protected deployment prerequisites. Its approved CI job/source proof
does not attest an arbitrary local executable, and model-only accounting cannot
enable the all-in real-tools evaluation.

`TestRunAdmissionService.prepare_model` accepts an authenticated human context,
an opaque scoped credential, a stable operation UUID, exact bytes and route. It
does not accept a quote, price, cap, source override or authority boolean. A trusted
server verifier must resolve canonical membership and mandatory approved runtime/run
identity. Its output binds the authenticated JWT subject, canonical principal and
hierarchy, existing run, source, runtime and credential ID. A raw JWT with asserted
test headers is insufficient. The route adapter must enforce that the
designated test credential/principal cannot omit this scope and fall back to an
ordinary paid route; the native-test runtime owns this mandatory route boundary.

The store reads an already-provisioned authority document. Every field is explicit:
identity binding; schema/revision; active state; exact $20 cumulative cap; immutable
meter epoch; dispatch, cleanup and meter deadlines; allowed model routes/models.
The separate dispatch-expiry Dynamo attribute must match the canonical document.
Conditional transactions compare the exact document and live deadline. There is
no enrollment, run creation, authority update, accumulator initialization or reset
API. A missing record, meter anchor or usage denominator refuses.

Operations bind exact bytes and route before pricing. One owner journals the
server quote and deterministic reservation ID before reserving. Replays only
return status; changed payload/source/identity conflicts. No automatic owner
takeover is supported, including a lost successful claim response. Preparing or
unknown operations require subsequent reconciliation work, not another submit.

Registered provider quote adapters supply the amount, model, pricing revision and
expiry. Full provider context bounds remain intact. The service rejects existing
delivery-flow/protected-run contexts instead of replacing their target. It uses a
fresh human budget context and the existing typed-model-quote strict
reservation path. Its target uses the existing `run` entity type with a separate
`tools-test:<existing-run-UUID>` entity ID and immutable test meter epoch. It is
not an accepted delivery flow. The test target and ordinary hierarchy targets
participate in the **same** reservation Lua call. Accepted flow caps are untouched.
The human model USER/person budget path keeps its existing authenticated JWT
subject key; the canonical human owner remains separately bound in the journal.
Team and department come from the verifier's current canonical membership.
Only `StrictTestBudgetEnforcementService` may be injected. Its dedicated person
budget hook propagates accounting failures into the existing strict refusal
boundary; the shared gateway's compatibility behavior is unchanged. Accounting
incompleteness is checked again after hierarchy admission.

The accumulator includes earlier actual costs and live bounds, including existing
cleanup composite holds. This slice never creates, modifies or releases cleanup
holds. It performs no paid setup and admits no tool lifecycle. Initial integration
must retain the existing real-execution harness refusal until supported provider
quotes, setup/cleanup admission and native mandatory identity are all deployed.

`begin_model_dispatch` revalidates identity, authority, exact quote binding and
every original test/hierarchy hold, then conditionally records one dispatch intent. It makes no
provider call. Only a fresh in-process prepared object can attempt handoff; a
status receipt is not dispatch authority. A lost intent acknowledgment cannot
authorize another handoff. The integration must place this immediately before the
provider boundary and propagate the original context/IDs to trusted usage writing.
It must not run another independent budget admission or mint an operation ID on
network recovery. The primitive itself does not consume rate limits; its protected
middleware composition preserves the existing rate-limit path.
The durable journal owns the reservation ID, quote and complete original target
metadata, including strict-anchor requirements. The in-process receipt and
context must still match it; replacing a receipt or weakening a same-key target
does not transfer authority. The intent CAS guards the full journal document.
Missing, expired, changed, or unknown holds refuse without re-creating them.
An aggregate read of each original target rejects uncertain siblings, including
unbounded records on ordinary hierarchy targets. Strict targets also require
their live anchor/pending marker; ordinary hierarchy targets retain their
existing marker-free representation.

Pricing is confirmed after the hold reads. Quote, credential, dispatch and
reservation deadlines are checked again inside the thread immediately before the intent CAS
and after its acknowledgement. A late successful CAS consumes the intent and
refuses provider handoff while preserving the holds; it cannot be replayed into
another attempt. The reservation deadline includes every original own hold and
each unsettled sibling hold/pending marker; a later await cannot turn a previously
known ledger into unknown spend and still authorize dispatch. Settled or bounded
sibling expiry retains its existing semantics. These checks do not make Redis, Dynamo and the provider one
transaction. The eventual provider adapter must still enforce its immediate
handoff and trusted-usage contract.

Quotes and confirmed reservations are distinct in receipts. A lost reservation
response keeps a durable quote/ID and unknown status without claiming a confirmed
reserved amount. Redis retains any actual hold. A successful admitted journal
records exact target keys and metadata for recovery. No settlement/release API exists here;
later adoption must persist authoritative provider usage, settle the exact scope
through existing pricing/settlement services, verify the ledger, and preserve all
unknown/cleanup obligations. Missing usage is never zero.

Offline tests use moto Dynamo transactions and the actual reservation Lua through
fakeredis/lupa. Identity and price adapters are explicit offline substitutions;
they do not attest a native runtime or produce approved live pricing. Tests cover
concurrent identities, same-ID replay, atomic hierarchy denial, scope/authority
changes, missing state, lost write acknowledgments, single-use handoff, unknown
holds, cleanup preservation and separation of quoted/reserved amounts.

Adoption still requires independent review, a protected authority/enrollment and
mandatory native credential contract, original facade/CLI/MCP integration, tool
quote enforcement, trusted settlement and deployed acceptance. This source slice
must not be used to remove the paid-execution refusal on its own.
