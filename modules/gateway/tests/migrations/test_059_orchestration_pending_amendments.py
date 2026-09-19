"""Tests for Alembic migration 059 — the pending-amendment tables.

Issue #4529 (EPIC #4191). This file is **mandatory**, and not only for coverage:
`modules/gateway/alembic/**` is absent from `gateway-ci.yml`'s trigger paths, so a
migration-only change gets **zero CI signal**. A test under `tests/` is what makes CI
run for this migration at all.

These tests exercise the REAL migration functions imported from the version module. A
test that re-implements the migration proves only that the author can write the same bug
twice.

What is under test, and why each matters for *this* story:

  - `upgrade()` creates both tables on an **alembic-only** database — no
    `Base.metadata.create_all` anywhere. That catches the quiet failure where a table is
    declared in models, has no DDL, and is absent on every deployed database. Here it
    would be especially quiet: with the tables missing, `replan` still writes its
    decision and still replies "recorded", so the platform looks exactly like the
    pre-#4529 behaviour this story exists to fix.
  - **Both uniqueness invariants**, asserted against the migration's own DDL rather than
    the model's, because the migration is what runs against a deployed database:
      * one replan decision ⇒ one authoring assignment (duplicate delivery converges);
      * one document ⇒ one draft per flow (a fail-soft author's retry converges).
    Without them the application-level existence reads are advisory, and two concurrent
    callers both read "absent" and both insert.
  - **The acceptance-provenance columns are nullable with no server_default.** A
    defaulted `accepted_by` would make a pending draft carry an acceptance actor, which
    is the "an agent cannot set the acceptance actor" requirement defeated by DDL.
  - **No backfill.** Existing `replan_requested` decisions must not become assignments;
    asserted structurally and behaviourally.
  - Postgres rendering, because tests run on SQLite and dev runs on Postgres —
    `proposal_document` must be `JSONB` there, and the timestamps timezone-aware.
  - Migration/model parity for both tables: both are hand-written, so drift is the live
    risk.
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.models import (
    AmendmentRequestState,
    OrchestrationAmendmentRequest,
    OrchestrationPendingAmendment,
    PendingAmendmentState,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

REQUESTS = "orchestration_amendment_requests"
DRAFTS = "orchestration_pending_amendments"
REQUEST_INDEX = "uq_orchestration_amendment_requests_decision"
DRAFT_INDEX = "uq_orchestration_pending_amendments_proposal"


def _load_migration(filename: str):
    """Import a migration module by path (they are not an importable package)."""
    path = MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG_059 = _load_migration("059_orchestration_pending_amendments.py")


def _run_migration(sync_conn, fn):
    """Run a migration's upgrade()/downgrade() with alembic's `op` proxy bound.

    The version module calls the module-level `op` proxy, so it must point at a real
    Operations object for the duration. This runs the migration as written rather than a
    paraphrase of it.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    ctx = MigrationContext.configure(sync_conn)
    with Operations.context(ctx):
        fn()


async def _bare_engine():
    """An engine with NO schema at all — no create_all, no models.

    This is the important fixture: `create_all` would build the tables from ORM metadata
    and mask a migration that never creates them itself.
    """
    return create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


async def _upgrade(engine):
    # Both tables carry an FK to `orchestration_flows`, and the drafts table one to the
    # requests table. SQLite does not enforce FKs unless asked, and this migration is not
    # the place to prove referential integrity — so `orchestration_flows` is deliberately
    # absent. That is also what keeps this an honestly "alembic-only, empty database"
    # fixture.
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_059.upgrade)


async def _downgrade(engine):
    async with engine.begin() as conn:
        await conn.run_sync(_run_migration, MIG_059.downgrade)


def _insert_request(
    request_id="req-1",
    org="org-1",
    flow="flow-a",
    decision="dec-1",
    by="user-1",
    version=3,
    plan_hash="a" * 64,
    text="gate the deploy wave",
    state=AmendmentRequestState.QUEUED.value,
    run=None,
):
    return sa.text(
        f"INSERT INTO {REQUESTS} "
        "(id, org_id, flow_id, replan_decision_id, requested_by, base_plan_version, base_plan_hash, "
        " request_text, state, author_run_id, created_at) "
        "VALUES (:id, :org, :flow, :decision, :by, :version, :plan_hash, :text, :state, :run, "
        " '2026-01-01 00:00:00')"
    ).bindparams(
        id=request_id,
        org=org,
        flow=flow,
        decision=decision,
        by=by,
        version=version,
        plan_hash=plan_hash,
        text=text,
        state=state,
        run=run,
    )


def _insert_draft(
    draft_id="draft-1",
    org="org-1",
    flow="flow-a",
    request_id="req-1",
    run="orch:author-1",
    version=3,
    base_hash="a" * 64,
    document='{"flow_slug": "demo", "nodes": []}',
    proposal_hash="b" * 64,
    state=PendingAmendmentState.PENDING.value,
):
    return sa.text(
        f"INSERT INTO {DRAFTS} "
        "(id, org_id, flow_id, request_id, author_run_id, base_plan_version, base_plan_hash, "
        " proposal_document, proposal_hash, state, created_at) "
        "VALUES (:id, :org, :flow, :request_id, :run, :version, :base_hash, :document, :proposal_hash, "
        " :state, '2026-01-01 00:00:00')"
    ).bindparams(
        id=draft_id,
        org=org,
        flow=flow,
        request_id=request_id,
        run=run,
        version=version,
        base_hash=base_hash,
        document=document,
        proposal_hash=proposal_hash,
        state=state,
    )


