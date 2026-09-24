"""The domain's budget reservation journal, operated by a raw-SQL adapter.

Registered here for Alembic autogeneration so the declared schema and migration
`018_add_operation_budget_reservations` cannot drift. The rows themselves are
written by `app/adapters/operation_budget_ledger.py` over the harness's asyncpg
connection rather than through a SQLAlchemy session — see that module for why the
ledger cannot borrow the request's session.

Like the bootstrap journals beside it, this table deliberately has no workspace or
organization foreign key: a reservation is taken while admitting the operation that
may itself be creating the workspace.
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)

from app.database import Base

# The four reservation states, and the only place they are spelled in Python.
# Imported by the ledger adapter so the code and the check constraint cannot
# disagree about the vocabulary.
STATE_RESERVED = "reserved"
STATE_CONFIRMED = "confirmed"
STATE_RELEASED = "released"
STATE_RETAINED = "retained"

RESERVATION_STATES: tuple[str, ...] = (
    STATE_RESERVED,
    STATE_CONFIRMED,
    STATE_RELEASED,
    STATE_RETAINED,
)

# The states that still count against a workspace's budget. `released` does not:
# it means established absence, so the budget is genuinely free again. `retained`
# DOES, because it means "held because we do not know" — treating an unreconciled
# reservation as free is how the same budget gets spent twice.
COMMITTED_STATES: tuple[str, ...] = (
    STATE_RESERVED,
    STATE_CONFIRMED,
    STATE_RETAINED,
)


class OperationBudgetReservation(Base):
    __tablename__ = "operation_budget_reservations"
    __table_args__ = (
        UniqueConstraint(
            "job_id", "attempt_id", name="uq_operation_budget_reservations_attempt"
        ),
        CheckConstraint(
            "state IN ('reserved', 'confirmed', 'released', 'retained')",
            name="ck_operation_budget_reservations_state",
        ),
        CheckConstraint(
            "reservation_id !~ '^[[:space:]]*$'",
            name="ck_operation_budget_reservations_reservation_id",
        ),
        CheckConstraint(
            "job_id !~ '^[[:space:]]*$'",
            name="ck_operation_budget_reservations_job_id",
        ),
        CheckConstraint(
            "attempt_id !~ '^[[:space:]]*$'",
            name="ck_operation_budget_reservations_attempt_id",
        ),
        CheckConstraint(
            "org_id !~ '^[[:space:]]*$'",
            name="ck_operation_budget_reservations_org_id",
        ),
        CheckConstraint(
            "workspace_id !~ '^[[:space:]]*$'",
            name="ck_operation_budget_reservations_workspace_id",
        ),
        CheckConstraint(
            "max_resource_units >= 0 AND max_runtime_seconds >= 0 "
            "AND max_cost_micros >= 0",
            name="ck_operation_budget_reservations_envelope_non_negative",
        ),
        CheckConstraint(
            "state = 'reserved' OR (reason IS NOT NULL AND reason !~ '^[[:space:]]*$')",
            name="ck_operation_budget_reservations_reason_when_settled",
        ),
        Index(
            "ix_operation_budget_reservations_workspace_state",
            "workspace_id",
            "state",
        ),
        Index("ix_operation_budget_reservations_org_id", "org_id"),
        # Excluded from `tests/conftest.py`'s `create_all` for the HTTP SQLite
        # double, by the same marker the bootstrap journals carry
        # (`app/models/bootstrap.py`). Reusing their key rather than inventing a
        # second one keeps one filter in the fixture; what the key actually means
        # at that filter is "this DDL requires real PostgreSQL".
        #
        # It does here, and not as a preference: the blank-guard constraints above
        # use the POSIX operator `!~`, which SQLite's parser rejects outright
        # (`unrecognized token: "!"`), so including this table would not degrade
        # the double — it would break every test that uses it, including the ones
        # that never touch a reservation.
        #
        # Weakening the constraints to keep SQLite happy was the alternative and
        # is the wrong trade: the constraints are what make a blank `job_id` or a
        # negative envelope unrepresentable, which is the guarantee the ledger
        # relies on instead of re-validating on read. The ledger's own tests run
        # against real PostgreSQL, where these constraints are live.
        {"info": {"postgresql_bootstrap_journal": True}},
    )

    reservation_id = Column(String(128), primary_key=True)
    job_id = Column(String(255), nullable=False)
    attempt_id = Column(String(255), nullable=False)
    org_id = Column(Text, nullable=False)
    workspace_id = Column(Text, nullable=False)
    state = Column(String(32), nullable=False)
    # `BigInteger` micros, mirroring `harness_jobs.approval.SpendEnvelope`. Not
    # `Numeric`: the micros denomination exists so money is integer arithmetic.
    max_resource_units = Column(BigInteger, nullable=False)
    max_runtime_seconds = Column(BigInteger, nullable=False)
    max_cost_micros = Column(BigInteger, nullable=False)
    reason = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
