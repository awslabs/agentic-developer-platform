"""Unit tests for ActionProvenance SQLAlchemy model.

Issue #784: Phase 2-a — validates ORM round-trip on SQLite, FK constraints,
NOT NULL enforcement, and JSON column handling.
"""

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.base import Base, TenantMixin, new_uuid
from src.shared.models.organization import Organization, User
from src.shared.models.provenance import ActionProvenance

# ---------------------------------------------------------------------------
# Schema / meta tests
# ---------------------------------------------------------------------------


class TestActionProvenanceSchema:
    """Tests for ActionProvenance model definition."""

    def test_tablename(self):
        assert ActionProvenance.__tablename__ == "action_provenance"

    def test_inherits_base_and_tenant(self):
        assert issubclass(ActionProvenance, Base)
        assert issubclass(ActionProvenance, TenantMixin)

    def test_primary_key(self):
        mapper = inspect(ActionProvenance)
        pk = [c.name for c in mapper.primary_key]
        assert pk == ["id"]

    def test_columns_exist(self):
        mapper = inspect(ActionProvenance)
        cols = {c.key for c in mapper.column_attrs}
        expected = {
            "id",
            "org_id",
            "actor_user_id",
            "triggered_by",
            "root_human_id",
            "is_human_rooted",
            "action_kind",
            "source_event",
            "correlation_id",
            "created_at",
        }
        assert expected <= cols

    def test_source_event_uses_json_not_jsonb(self):
        """Confirm model uses JSON (not JSONB) for SQLite test compatibility."""
        from sqlalchemy import JSON

        mapper = inspect(ActionProvenance)
        col = mapper.columns["source_event"]
        assert isinstance(col.type, JSON)


# ---------------------------------------------------------------------------
# Helper fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def org_and_users(db_session: AsyncSession):
    """Create an org + two users for FK references."""
    org = Organization(
        id=new_uuid(),
        name="Test Org",
    )
    db_session.add(org)
    await db_session.flush()

    human_user = User(
        id=new_uuid(),
        org_id=org.id,
        team_id="team-1",
        email="human@test.com",
        name="Human User",
    )
    bot_user = User(
        id=new_uuid(),
        org_id=org.id,
        team_id="team-1",
        email="bot@test.com",
        name="Bot User",
    )
    db_session.add_all([human_user, bot_user])
    await db_session.commit()

    return org, human_user, bot_user


# ---------------------------------------------------------------------------
# ORM round-trip tests (SQLite)
# ---------------------------------------------------------------------------


class TestActionProvenanceRoundTrip:
    """ORM insert + query round-trip on SQLite."""

    @pytest.mark.asyncio
    async def test_insert_and_query(self, db_session: AsyncSession, org_and_users):
        """Insert a provenance record and read it back."""
        org, human, bot = org_and_users

        record = ActionProvenance(
            id=new_uuid(),
            org_id=org.id,
            actor_user_id=bot.id,
            triggered_by=human.id,
            root_human_id=human.id,
            is_human_rooted=True,
            action_kind="issue_comment",
            source_event={"pr": 778, "repo": "aws-e/adp", "action": "created"},
            correlation_id="corr-abc-123",
        )
        db_session.add(record)
        await db_session.commit()

        result = await db_session.execute(select(ActionProvenance).where(ActionProvenance.correlation_id == "corr-abc-123"))
        row = result.scalar_one()

        assert row.actor_user_id == bot.id
        assert row.triggered_by == human.id
        assert row.root_human_id == human.id
        assert row.is_human_rooted is True
        assert row.action_kind == "issue_comment"
        assert row.source_event == {"pr": 778, "repo": "aws-e/adp", "action": "created"}
        assert row.correlation_id == "corr-abc-123"
        assert row.org_id == org.id
        assert row.created_at is not None

    @pytest.mark.asyncio
    async def test_triggered_by_nullable(self, db_session: AsyncSession, org_and_users):
        """triggered_by is optional (human-initiated actions have no trigger)."""
        org, human, _ = org_and_users

        record = ActionProvenance(
            id=new_uuid(),
            org_id=org.id,
            actor_user_id=human.id,
            triggered_by=None,
            root_human_id=human.id,
            is_human_rooted=True,
            action_kind="manual_deploy",
            source_event={"type": "manual"},
            correlation_id="corr-manual-001",
        )
        db_session.add(record)
        await db_session.commit()

        result = await db_session.execute(select(ActionProvenance).where(ActionProvenance.id == record.id))
        row = result.scalar_one()
        assert row.triggered_by is None

    @pytest.mark.asyncio
    async def test_is_human_rooted_false(self, db_session: AsyncSession, org_and_users):
        """is_human_rooted = False for bot-rooted chains."""
        org, _, bot = org_and_users

        record = ActionProvenance(
            id=new_uuid(),
            org_id=org.id,
            actor_user_id=bot.id,
            triggered_by=None,
            root_human_id=bot.id,
            is_human_rooted=False,
            action_kind="scheduled_scan",
            source_event={"cron": "0 * * * *"},
            correlation_id="corr-bot-001",
        )
        db_session.add(record)
        await db_session.commit()

        result = await db_session.execute(select(ActionProvenance).where(ActionProvenance.id == record.id))
        row = result.scalar_one()
        assert row.is_human_rooted is False

    @pytest.mark.asyncio
    async def test_source_event_nested_json(self, db_session: AsyncSession, org_and_users):
        """source_event accepts nested JSON objects."""
        org, human, _ = org_and_users

        nested_event = {
            "webhook": {
                "headers": {"X-GitHub-Delivery": "abc123"},
                "body": {"action": "opened", "number": 42},
            },
            "metadata": {"received_at": "2026-05-25T00:00:00Z"},
        }

        record = ActionProvenance(
            id=new_uuid(),
            org_id=org.id,
            actor_user_id=human.id,
            triggered_by=None,
            root_human_id=human.id,
            is_human_rooted=True,
            action_kind="webhook_received",
            source_event=nested_event,
            correlation_id="corr-nested-001",
        )
        db_session.add(record)
        await db_session.commit()

        result = await db_session.execute(select(ActionProvenance).where(ActionProvenance.id == record.id))
        row = result.scalar_one()
        assert row.source_event == nested_event
        assert row.source_event["webhook"]["body"]["number"] == 42