class TestAlembicOnlyDatabase:
    """The migration stands on its own, with no `create_all` involved."""

    async def test_upgrade_creates_both_tables_without_create_all(self):
        """Both tables exist after upgrade() on a database that started empty."""
        engine = await _bare_engine()
        async with engine.connect() as conn:
            before = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        assert before == set(), "fixture must start with an empty database for this to prove anything"

        await _upgrade(engine)

        async with engine.connect() as conn:
            after = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        await engine.dispose()

        assert {REQUESTS, DRAFTS} <= after, f"upgrade() did not create both tables; got {sorted(after)}"

    async def test_rows_are_insertable_after_alembic_only_upgrade(self):
        """Both tables are actually usable, not just present.

        Catches a table created with a shape the application cannot write to — e.g. a
        NOT NULL column neither the producer nor the registration path populates.
        """
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.begin() as conn:
            await conn.execute(_insert_request())
            await conn.execute(_insert_draft())

        async with engine.connect() as conn:
            request = (await conn.execute(sa.text(f"SELECT state, base_plan_version, request_text FROM {REQUESTS}"))).one()
            draft = (await conn.execute(sa.text(f"SELECT state, base_plan_version, author_run_id FROM {DRAFTS}"))).one()
        await engine.dispose()

        assert request == ("queued", 3, "gate the deploy wave")
        assert draft == ("pending", 3, "orch:author-1")

    async def test_a_flow_with_no_accepted_plan_can_be_replanned(self):
        """A NULL base version and hash are writable on both tables.

        Not a hypothetical: a flow may carry no accepted plan yet, and NOT NULL here
        would make `record_replan_request` raise on exactly that flow — turning "nothing
        is accepted yet" into a 500 on an ordinary human comment.
        """
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.begin() as conn:
            await conn.execute(_insert_request(version=None, plan_hash=None))
            await conn.execute(_insert_draft(version=None, base_hash=None))

        async with engine.connect() as conn:
            assert (await conn.execute(sa.text(f"SELECT base_plan_version FROM {REQUESTS}"))).scalar_one() is None
            assert (await conn.execute(sa.text(f"SELECT base_plan_hash FROM {DRAFTS}"))).scalar_one() is None
        await engine.dispose()

    async def test_a_whole_plan_document_round_trips(self):
        """`proposal_document` holds a plan-sized JSON body verbatim.

        Stored rather than referenced so acceptance cannot depend on a branch artifact
        staying unedited between authoring and the human's yes — which only works if the
        column can actually hold it.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        document = (
            '{"flow_slug": "demo", "title": "t", "org_id": "org-1", "nodes": '
            '[{"address": "demo/e1/w1/deploy-gate", "kind": "gate", "title": "Deploy?"}], '
            '"edges": [{"from_address": "demo/e1/w1/build", "to_address": "demo/e1/w1/deploy-gate"}]}'
        )

        async with engine.begin() as conn:
            await conn.execute(_insert_request())
            await conn.execute(_insert_draft(document=document))

        async with engine.connect() as conn:
            stored = (await conn.execute(sa.text(f"SELECT proposal_document FROM {DRAFTS}"))).scalar_one()
        await engine.dispose()
        assert "demo/e1/w1/deploy-gate" in stored


class TestRequestUniqueness:
    """One replan comment owes exactly one authoring job."""

    @pytest.fixture
    async def engine(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        yield engine
        await engine.dispose()

    async def test_decision_index_exists_and_is_unique(self, engine):
        """`(org_id, replan_decision_id)`, unique.

        Asserted on the migration's own DDL, because the migration is what runs against
        a deployed database. If this index is missing there, `record_replan_request`'s
        existence read is advisory: two concurrent ticks both read "not requested" and
        both insert, and the human's one ask summons two authors.
        """
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: sa_inspect(c).get_indexes(REQUESTS))

        unique = {i["name"]: tuple(i["column_names"]) for i in indexes if i["unique"]}
        assert REQUEST_INDEX in unique, f"decision uniqueness index missing; got {sorted(unique)}"
        assert unique[REQUEST_INDEX] == ("org_id", "replan_decision_id")

    async def test_duplicate_delivery_of_one_replan_is_rejected_by_the_database(self, engine):
        """The same replan decision cannot produce two assignments.

        This is the "duplicate events must reconcile to one authoring assignment"
        acceptance criterion, enforced below the application layer.
        """
        async with engine.begin() as conn:
            await conn.execute(_insert_request(request_id="req-1"))

        with pytest.raises(sa.exc.IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(_insert_request(request_id="req-2"))

    async def test_the_index_keys_on_the_decision_not_the_request_text(self, engine):
        """Two humans asking for the same change are two authorizations.

        Keying on `request_text` would silently collapse them, so the second human's ask
        would be answered by the first's draft — a plan applied on an authorization that
        was never given for it. Keyed on the decision instead, which is also what the
        authoring run's attribution roots in.
        """
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: sa_inspect(c).get_indexes(REQUESTS))
        index = next(i for i in indexes if i["name"] == REQUEST_INDEX)

        assert "replan_decision_id" in index["column_names"]
        assert "request_text" not in index["column_names"]
        assert "requested_by" not in index["column_names"]

    async def test_the_index_does_not_include_state(self, engine):
        """A dispatched request must keep occupying its decision.

        If `state` were in the key, marking a request `dispatched` would free its
        decision id for a *second* request row — so a re-delivered comment after a
        successful dispatch would queue a second author, which is precisely what the
        index exists to prevent.
        """
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: sa_inspect(c).get_indexes(REQUESTS))
        index = next(i for i in indexes if i["name"] == REQUEST_INDEX)
        assert "state" not in index["column_names"]

        async with engine.begin() as conn:
            await conn.execute(_insert_request(request_id="req-1"))
            await conn.execute(sa.text(f"UPDATE {REQUESTS} SET state = 'dispatched' WHERE id = 'req-1'"))

        with pytest.raises(sa.exc.IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(_insert_request(request_id="req-2"))

    async def test_distinct_decisions_and_tenants_coexist(self, engine):
        """Uniqueness is scoped to the decision within a tenant, not global.

        The cross-tenant row matters: an identical decision id under another `org_id`
        must not collide, or one tenant's replan could block another's.
        """
        async with engine.begin() as conn:
            await conn.execute(_insert_request(request_id="r1", decision="dec-1"))
            await conn.execute(_insert_request(request_id="r2", decision="dec-2"))
            await conn.execute(_insert_request(request_id="r3", decision="dec-1", org="org-2"))

        async with engine.connect() as conn:
            count = (await conn.execute(sa.text(f"SELECT count(*) FROM {REQUESTS}"))).scalar_one()
        assert count == 3


class TestDraftUniqueness:
    """One authored document is one draft per flow."""

    @pytest.fixture
    async def engine(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await conn.execute(_insert_request())
        yield engine
        await engine.dispose()

    async def test_proposal_index_exists_and_is_unique(self, engine):
        """`(org_id, flow_id, proposal_hash)`, unique."""
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: sa_inspect(c).get_indexes(DRAFTS))

        unique = {i["name"]: tuple(i["column_names"]) for i in indexes if i["unique"]}
        assert DRAFT_INDEX in unique, f"proposal uniqueness index missing; got {sorted(unique)}"
        assert unique[DRAFT_INDEX] == ("org_id", "flow_id", "proposal_hash")

    async def test_a_retried_registration_cannot_file_a_second_draft(self, engine):
        """The fail-soft author's retry converges instead of forking.

        Two ids for one document would make a human choose between drafts that mean the
        same thing — and whichever they accept, the other stays pending and then fails as
        stale, which reads like a bug in the amendment they just accepted.
        """
        async with engine.begin() as conn:
            await conn.execute(_insert_draft(draft_id="d1"))

        with pytest.raises(sa.exc.IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(_insert_draft(draft_id="d2", run="orch:author-1-retry"))

    async def test_the_index_keys_on_content_not_the_run(self, engine):
        """`proposal_hash`, not `author_run_id`.

        A run-keyed index would let one run file one draft and a *re-dispatched* run file
        an identical second one — the same document twice, which is the case idempotency
        exists to collapse. Keying on the canonical content hash makes the identity the
        proposal rather than the upload.
        """
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: sa_inspect(c).get_indexes(DRAFTS))
        index = next(i for i in indexes if i["name"] == DRAFT_INDEX)

        assert "proposal_hash" in index["column_names"]
        assert "author_run_id" not in index["column_names"]
        assert "request_id" not in index["column_names"]

    async def test_the_index_does_not_include_state(self, engine):
        """A superseded draft keeps occupying its content hash.

        If `state` were in the key, superseding a draft would free its document for a
        second pending row — so the amendment a human declined, or that lost a race,
        could be re-filed verbatim and offered again as if new.
        """
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: sa_inspect(c).get_indexes(DRAFTS))
        index = next(i for i in indexes if i["name"] == DRAFT_INDEX)
        assert "state" not in index["column_names"]

        async with engine.begin() as conn:
            await conn.execute(_insert_draft(draft_id="d1"))
            await conn.execute(sa.text(f"UPDATE {DRAFTS} SET state = 'superseded' WHERE id = 'd1'"))

        with pytest.raises(sa.exc.IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(_insert_draft(draft_id="d2"))

    async def test_different_documents_flows_and_tenants_coexist(self, engine):
        """Uniqueness is per document per flow per tenant.

        Two genuinely different proposals on one flow must both be able to wait — that is
        the concurrent-amendment case, where acceptance admits exactly one and supersedes
        the rest.
        """
        async with engine.begin() as conn:
            await conn.execute(_insert_draft(draft_id="d1", proposal_hash="b" * 64))
            await conn.execute(_insert_draft(draft_id="d2", proposal_hash="c" * 64))
            await conn.execute(_insert_draft(draft_id="d3", proposal_hash="b" * 64, flow="flow-b"))
            await conn.execute(_insert_draft(draft_id="d4", proposal_hash="b" * 64, org="org-2"))

        async with engine.connect() as conn:
            count = (await conn.execute(sa.text(f"SELECT count(*) FROM {DRAFTS}"))).scalar_one()
        assert count == 4


class TestRequestSchema:
    @pytest.fixture
    async def engine(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        yield engine
        await engine.dispose()

    @pytest.fixture
    async def columns(self, engine):
        async with engine.connect() as conn:
            return await conn.run_sync(lambda c: {x["name"]: x for x in sa_inspect(c).get_columns(REQUESTS)})

    async def test_table_carries_org_id(self, columns):
        """Tenant isolation is a column, not a convention."""
        assert columns["org_id"]["nullable"] is False

    async def test_attribution_columns_are_required(self, columns):
        """A request that cannot name its decision or its human authorizes nothing.

        `replan_decision_id` is in this set because it is the authoring run's attribution
        root: a NULL would produce an assignment traceable to no human act, which is the
        one thing an agent dispatch must never be. `requested_by` for the same reason on
        the display side.
        """
        for name in ("flow_id", "replan_decision_id", "requested_by", "request_text", "state", "created_at"):
            assert columns[name]["nullable"] is False, f"{name} must be NOT NULL"

    async def test_the_base_plan_columns_are_nullable(self, columns):
        """A flow may have no accepted plan yet; NULL says so honestly."""
        for name in ("base_plan_version", "base_plan_hash"):
            assert columns[name]["nullable"] is True, f"{name} must be nullable"

    async def test_dispatch_columns_are_nullable_with_no_server_default(self, columns):
        """`author_run_id` and `dispatched_at` are NULL until dispatch really happened.

        A `server_default` on `dispatched_at` would mark every request as already
        published the moment it was recorded, so a publish whose ack was lost would look
        complete and the authoring job would never be retried — the exact
        silently-nothing-happened failure this story has to make visible. A defaulted
        `author_run_id` would be worse: `resolve_authoring_request` compares the presented
        run against it, so a non-NULL default is a run id nobody was assigned.
        """
        for name in ("author_run_id", "dispatched_at"):
            assert columns[name]["nullable"] is True, f"{name} must be nullable"
            assert columns[name].get("default") is None, f"{name} must have no server_default"

    async def test_state_has_no_server_default(self, columns):
        """The application decides; the database must not guess.

        A `server_default` of `'dispatched'` would let a row that bypassed the producer
        look published and never be picked up. The model's Python-side default supplies
        `queued` on the real path.
        """
        assert columns["state"].get("default") is None

    async def test_replan_decision_id_is_not_a_foreign_key(self, engine):
        """No CASCADE path into the append-only decision log.

        `orchestration_decisions` is append-only and enforced so at the ORM boundary. An
        FK with `ondelete=CASCADE` would be a delete path into it; an FK with RESTRICT
        would be a constraint on a table nothing deletes from anyway. The value is
        server-written in the same transaction as the decision, so it is not dangling in
        practice.
        """
        async with engine.connect() as conn:
            fks = await conn.run_sync(lambda c: {tuple(f["constrained_columns"]): f for f in sa_inspect(c).get_foreign_keys(REQUESTS)})

        assert set(fks) == {("flow_id",)}, f"unexpected foreign keys: {sorted(fks)}"
        assert fks[("flow_id",)]["referred_table"] == "orchestration_flows"
        assert fks[("flow_id",)]["options"].get("ondelete") == "CASCADE"

    async def test_read_indexes_exist(self, engine):
        """The publisher reads requests by state, per tenant.

        Without `(org_id, state)` that read is a scan of a table that grows with every
        replan the platform ever handles, on every publisher pass.
        """
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: {i["name"]: tuple(i["column_names"]) for i in sa_inspect(c).get_indexes(REQUESTS)})

        assert indexes.get("ix_orchestration_amendment_requests_state") == ("org_id", "state")
        assert indexes.get("ix_orchestration_amendment_requests_flow_id") == ("org_id", "flow_id")
        assert "ix_orchestration_amendment_requests_org_id" in indexes


class TestDraftSchema:
    @pytest.fixture
    async def engine(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        yield engine
        await engine.dispose()

    @pytest.fixture
    async def columns(self, engine):
        async with engine.connect() as conn:
            return await conn.run_sync(lambda c: {x["name"]: x for x in sa_inspect(c).get_columns(DRAFTS)})

    async def test_table_carries_org_id(self, columns):
        assert columns["org_id"]["nullable"] is False

    async def test_identity_and_provenance_columns_are_required(self, columns):
        """A draft that cannot name its request or its run is unattributable.

        `request_id` NOT NULL is what makes every draft trace to a human replan — an
        orphan draft would be agent-authored content with no authorization behind it,
        waiting in a table whose whole purpose is to be accepted. `author_run_id` NOT NULL
        is what `register_amendment_draft` compares the presented run against, so the
        write is bound to the protected assignment rather than to route permission.
        """
        for name in ("flow_id", "request_id", "author_run_id", "proposal_document", "proposal_hash", "state", "created_at"):
            assert columns[name]["nullable"] is False, f"{name} must be NOT NULL"

    async def test_acceptance_provenance_columns_are_nullable_with_no_server_default(self, columns):
        """A pending draft must carry no acceptance actor, version or decision.

        This is "an agent cannot set the acceptance actor or status" expressed in DDL. A
        `server_default` on `accepted_by` would give every freshly filed draft an
        acceptance actor it never had, and the asymmetry between NULL-while-pending and
        set-once-accepted is exactly what makes the audit trail readable. `decided_at`
        likewise: defaulted, every pending draft would look decided.
        """
        for name in ("accepted_by", "accepted_by_decision_id", "accepted_plan_version", "superseded_by_draft_id", "decided_at"):
            assert columns[name]["nullable"] is True, f"{name} must be nullable"
            assert columns[name].get("default") is None, f"{name} must have no server_default"

    async def test_state_has_no_server_default(self, columns):
        """A `server_default` of `'accepted'` would apply nothing but claim everything.

        More realistically it would make a row that bypassed `register_amendment_draft`
        indistinguishable from one a human accepted. The model's Python-side default
        supplies `pending`, and `register_amendment_draft` sets it unconditionally.
        """
        assert columns["state"].get("default") is None

    async def test_foreign_keys_point_at_the_flow_and_the_request(self, engine):
        """A draft is meaningless without its flow or its request, so it dies with them.

        CASCADE on `request_id` is deliberate: deleting the request means the human's ask
        is gone, and a draft answering a request nobody made must not stay acceptable.
        """
        async with engine.connect() as conn:
            fks = await conn.run_sync(lambda c: {tuple(f["constrained_columns"]): f for f in sa_inspect(c).get_foreign_keys(DRAFTS)})

        assert set(fks) == {("flow_id",), ("request_id",)}, f"unexpected foreign keys: {sorted(fks)}"
        assert fks[("flow_id",)]["referred_table"] == "orchestration_flows"
        assert fks[("request_id",)]["referred_table"] == REQUESTS
        for fk in fks.values():
            assert fk["options"].get("ondelete") == "CASCADE"

    async def test_read_indexes_exist(self, engine):
        """The acceptance path reads this flow's drafts by status, per tenant."""
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: {i["name"]: tuple(i["column_names"]) for i in sa_inspect(c).get_indexes(DRAFTS)})

        assert indexes.get("ix_orchestration_pending_amendments_flow_state") == ("org_id", "flow_id", "state")
        assert indexes.get("ix_orchestration_pending_amendments_request") == ("org_id", "request_id")
        assert "ix_orchestration_pending_amendments_org_id" in indexes

    async def test_state_column_holds_every_declared_state(self, engine):
        """`String(16)` fits all four states, and a fifth would need no DDL.

        The states are a `StrEnum` in Python and a plain string column here on purpose: a
        database ENUM would make adding a state an ALTER on a live table, and this
        lifecycle is young enough that one more state is likely.
        """
        async with engine.begin() as conn:
            await conn.execute(_insert_request())
        for i, state in enumerate(PendingAmendmentState):
            assert len(state.value) <= 16, f"{state.value!r} does not fit String(16)"
            async with engine.begin() as conn:
                await conn.execute(_insert_draft(draft_id=f"d{i}", proposal_hash=str(i) * 64, state=state.value))

        async with engine.connect() as conn:
            stored = {r[0] for r in (await conn.execute(sa.text(f"SELECT state FROM {DRAFTS}"))).all()}
        assert stored == {s.value for s in PendingAmendmentState}


