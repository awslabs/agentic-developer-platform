"""Receiver-side state for the observation contract — issue #5056 (U15).

Two tables, each existing because the contract cannot be honoured without it.

``observation_receipts`` is the per-cluster authenticated submission stream. A
valid HMAC proves a body was produced by a credential holder; it says nothing
about whether that body has already been submitted. The freshness window in
``verify_submission`` bounds how *old* a replay can be, not how many times the
same body may be replayed inside it, so the receiver has to remember what it
last accepted per cluster. This table is that memory.

``observation_leases`` replaces the monitor's write access to ``reconcile_locks``.
It is a separate table rather than a column added to that one, because the point
of U15 is that the monitor no longer writes to a domain table at all — leases are
granted by the receiver, so their storage is the receiver's.

The one non-obvious column is ``fence_token``, and the reason it is on this table
rather than derived: it must be monotonic *across* releases. A released lease's
row is retained with its token so the next grant advances past it. Deleting the
row on release — which is what the old ``DELETE FROM reconcile_locks`` did —
would reset the sequence and let a stalled previous holder's token compare equal
to a new one, which is exactly the overlap the fence exists to prevent.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ObservationReceipt(Base):
    """The last observation accepted for one cluster, keyed by cluster.

    One row per cluster rather than per submission: the receiver needs to answer
    "have I seen this, and is it newer than what I have?", which is a question
    about the latest accepted submission. Keeping every submission would be an
    audit log, and the events table already is one.
    """

    __tablename__ = "observation_receipts"

    cluster_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # The workspace this stream was authorized for, as resolved from stored
    # cluster ownership at the time of acceptance — never from the payload.
    # Indexed to match the migration and to support the scoped read path, which
    # filters recorded observations by the caller's authorized workspace.
    workspace: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    submitter_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # `reported_at` of the last accepted submission. Monotonic: a submission at
    # or before this instant is a replay, not news.
    last_reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # SHA-256 of the exact accepted body bytes, so a retry of the identical
    # submission is recognisable as idempotent rather than refused as a replay.
    # A digest, not the body: storing bodies would grow without bound and would
    # persist whatever a submitter chose to put in a label.
    last_body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    last_status: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ObservationLease(Base):
    """A lease on a named scope, with the highest fence token ever issued for it.

    ``holder``/``expires_at`` are nullable because a released lease keeps its row
    (see the module docstring on why the token must survive release); a row with
    no holder is a scope that has been held before and is free now.
    """

    __tablename__ = "observation_leases"

    scope: Mapped[str] = mapped_column(String(255), primary_key=True)
    holder: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # BigInteger rather than Integer: this only ever increases, once per acquire,
    # and a monitor acquiring every 30 seconds crosses a 32-bit ceiling in
    # decades rather than centuries. Widening it later would need a migration on
    # a table the fence depends on.
    fence_token: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    # Which submitter last held it, retained after release for operator
    # attribution when two monitors contend for the same scope.
    last_holder: Mapped[str | None] = mapped_column(String(255), nullable=True)
    acquire_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