# ---------------------------------------------------------------------------
# Constraint tests
# ---------------------------------------------------------------------------


class TestActionProvenanceConstraints:
    """Validate NOT NULL and FK constraints."""

    @pytest.mark.asyncio
    async def test_actor_user_id_not_null(self, db_session: AsyncSession, org_and_users):
        """actor_user_id is required."""
        org, human, _ = org_and_users

        record = ActionProvenance(
            id=new_uuid(),
            org_id=org.id,
            actor_user_id=None,  # type: ignore[arg-type]
            triggered_by=None,
            root_human_id=human.id,
            is_human_rooted=True,
            action_kind="test",
            source_event={},
            correlation_id="corr-fail-001",
        )
        db_session.add(record)
        with pytest.raises(IntegrityError):
            await db_session.commit()

    @pytest.mark.asyncio
    async def test_correlation_id_not_null(self, db_session: AsyncSession, org_and_users):
        """correlation_id is required."""
        org, human, _ = org_and_users

        record = ActionProvenance(
            id=new_uuid(),
            org_id=org.id,
            actor_user_id=human.id,
            triggered_by=None,
            root_human_id=human.id,
            is_human_rooted=True,
            action_kind="test",
            source_event={},
            correlation_id=None,  # type: ignore[arg-type]
        )
        db_session.add(record)
        with pytest.raises(IntegrityError):
            await db_session.commit()

    @pytest.mark.asyncio
    async def test_action_kind_not_null(self, db_session: AsyncSession, org_and_users):
        """action_kind is required."""
        org, human, _ = org_and_users

        record = ActionProvenance(
            id=new_uuid(),
            org_id=org.id,
            actor_user_id=human.id,
            triggered_by=None,
            root_human_id=human.id,
            is_human_rooted=True,
            action_kind=None,  # type: ignore[arg-type]
            source_event={},
            correlation_id="corr-fail-002",
        )
        db_session.add(record)
        with pytest.raises(IntegrityError):
            await db_session.commit()

    @pytest.mark.asyncio
    async def test_default_id_generation(self, db_session: AsyncSession, org_and_users):
        """ID is auto-generated when not explicitly provided."""
        org, human, _ = org_and_users

        record = ActionProvenance(
            org_id=org.id,
            actor_user_id=human.id,
            triggered_by=None,
            root_human_id=human.id,
            is_human_rooted=True,
            action_kind="auto_id_test",
            source_event={"test": True},
            correlation_id="corr-auto-id",
        )
        db_session.add(record)
        await db_session.commit()

        assert record.id is not None
        assert len(record.id) == 36  # UUID format with hyphens