class TestDowngrade:
    async def test_downgrade_removes_both_tables(self):
        """The documented rollback plan; prove it works."""
        engine = await _bare_engine()
        await _upgrade(engine)
        await _downgrade(engine)

        async with engine.connect() as conn:
            remaining = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        await engine.dispose()

        assert REQUESTS not in remaining and DRAFTS not in remaining, "downgrade() left a table behind"

    async def test_downgrade_drops_the_tables_even_with_rows_present(self):
        """Unguarded means unguarded: pending drafts do not block rollback.

        Asserted explicitly because a nearby migration (049) raises on non-empty tables,
        and someone copying that pattern here would break the rollback. Nothing in these
        tables records a human approval — an accepted amendment's authority lives in
        `orchestration_accepted_plans` and its `PLAN_AMENDED` decision, untouched by this
        migration — so dropping them cannot un-accept anything. The cost is that a replan
        which had produced an un-accepted draft reverts to "requested, not authored", and
        the human re-issues it. If that ever stops being acceptable, this test is the
        review signal.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.begin() as conn:
            await conn.execute(_insert_request())
            await conn.execute(_insert_draft())

        await _downgrade(engine)

        async with engine.connect() as conn:
            remaining = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
        await engine.dispose()
        assert REQUESTS not in remaining and DRAFTS not in remaining

    async def test_upgrade_downgrade_upgrade_is_clean(self):
        """Rollback then re-deploy must work — that is the point of rollback.

        A downgrade that leaves an index behind fails the next upgrade on "index already
        exists", which is a broken deploy rather than a degraded one.
        """
        engine = await _bare_engine()
        await _upgrade(engine)
        await _downgrade(engine)
        await _upgrade(engine)

        async with engine.connect() as conn:
            tables = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
            request_indexes = await conn.run_sync(lambda c: {i["name"] for i in sa_inspect(c).get_indexes(REQUESTS)})
            draft_indexes = await conn.run_sync(lambda c: {i["name"] for i in sa_inspect(c).get_indexes(DRAFTS)})
        await engine.dispose()

        assert {REQUESTS, DRAFTS} <= tables
        assert REQUEST_INDEX in request_indexes
        assert DRAFT_INDEX in draft_indexes


class TestNoBackfill:
    """The migration creates two tables and invents no requests or drafts."""

    async def test_preexisting_rows_are_untouched(self):
        """A populated pre-existing table is bit-identical after upgrade()."""
        engine = await _bare_engine()

        async with engine.begin() as conn:
            await conn.execute(sa.text("CREATE TABLE preexisting (id VARCHAR(36) PRIMARY KEY, payload VARCHAR(64))"))
            await conn.execute(sa.text("INSERT INTO preexisting (id, payload) VALUES ('a', 'original-a'), ('b', 'original-b')"))
            before = (await conn.execute(sa.text("SELECT id, payload FROM preexisting ORDER BY id"))).all()

        await _upgrade(engine)

        async with engine.connect() as conn:
            after = (await conn.execute(sa.text("SELECT id, payload FROM preexisting ORDER BY id"))).all()
            cols = await conn.run_sync(lambda c: {x["name"] for x in sa_inspect(c).get_columns("preexisting")})
        await engine.dispose()

        assert after == before == [("a", "original-a"), ("b", "original-b")]
        assert cols == {"id", "payload"}, "upgrade() must not add columns to existing tables"

    async def test_upgrade_creates_no_request_or_draft_rows(self):
        """No invented assignments. An acceptance requirement, not tidiness.

        The obvious "helpful" migration would insert an
        `orchestration_amendment_requests` row for every historical
        `replan_requested` decision. That would queue an authoring agent per replan the
        moment the producer ships — retroactively dispatching work for conversations that
        concluded weeks ago, against plans that have since changed. A request row means
        "an authoring job is owed"; asserting that about the past asserts work nobody
        asked for now.
        """
        engine = await _bare_engine()
        await _upgrade(engine)

        async with engine.connect() as conn:
            requests = (await conn.execute(sa.text(f"SELECT count(*) FROM {REQUESTS}"))).scalar_one()
            drafts = (await conn.execute(sa.text(f"SELECT count(*) FROM {DRAFTS}"))).scalar_one()
        await engine.dispose()
        assert (requests, drafts) == (0, 0)

    def test_migration_source_contains_no_alter_update_or_insert(self):
        """Structural guard: the migration creates two tables and nothing else.

        Reading the source is the only way to assert the *absence* of a destructive or
        fabricating operation against tables this test does not know about — in
        particular `orchestration_accepted_plans`, which this story must not touch. A
        future edit that backfills or alters has to change this test, which is the review
        signal.
        """
        source = (MIGRATIONS_DIR / "059_orchestration_pending_amendments.py").read_text()
        # Strip the docstring, which legitimately discusses backfill and ALTER.
        body = source.split('"""', 2)[-1]
        lowered = body.lower()

        for forbidden in (
            "alter table",
            "op.alter_column",
            "op.add_column",
            "op.drop_column",
            "update ",
            "insert into",
            "op.bulk_insert",
            "op.execute",
        ):
            assert forbidden not in lowered, f"migration must only create the new tables; found {forbidden!r}"

    def test_migration_operates_on_no_table_but_its_own_two(self):
        """The plan of record is out of scope, structurally.

        #4529 adds a place for un-accepted proposals to wait; it must not reshape the
        table that holds accepted ones. An `op.*` call naming
        `orchestration_accepted_plans` would be the first step towards the
        `status`-column design this story explicitly rejected.

        Asserted over the parsed `op.*` calls rather than over the source text, because
        the migration's prose legitimately *discusses* that table — explaining that
        acceptance authority lives there and is untouched — and a substring check would
        forbid the explanation along with the operation. Reading arguments off the AST
        asserts the thing that matters: which tables the DDL touches.
        """
        import ast

        source = (MIGRATIONS_DIR / "059_orchestration_pending_amendments.py").read_text()
        tree = ast.parse(source)

        touched: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "op"):
                continue
            # Table name is the first string positional for create_table/create_index's
            # table argument, and the `table_name=` keyword for drop_index.
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    touched.add(arg.value)
            for keyword in node.keywords:
                if keyword.arg == "table_name" and isinstance(keyword.value, ast.Constant):
                    touched.add(keyword.value.value)

        assert touched, "found no op.* calls at all — the AST walk is broken, not the migration"
        # Index names are in `touched` too; filter to things that name a table.
        tables = {name for name in touched if name.startswith("orchestration_") and not name.startswith(("ix_", "uq_"))}
        assert tables == {REQUESTS, DRAFTS}, f"migration touches tables outside its own two: {sorted(tables - {REQUESTS, DRAFTS})}"


