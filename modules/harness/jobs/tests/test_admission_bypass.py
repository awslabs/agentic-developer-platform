"""AC-02: a generic jobs client cannot bypass admission or borrow another identity.

Issue #5526 (w6-03), EPIC #4910, Wave 6.

This is the acceptance criterion most likely to be satisfied "by intention" -- the gate
is documented, the docstrings say to use it, and nothing checks. So these tests actually
attempt the two bypasses the criterion names:

1. **Reaching dispatch without passing admission.** The store is a public class with a
   public `admit()`, and it is a legitimate part of this package's API. What must not be
   true is that the two are *interchangeable*: the store must not claim to check
   approval, and the gate must not be optional for anything that does.
2. **Obtaining permissions by supplying another workspace identity.** Attempted through
   every field that reaches admission -- the request, its parameter map, the approval
   binding and the derived ids.

The structural half deliberately has no module-level `requires_postgres` mark. #5525
learned this the hard way (`test_contract_agreement.py`, "it was written there, where
the module-level mark meant it skipped on any machine without a database") -- a check
that the gate cannot be bypassed must not be the check that is silent on a developer
machine.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harness_jobs import (
    REQUIRED_PERMISSION,
    ContractViolation,
    OperationRequest,
    OperationStore,
    ResolvedPrincipal,
)
from harness_jobs import admission as admission_module
from harness_jobs.admission import (
    BudgetLedger,
    CreationFence,
    Reservation,
    admit_operation,
    derive_operation_identity,
)
from harness_jobs.approval import (
    APPROVAL_PERMISSION,
    ApprovalBinding,
    ApprovalRecord,
    ApprovalRefused,
    ApprovalResult,
    ApproverStatus,
    SpendEnvelope,
)

from .conftest import requires_postgres

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
REQUESTER = "user:alice"
APPROVER = "user:boss"


def principal(
    org: str = "org-a", workspace: str = "ws-1", subject: str = REQUESTER
) -> ResolvedPrincipal:
    return ResolvedPrincipal(
        org_id=org,
        workspace_id=workspace,
        subject=subject,
        permissions=frozenset({REQUIRED_PERMISSION}),
    )


def request(key: str = "key-1", **parameters: str) -> OperationRequest:
    return OperationRequest(
        action="provision", idempotency_key=key, parameters=dict(parameters)
    )


def envelope() -> SpendEnvelope:
    return SpendEnvelope(
        max_resource_units=4, max_runtime_seconds=3600, max_cost_micros=5_000_000
    )


def approval(
    *, actor: ResolvedPrincipal | None = None, req: OperationRequest | None = None
) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id="appr-1",
        binding=ApprovalBinding.for_request(actor or principal(), req or request()),
        envelope=envelope(),
        result=ApprovalResult.ALLOWED_ONCE,
        approvers=frozenset({APPROVER}),
        decided_by=APPROVER,
        decided_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=1),
    )


def statuses() -> dict[str, ApproverStatus]:
    return {
        APPROVER: ApproverStatus(
            subject=APPROVER,
            is_member=True,
            permissions=frozenset({APPROVAL_PERMISSION}),
        )
    }


class NullLedger:
    """A ledger that records nothing and must never be reached in this module.

    Every test here expects a refusal *before* the ledger is consulted. Asserting on
    `calls` being empty is what distinguishes "refused" from "refused after reserving
    budget", and the second one still costs headroom on every attack attempt.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def reserve(self, **kwargs: object) -> Reservation:
        self.calls.append("reserve")
        return Reservation(
            reservation_id="res-1",
            job_id=str(kwargs["job_id"]),
            attempt_id=str(kwargs["attempt_id"]),
        )

    async def confirm(self, **kwargs: object) -> None:
        self.calls.append("confirm")

    async def release(self, **kwargs: object) -> None:
        self.calls.append("release")

    async def retain(self, **kwargs: object) -> None:
        self.calls.append("retain")