class TestPostgresRendering:
    """The tests run on SQLite, but dev runs on Postgres. Render for Postgres.

    Nothing else in this file would notice a Postgres-only DDL problem, because SQLite is
    more permissive — it does not even distinguish JSON from JSONB. Rendering the
    migration in alembic's offline (`--sql`) mode against the Postgres dialect exercises
    the compiler that actually matters, with no live database.
    """

    def _render_postgres_ddl(self) -> str:
        from sqlalchemy.dialects import postgresql

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        chunks: list[str] = []

        class _Buffer:
            def write(self, text):
                chunks.append(text)

            def flush(self):
                pass

        ctx = MigrationContext.configure(
            dialect=postgresql.dialect(),
            opts={"as_sql": True, "output_buffer": _Buffer()},
        )
        with Operations.context(ctx):
            MIG_059.upgrade()
        return "".join(chunks)

    def test_proposal_document_renders_as_jsonb(self):
        """`JSONB`, not `JSON`, on Postgres.

        A bare `sa.JSON` renders as `JSON` there — a text type with no operator support —
        and SQLite would never reveal the difference. This column holds whole plan
        documents, and the gate-diff read inspects their `nodes` array, so forfeiting
        JSONB forfeits every index and containment operator that read could ever use.
        """
        ddl = self._render_postgres_ddl()
        assert "proposal_document JSONB" in ddl, f"must render as JSONB on Postgres; got:\n{ddl[:2000]}"

    def test_timestamps_are_timezone_aware_on_postgres(self):
        """A naive timestamp makes acceptance ordering ambiguous across zones.

        It also has a concrete failure mode here: the acceptance path stamps
        `decided_at` from an aware `utcnow()`, and a naive column turns the comparison
        into a TypeError mid-transaction — failing the human's accept, not just a report.
        """
        ddl = self._render_postgres_ddl()
        assert "TIMESTAMP WITH TIME ZONE" in ddl
        assert ddl.count("TIMESTAMP WITHOUT TIME ZONE") == 0

    def test_hash_columns_are_wide_enough_for_a_sha256(self):
        """64 hex chars. `plan_hash` is SHA-256, and a narrower column truncates.

        A truncated `base_plan_hash` compares unequal to the real one, so every
        acceptance would be refused as stale under Postgres' stricter length handling —
        an amendment loop that can never close.
        """
        ddl = self._render_postgres_ddl()
        assert ddl.count("base_plan_hash VARCHAR(64)") == 2
        assert "proposal_hash VARCHAR(64)" in ddl

    def test_two_tables_and_two_unique_indexes_render(self):
        """Exactly two uniqueness rules, one per table.

        A third would be a third way to refuse a write, and a refused registration is an
        authored amendment that never reaches a human.
        """
        ddl = self._render_postgres_ddl()
        assert ddl.count("CREATE TABLE") == 2
        assert ddl.count("CREATE UNIQUE INDEX") == 2
        assert REQUEST_INDEX in ddl
        assert DRAFT_INDEX in ddl

    def test_the_requests_table_is_created_before_the_drafts_table(self):
        """Order matters: the drafts table's FK references the requests table.

        Reversed, the `CREATE TABLE` for drafts fails on Postgres against an
        unknown relation. SQLite would not care, so this is the only place the ordering
        is checked.
        """
        ddl = self._render_postgres_ddl()
        assert ddl.index(f"CREATE TABLE {REQUESTS}") < ddl.index(f"CREATE TABLE {DRAFTS}")


class TestRevisionChain:
    def test_revision_id_and_down_revision(self):
        """Chains onto the head that was real when this landed.

        Originally `052_orch_pending_amend` on top of `051_orch_pr_bindings`. Main then
        landed its own `052` (`052_orchestration_executions`, #5142) off that same parent
        and advanced to `058`, so keeping `051` here would have left the chain with **two
        heads** — an apply-time break that no line-level merge can detect, because the two
        migrations share no line to conflict on. Renumbered onto the real current tip.

        Pinning the exact parent (rather than just asserting a single head, which
        `test_migration_leaves_exactly_one_head` covers) is what makes an accidental
        re-fork onto a stale parent fail here instead of at `alembic upgrade head`.
        """
        assert MIG_059.revision == "059_orch_pending_amend"
        assert MIG_059.down_revision == "058_model_probe_admission"

    def test_revision_id_fits_the_alembic_version_column(self):
        """`alembic_version.version_num` is VARCHAR(32); a longer id fails at apply.

        This is why the revision is `059_orch_pending_amend` rather than the filename
        stem `059_orchestration_pending_amendments` (36 chars), which would exceed it.
        """
        assert len(MIG_059.revision) <= 32

    def test_migration_leaves_exactly_one_head(self):
        """Two heads is a broken deploy, and it is invisible until a pod runs
        `alembic upgrade head`.

        Asserts the *count*, not the head's name: the head advances with every migration
        that lands, and a name-pinned assertion turns every future migration into a
        spurious failure here — which trains people to edit this test rather than read it.
        What must never change is that there is exactly one head, and that 059 is still on
        the chain.
        """
        import ast

        revisions: dict[str, str | tuple[str, ...] | None] = {}
        for path in MIGRATIONS_DIR.glob("*.py"):
            if path.name == "__init__.py":
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            found: dict[str, str | tuple[str, ...] | None] = {}
            for node in tree.body:
                if not isinstance(node, ast.AnnAssign | ast.Assign):
                    continue
                targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
                names = {t.id for t in targets if isinstance(t, ast.Name)} & {"revision", "down_revision"}
                if not names or not isinstance(node.value, ast.Constant | ast.Tuple):
                    continue
                for name in names:
                    found[name] = ast.literal_eval(node.value)
            if "revision" in found:
                revisions[found["revision"]] = found.get("down_revision")

        parents = {parent for down in revisions.values() if down is not None for parent in ((down,) if isinstance(down, str) else down)}
        heads = sorted(rev for rev in revisions if rev not in parents)

        assert len(heads) == 1, f"expected exactly one head, got {heads}"
        assert "059_orch_pending_amend" in revisions, "059 must still be on the chain"