# ---------------------------------------------------------------------------
# The gate is not optional, and the store does not stand in for it
# ---------------------------------------------------------------------------


def test_the_store_does_not_claim_to_check_approval():
    """`OperationStore` has no approval parameter, so it cannot appear to check one.

    The dangerous version of this package is one where `store.admit()` grows an optional
    `approval=None` argument: callers that pass nothing then look like they are using an
    approval-aware API, and the default is no approval at all. Asserted on the signature
    so that change fails here.
    """
    parameters = set(inspect.signature(OperationStore.admit).parameters)
    for smell in ("approval", "envelope", "budget", "ledger"):
        assert not any(smell in name for name in parameters), (
            f"OperationStore.admit takes a {smell!r} parameter. The store is the "
            "durable write; adjudicating approval is the gate's job, and a store that "
            "appears to do both makes 'which one did I call' the security boundary"
        )


def test_the_gate_requires_an_approval_argument_with_no_default():
    """`admit_operation` cannot be called without deciding about approval.

    `approval` is typed `ApprovalRecord | None` because absence is a case the gate must
    handle -- but it must be *supplied*, not defaulted. A default of `None` would mean
    `admit_operation(...)` with no approval is a valid call that reads as approved
    admission, which is the whole bypass in one keyword argument.
    """
    signature = inspect.signature(admit_operation)
    for required in (
        "approval",
        "requested_envelope",
        "approver_statuses",
        "now",
    ):
        parameter = signature.parameters[required]
        assert parameter.default is inspect.Parameter.empty, (
            f"{required} has a default; the gate must not be satisfiable by omission"
        )
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{required} is positional; keyword-only is what stops a caller supplying "
            "it by accident in the wrong slot"
        )


def test_the_gate_does_not_let_a_caller_choose_the_ledger_key():
    """No operation_id / job_id / attempt_id parameters on `admit_operation`.

    `OperationStore.admit` accepts them, for tests. The gate must not, because the job
    and attempt ids *are* the ledger's idempotency key: a caller who can choose a fresh
    key can present the same approval twice and have the ledger grant a second hold,
    because from the ledger's side those are two different attempts.
    """
    parameters = set(inspect.signature(admit_operation).parameters)
    for forbidden in ("operation_id", "job_id", "attempt_id"):
        assert forbidden not in parameters, (
            f"admit_operation accepts {forbidden!r}; a caller-chosen ledger key is a "
            "caller-chosen budget renewal"
        )


def test_the_derived_identity_is_a_function_of_the_approval_alone():
    """Same approval -> same three ids, every time and in every process.

    This is what makes the retry safe. Deterministic rather than random, and derived
    from the approval rather than the request, so a caller varying the request cannot
    vary the key it presents to the ledger under one approval.
    """
    first = derive_operation_identity("appr-1")
    second = derive_operation_identity("appr-1")
    assert first == second
    assert derive_operation_identity("appr-2") != first
    # Three distinct values, not one repeated: they are different identities with
    # different lifetimes, and a later story fences on the attempt.
    assert len(set(first)) == 3


def test_this_module_implements_no_ledger():
    """The domain owns the ledger; a class here satisfying it would be a second answer.

    `BudgetLedger` and `CreationFence` are Protocols. If this package shipped a concrete
    implementation, composition would have something local to bind to and the "domain
    accounting remains Superplane-owned" boundary in the issue's impact surface would be
    breached by a default.
    """
    for protocol in (BudgetLedger, CreationFence):
        assert getattr(protocol, "_is_protocol", False), (
            f"{protocol.__name__} is no longer a Protocol; a concrete class here is a "
            "budget authority this package must not hold"
        )

    source = inspect.getsource(admission_module)
    tree = ast.parse(source)
    ledger_methods = {"reserve", "confirm", "release", "retain"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if any(
            isinstance(base, ast.Name) and base.id == "Protocol" for base in node.bases
        ):
            continue
        defined = {
            child.name
            for child in node.body
            if isinstance(child, ast.AsyncFunctionDef | ast.FunctionDef)
        }
        assert not ledger_methods <= defined, (
            f"class {node.name} implements the full ledger interface inside this "
            "package; the ledger is the domain's"
        )


def test_this_module_computes_no_cost():
    """No arithmetic on the envelope's amounts anywhere in the admission path.

    `accounting.py:31-34`: "a second place computing cost is a second answer that can
    disagree with the real one." The gate compares an envelope against another envelope
    (`covers`), and passes amounts through unchanged; it never adds, subtracts or scales
    them. Asserted by looking for arithmetic on the three amount fields rather than by
    reading the code, because this is the property a helpful future edit breaks first.
    """
    amounts = {"max_resource_units", "max_runtime_seconds", "max_cost_micros"}
    tree = ast.parse(inspect.getsource(admission_module))
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp):
            continue
        names = {
            child.attr
            for child in ast.walk(node)
            if isinstance(child, ast.Attribute) and child.attr in amounts
        }
        assert not names, (
            f"the admission path performs arithmetic on {sorted(names)}; computing a "
            "cost here is a second answer to a question the domain ledger owns"
        )


def test_the_admission_module_reads_no_configuration():
    """Same property `facade.py` is held to, for the module that now holds the ordering.

    A gate that could open its own connection or read a DSN would be a second
    composition root, and "is the gate installed" would become a property of this source
    tree rather than of the reviewed startup sequence.
    """
    source = inspect.getsource(admission_module)
    for smell in ("os.environ", "getenv", "create_pool", "DATABASE_URL"):
        assert smell not in source, (
            f"{smell!r} appears in admission.py; connection and credential handling "
            "belongs to the composer"
        )


def test_the_gate_lives_in_the_harness_and_not_in_the_domain_app():
    """#5524 §3.4: admission tables belong to the harness, not to the domain API.

    "A Wave 6 story that puts the admission record or the outbox in
    `src/superplane-api/alembic/` has implemented shared jobs in the domain API." The
    consumption table is an admission table, so this asserts it was not *created*
    there.

    ## Why this looks for DDL rather than for the table's name

    The first version scanned every `.py` file for the substring
    ``harness_approval_consumption``, and #5535 produced two false positives that are
    worth recording, because both are cases the property should permit:

    * **A comment.** `018_add_operation_budget_reservations.py` names the table to say
      its own four-state vocabulary agrees with the harness's check constraint. A
      reference that exists to keep the two sides of the seam from disagreeing is the
      opposite of a bypass, and a detector that forbids it pushes the next author to
      delete the explanation rather than the coupling.
    * **A staged copy of this package.** `scripts/stage-domain-auth.sh` copies
      `harness_jobs` into `src/superplane-api/vendor/` so `docker build` can reach it —
      gitignored, refreshed per run, never committed. Those files ARE the harness, so
      flagging them reported the harness for living in the harness.

    Both were dismissable by inspection, which is the problem: a check that needs a
    human to dismiss it every time stops being read. So the assertion is now about the
    thing §3.4 actually forbids — the domain app declaring or writing the table — and
    the prose-and-copies cases can no longer trip it.

    `vendor/` is excluded by path rather than by content. It is build output; anything
    found there says what the staging script copied, not what this repository
    maintains, and it is the one directory under the component whose contents are not
    reviewed as domain source.
    """
    repo = Path(__file__).resolve().parents[4]
    component = (
        repo / "modules" / "domain-apps" / "superplane" / "src" / "superplane-api"
    )
    if not component.is_dir():
        pytest.skip("the domain app is not present in this checkout")

    # Statements that would make the domain app an owner of the table rather than a
    # reader of the harness's. `INSERT`/`UPDATE`/`DELETE` are included because writing
    # a consumption row is asserting an admission decision, which is the bypass — not
    # merely an ownership smell.
    owning = (
        "CREATE TABLE",
        "DROP TABLE",
        "ALTER TABLE",
        "INSERT INTO",
        "UPDATE",
        "DELETE FROM",
        # The SQLAlchemy and Alembic spellings, so a declarative model or a migration
        # op is caught as well as raw SQL.
        "__tablename__",
        "create_table",
        "drop_table",
        "add_column",
    )
    offenders = []
    for path in component.rglob("*.py"):
        if "vendor" in path.relative_to(component).parts:
            continue
        text = path.read_text(errors="ignore")
        if "harness_approval_consumption" not in text:
            continue
        # Same line, so a migration that creates an unrelated table in a file that
        # merely mentions this one in a comment is not reported.
        for number, line in enumerate(text.splitlines(), start=1):
            if "harness_approval_consumption" not in line:
                continue
            statement = next((word for word in owning if word in line), None)
            if statement is not None:
                offenders.append(f"{path.relative_to(repo)}:{number} ({statement})")
    assert not offenders, (
        f"the domain app declares or writes the approval-consumption table: "
        f"{offenders}. Shared admission belongs to the harness — the domain app may "
        f"read the harness's table through this package, and may name it in a comment, "
        f"but may not own it."
    )