class TestModelMigrationParity:
    """The migrations and the models are hand-written separately, so they can drift.

    The migration is what runs against dev; the models are what every test uses. When
    they disagree, tests pass and production breaks — so compare them, for both tables.
    """

    @pytest.fixture
    async def migrated(self):
        engine = await _bare_engine()
        await _upgrade(engine)
        async with engine.connect() as conn:
            tables = {
                table: await conn.run_sync(lambda c, t=table: {x["name"]: x for x in sa_inspect(c).get_columns(t)}) for table in (REQUESTS, DRAFTS)
            }
            indexes = {
                table: await conn.run_sync(lambda c, t=table: {i["name"] for i in sa_inspect(c).get_indexes(t)}) for table in (REQUESTS, DRAFTS)
            }
        await engine.dispose()
        return tables, indexes

    @pytest.mark.parametrize(
        ("model", "table"),
        [(OrchestrationAmendmentRequest, REQUESTS), (OrchestrationPendingAmendment, DRAFTS)],
    )
    async def test_column_names_match(self, migrated, model, table):
        columns, _ = migrated
        model_cols = {c.name for c in model.__table__.columns}
        assert model_cols == set(columns[table]), (
            f"model/migration column drift on {table} — "
            f"only in model: {model_cols - set(columns[table])}, "
            f"only in migration: {set(columns[table]) - model_cols}"
        )

    @pytest.mark.parametrize(
        ("model", "table"),
        [(OrchestrationAmendmentRequest, REQUESTS), (OrchestrationPendingAmendment, DRAFTS)],
    )
    async def test_nullability_matches(self, migrated, model, table):
        """A column the model calls optional and the migration calls NOT NULL fails only
        on the deployed database, where nothing tests it."""
        columns, _ = migrated
        declared = {c.name: c.nullable for c in model.__table__.columns}
        assert declared == {name: col["nullable"] for name, col in columns[table].items()}

    @pytest.mark.parametrize(
        ("model", "table"),
        [(OrchestrationAmendmentRequest, REQUESTS), (OrchestrationPendingAmendment, DRAFTS)],
    )
    async def test_column_types_match(self, migrated, model, table):
        """Rendered types, not Python types: `String(16)` vs `String(32)` and
        `Integer` vs `BigInteger` are both `str`/`int` in the model annotation."""
        from sqlalchemy.dialects import sqlite

        columns, _ = migrated
        dialect = sqlite.dialect()
        declared = {c.name: str(c.type.compile(dialect=dialect)) for c in model.__table__.columns}
        found = {name: str(col["type"].compile(dialect=dialect)) for name, col in columns[table].items()}
        assert declared == found

    @pytest.mark.parametrize(
        ("model", "table"),
        [(OrchestrationAmendmentRequest, REQUESTS), (OrchestrationPendingAmendment, DRAFTS)],
    )
    async def test_index_names_match(self, migrated, model, table):
        """Model-declared indexes must exist in the migration under the same names.

        Drift here is quiet: the ORM index only exists where `create_all` ran, so a query
        is fast in tests and a sequential scan in dev.
        """
        _, indexes = migrated
        declared = {ix.name for ix in model.__table__.indexes}
        # `TenantMixin` declares org_id with index=True, which SQLAlchemy names implicitly
        # rather than adding to `__table__.indexes`.
        declared.add(f"ix_{table}_org_id")
        assert declared <= indexes[table], f"declared on the model but missing from the migration: {sorted(declared - indexes[table])}"

    @pytest.mark.parametrize(
        ("model", "table"),
        [(OrchestrationAmendmentRequest, REQUESTS), (OrchestrationPendingAmendment, DRAFTS)],
    )
    async def test_no_duplicate_index_names_on_the_model(self, model, table):
        """Two indexes with one name is a migration that fails on its second CREATE.

        A real trap on these tables: `flow_id` is a natural candidate for `index=True`,
        and the name SQLAlchemy generates for that is exactly
        `ix_<table>_flow_id` — which collides with the tenant-leading composite
        `(org_id, flow_id)` index declared in `__table_args__`. SQLAlchemy does not
        complain at import time; Postgres complains at deploy time.
        """
        names = [ix.name for ix in model.__table__.indexes]
        assert len(names) == len(set(names)), f"duplicate index names on {table}: {sorted(names)}"

    async def test_declared_state_values_fit_the_column(self):
        """Every enum member fits `String(16)`, on both tables.

        A member that does not fit is silently truncated on SQLite and rejected on
        Postgres, so a new state would pass tests and fail in dev.
        """
        for state in (*AmendmentRequestState, *PendingAmendmentState):
            assert len(state.value) <= 16, f"{state.value!r} does not fit String(16)"