# ---------------------------------------------------------------------------
# Identity smuggling (AC-02)
# ---------------------------------------------------------------------------


def test_a_request_cannot_carry_a_workspace_identity_at_all():
    """The field does not exist, so there is nothing for admission to prefer.

    Established by #5525 for the store and re-asserted here because the gate is a new
    caller of the same type: if `OperationRequest` grew a tenant field, every argument
    about where the tenant comes from would have to be re-made.
    """
    assert "org_id" not in OperationRequest.__dataclass_fields__
    assert "workspace_id" not in OperationRequest.__dataclass_fields__


@pytest.mark.parametrize(
    "key",
    ["org_id", "workspace_id", "workspaceId", "workspace-id", "tenant_id", "orgId"],
)
def test_a_tenant_claim_in_the_parameter_map_is_refused(key: str):
    """The dict-shaped side channel, which a missing field does not close."""
    with pytest.raises(ContractViolation):
        OperationRequest(
            action="provision", idempotency_key="k", parameters={key: "ws-victim"}
        )


async def test_an_approval_bound_to_another_workspace_does_not_admit():
    """A stolen approval is not spendable from the thief's workspace.

    The binding names the workspace it was granted for, and the gate compares it against
    the *resolved* principal -- so presenting a valid approval from a different tenant
    is refused, and refused before the ledger is called.
    """
    ledger = NullLedger()
    victim = principal(org="org-victim", workspace="ws-victim")
    thief = principal(org="org-thief", workspace="ws-thief")

    with pytest.raises(ApprovalRefused):
        await admit_operation(
            None,  # never reached: the refusal precedes every database access
            OperationStore(),
            ledger,
            principal=thief,
            request=request(),
            approval=approval(actor=victim),
            requested_envelope=envelope(),
            approver_statuses=statuses(),
            now=NOW,
        )
    assert ledger.calls == [], "the ledger was consulted for a refused request"


async def test_an_approval_granted_to_another_subject_does_not_admit():
    """Nor is an approval transferable to whoever presents it.

    The same workspace, a different requester. Without this, a low-privilege principal
    in a workspace could spend an approval granted to a colleague.
    """
    ledger = NullLedger()
    with pytest.raises(ApprovalRefused):
        await admit_operation(
            None,
            OperationStore(),
            ledger,
            principal=principal(subject="user:mallory"),
            request=request(),
            approval=approval(),
            requested_envelope=envelope(),
            approver_statuses=statuses(),
            now=NOW,
        )
    assert ledger.calls == []


async def test_no_approval_at_all_admits_nothing():
    """The absence case, through the gate rather than through `evaluate_approval`.

    A generic client that simply does not have an approval is the most likely bypass
    attempt, and it must be a refusal rather than a permissive default.
    """
    ledger = NullLedger()
    with pytest.raises(ApprovalRefused, match="absence is not permission"):
        await admit_operation(
            None,
            OperationStore(),
            ledger,
            principal=principal(),
            request=request(),
            approval=None,
            requested_envelope=envelope(),
            approver_statuses=statuses(),
            now=NOW,
        )
    assert ledger.calls == []


# ---------------------------------------------------------------------------
# The database half: the store's own write is still refused without permission
# ---------------------------------------------------------------------------


@requires_postgres
async def test_the_store_still_refuses_a_principal_without_the_permission(connection):
    """The gate is a layer on top of the store's check, not a replacement for it.

    Asserted against the database so that "a caller who skipped the gate" is genuinely
    tested rather than argued about: bypassing `admit_operation` gets you the store's
    permission check, which is weaker than the gate but is not nothing.
    """
    from harness_jobs import OperationRefused

    unprivileged = ResolvedPrincipal(
        org_id="org-a", workspace_id="ws-1", subject=REQUESTER, permissions=frozenset()
    )
    with pytest.raises(OperationRefused):
        await OperationStore().admit(connection, unprivileged, request())

    assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 0


@requires_postgres
async def test_an_operation_admitted_without_the_gate_is_never_delivered(connection):
    """CXR-001: an unpaid operation must not reach an executor.

    Auditing it is not enough.

    The reproduction: `OperationStore.admit` with a provision-capable principal, then
    `DispatchOutbox.drain_once`. It reported `delivered=1` against zero consumption
    rows -- the work ran, nothing paid for it, and the only control was a query someone
    had to remember to run. A control that fires after the provisioning is a report.

    The store still cannot refuse this caller; that is deliberate (#5525 left approval
    out, and the store's question is "well-formed, unique, tenant-scoped"). The repair
    is
    that eligibility is a predicate inside `claim`: without a spendable
    `harness_approval_consumption` row the outbox row is not claimable, so a bypass
    produces an operation that exists and never dispatches.

    Detectability is kept as a secondary assertion. It is still useful -- an operator
    needs to find these rows -- but it is evidence, not the control.
    """
    from harness_jobs import DispatchOutbox

    await OperationStore().admit(connection, principal(), request("unpaid"))

    class RecordingExecutor:
        def __init__(self) -> None:
            self.envelopes: list[object] = []

        async def deliver(self, envelope):
            self.envelopes.append(envelope)

    executor = RecordingExecutor()
    report = await DispatchOutbox().drain_once(connection, executor)

    assert executor.envelopes == [], (
        "an operation that never passed the approval gate was handed to an executor; "
        "this is CXR-001, and the work has already run by the time an audit sees it"
    )
    assert report.delivered == 0
    assert report.failed == 0 and report.exhausted == 0, (
        "the row must be unclaimable, not claimed-and-failed: a failed attempt burns "
        "an "
        "attempt and eventually abandons the row, which hides the bypass"
    )
    assert (
        await connection.fetchval("SELECT max(attempts) FROM harness_dispatch_outbox")
        == 0
    )

    # The row is still there, still undelivered, and still findable -- not silently
    # dropped. A bypass that vanished from the queue would be just as hard to
    # investigate as one that shipped.
    queued = await connection.fetchval("SELECT count(*) FROM harness_dispatch_outbox")
    assert queued == 1
    assert await DispatchOutbox().ineligible_count(connection) == 1
    ineligible = await DispatchOutbox().list_ineligible(connection)
    assert len(ineligible) == 1
    assert ineligible[0][1] is None, (
        "a never-paid row must be distinguishable from one whose reservation was "
        "released or retained: the first is a gate bypass, the second is compensation"
    )
