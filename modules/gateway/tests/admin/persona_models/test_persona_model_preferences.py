"""Tests for persona-model preference endpoints — Issue #5419 (PMM-02).

**AC coverage map and honest disclosure:**

- AC-01: **Nothing in this file bears on AC-01.** The ``create_all``/``drop_all``
  tests here prove the ORM models are coherent; they never execute the migration.
  AC-01 is proven in ``tests/migrations/test_056_persona_model_prefs.py``, which
  executes ``upgrade()``/``downgrade()`` on a bare database, renders the DDL for
  the PostgreSQL dialect, and runs the real Alembic CLI end-to-end against a real
  PostgreSQL 16 server (clean upgrade, downgrade to predecessor, re-upgrade).
- AC-02: Save and list through real HTTP routes.  Bypasses the fail-closed validator
  by patching it to return the model string.  This is intentional: AC-02 proves the
  preference store and list, not the validator (which is AC-07's job).
- AC-03: Unknown persona, non-configurable persona, empty model — each refused with
  a stable reason code.
- AC-04: Self endpoint ignores injected ``principal_id`` in body; proves the *other*
  principal's row is unchanged (not just that the response looks right).
- AC-05: Creates a real tenant-B principal, then proves tenant-A callers cannot read
  or write it.  Also asserts no audit row is created in tenant B.
- AC-06: Stale-revision conflict via sequential requests, **plus a genuinely
  concurrent create** (``asyncio.gather`` over two independent sessions) proving one
  winner, one 409, one stored row. An earlier revision of this docstring claimed
  "true concurrent-transaction testing requires Postgres and is not provable in
  SQLite". That was wrong, and it is why a real create-path race shipped unnoticed:
  what defeats concurrency testing here is not SQLite but the ``engine`` fixture's
  ``StaticPool``, which hands every session the same connection. The
  ``concurrent_engine`` fixture uses a file-backed database instead and the
  interleaving is directly observable.
- AC-07: Proves the fail-closed stub refuses every write with ``probing_disabled``.
  Does NOT prove refusal against a real catalogue (requires PMM-03).
- AC-08: Save (via patched validator), then reset, asserting the audit records the
  actual model **transition** — two writes so the second has a real predecessor,
  then ``before_model``/``after_model``/``revision`` and provenance by value, plus
  ``actor_id`` being the canonical ID and never a raw subject. An earlier version
  asserted only that keys were ``in details``, which passes no matter how wrong
  every value is, and did pass while ``before_model`` was absent entirely. A
  refused write is also audited, and the reset audit must name what it removed.
- AC-09: Service account self-management through the real route.  Patched validator
  for the write path; service account attempting admin route gets 403.
- AC-10: Human org-admin sets and lists for in-tenant service principal (patched
  validator); cross-tenant attempt is refused.  Audit is distinct (``persona_model_admin_set``).
  A cross-tenant attempt is audited in the CALLER's tenant and leaves the victim's
  trail empty (§5.6) — it previously recorded nothing anywhere.
- AC-11: **Behavioural, not structural.** Drives the mutating verbs at the policy
  table (the class names, the ``__platform__`` sentinel, path traversal,
  ``default``) with the validator patched so a refusal is not what protects the
  row, then asserts the policy-settings rows are unchanged field by field. The
  earlier version asserted only that no route *path* contained "policy" or
  "settings", which proves nothing — it passes for a route that rewrites the
  platform default on every call. Replacing it immediately exposed three real
  defects: an unhandled 500 on ``DELETE`` with an unknown persona key on both the
  self and admin surfaces, and a persona catalogue enforced only inside the
  replaceable PMM-03 validator seam, so a model-only replacement would have let a
  row be stored under any key at all.

**Gate 2 — service-principal identity lifecycle (operator comment 17):**

- Register: happy path (new SP + first alias + audit), duplicate active alias refused,
  service caller 403.
- Link alias: happy path (additional alias + audit), unknown principal refused.
- Revoke alias: happy path (deactivates + audit), not-found refused.
- Re-register after revoke: creates a new canonical principal (revoked aliases are
  permanently dead — design invariant).
- Non-admin human: cannot register, cannot list manageable principals.
- Auth carrier: ``canonical_service_principal_id`` on TokenContext is used when
  populated; empty canonical field is REFUSED (no fallback).
- Lifecycle transitions: active→suspended, suspended→retired (valid);
  retired→active (refused, terminal); active→active (refused, no self-transition).

**What this file's schema assertions are worth — read before trusting them.**

This suite builds its schema with ``Base.metadata.create_all`` on SQLite. That
never executes the Alembic migration, so **no assertion here says anything about
whether the migration is valid.** An earlier revision of this docstring claimed
"Invariants NOT proven (Postgres-only): None identified — all constraints use
portable constructs". That claim was false, and the cost of it was three
PostgreSQL-only defects reaching main under a green run of this file: an integer
default on a boolean column, a ``COALESCE`` over a ``timestamptz`` against ``''``,
and then a ``CAST``-based repair that PostgreSQL rejects as non-``IMMUTABLE``
inside an index expression. The third passed both a PostgreSQL-dialect compile and
a SQLite execution, and was caught only by a live ``alembic upgrade head``.

Schema and migration invariants therefore belong in
``tests/migrations/test_056_persona_model_prefs.py``. Add them there, not here.

**What this file does prove**, all of it dialect-independent service and route
behaviour:

- ``uq_persona_model_pref_scope`` — plain composite unique, no dialect guard.
- ``ck_persona_pref_principal_kind`` — CHECK constraint.
- Compare-and-set (revision conflict) — service-layer logic.
- Authorization, refusal and audit behaviour of the routes.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers
from starlette.requests import Request

from src.auth.cognito_jwt import CognitoJWTValidator
from src.auth.dependencies import get_current_user
from src.shared.models.audit import AuditLog
from src.shared.models.base import Base, new_uuid
from src.shared.models.organization import User
from src.shared.models.persona_models import (
    PersonaModelPolicySetting,
    PersonaModelPreference,
    ServicePrincipal,
    ServicePrincipalAlias,
)
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext

os.environ["TESTING"] = "1"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["AWS_REGION"] = "us-east-1"
os.environ["JWT_SECRET_KEY"] = "test-secret-key"
os.environ["BG_TOKEN_SECRET_KEY"] = "test-secret-key"
os.environ["REDIS_URL"] = ""

TEST_ORG_A = "org-a"
TEST_ORG_B = "org-b"
TEST_USER_ID = "user-canonical-123"
TEST_USER_COGNITO_SUB = "cognito-sub-123"
TEST_OTHER_USER_ID = "user-other-456"
TEST_SP_CANONICAL_ID = "sp-canonical-789"
TEST_SP_ALIAS_ID = "agent-worker-1"
TEST_ADMIN_USER_ID = "user-admin-001"


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def engine():
    e = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield e
    await e.dispose()


@pytest.fixture
async def concurrent_engine(tmp_path):
    """A file-backed engine whose sessions get genuinely separate connections.

    The ``engine`` fixture above deliberately uses ``StaticPool`` over
    ``:memory:`` so every session shares one connection and one in-memory
    database — correct for the route tests, but it makes two "concurrent"
    sessions a single transaction, so no interleaving can be observed. Any test
    asserting real concurrency must use this fixture instead; see
    ``test_ac06_concurrent_create_yields_one_row_and_a_conflict``.

    Seeds only the user row the concurrency tests anchor on.
    """
    e = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'concurrent.db'}")
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(e, expire_on_commit=False)
    async with factory() as session:
        session.add(
            User(
                id=TEST_USER_ID,
                org_id=TEST_ORG_A,
                team_id="team-1",
                name="testuser",
                email="test@example.com",
                cognito_sub=TEST_USER_COGNITO_SUB,
            )
        )
        await session.commit()

    yield e
    await e.dispose()


@pytest.fixture
async def db(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest.fixture
async def seed_data(db: AsyncSession):
    """Seed user, service principal, alias, and platform default."""
    db.add(
        User(
            id=TEST_USER_ID,
            org_id=TEST_ORG_A,
            team_id="team-1",
            name="testuser",
            email="test@example.com",
            cognito_sub=TEST_USER_COGNITO_SUB,
        )
    )
    db.add(
        User(
            id=TEST_OTHER_USER_ID,
            org_id=TEST_ORG_A,
            team_id="team-1",
            name="otheruser",
            email="other@example.com",
            cognito_sub="cognito-sub-other",
        )
    )
    db.add(
        User(
            id=TEST_ADMIN_USER_ID,
            org_id=TEST_ORG_A,
            team_id="team-1",
            name="adminuser",
            email="admin@example.com",
            cognito_sub="cognito-sub-admin",
        )
    )
    db.add(
        ServicePrincipal(
            canonical_service_principal_id=TEST_SP_CANONICAL_ID,
            org_id=TEST_ORG_A,
            display_name="Test Worker",
            status="active",
            approved_by=TEST_ADMIN_USER_ID,
        )
    )
    db.add(
        ServicePrincipalAlias(
            id=new_uuid(),
            canonical_service_principal_id=TEST_SP_CANONICAL_ID,
            org_id=TEST_ORG_A,
            alias_source="agent_registry",
            alias_id=TEST_SP_ALIAS_ID,
            is_active=True,
            registered_by=TEST_ADMIN_USER_ID,
        )
    )
    db.add(
        PersonaModelPolicySetting(
            compatibility_class="claude-agent-sdk",
            active_default_model_id="us.anthropic.claude-sonnet-4-6",
            enforcement_posture="report_only",
        )
    )
    await db.commit()


def _human_context(user_id: str = TEST_USER_ID, org_id: str = TEST_ORG_A) -> TokenContext:
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="team-1",
        department_id="dept-1",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        auth_source="jwt",
    )


def _admin_context(user_id: str = TEST_ADMIN_USER_ID, org_id: str = TEST_ORG_A) -> TokenContext:
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="team-1",
        department_id="dept-1",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        auth_source="jwt",
    )


def _service_context(
    user_id: str = TEST_SP_ALIAS_ID,
    org_id: str = TEST_ORG_A,
    canonical_id: str = TEST_SP_CANONICAL_ID,
    alias_source: str = "agent_registry",
) -> TokenContext:
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id="",
        department_id="",
        account_type="service",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        auth_source="iam",
        canonical_service_principal_id=canonical_id,
        canonical_alias_source=alias_source,
    )


@pytest.fixture
async def client(engine, seed_data):
    """HTTP client with overridden DB and auth dependencies."""
    from src.app import create_app

    app = create_app()
    from src.admin.persona_models.self_routes import get_persona_model_current_user
    from src.auth.dependencies import get_current_user
    from src.shared.database import get_db

    _current_context = _human_context()

    async def override_get_db():
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    async def override_get_current_user():
        return _current_context

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    # Override the PMM enrichment dependency too — it calls resolve_by_exact_source
    # which needs the DB session. In tests, the context is pre-populated.
    app.dependency_overrides[get_persona_model_current_user] = override_get_current_user

    transport = ASGITransport(app=app)
    # PMM-02 route tests do not own the external Agent Registry.  Keep their
    # managed-principal fixture on the no-explicit-restriction path; dedicated
    # PMM-03 tests exercise the real resolver and override this seam when they
    # need to prove route propagation.
    managed_policy = AsyncMock(return_value=([], None))
    with patch(
        "src.admin.persona_models.catalogue_routes.resolve_managed_service_restriction_policy",
        managed_policy,
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            c._app = app  # type: ignore[attr-defined]
            yield c
    app.dependency_overrides.clear()


def _set_context(client: AsyncClient, ctx: TokenContext):
    """Switch the auth context for subsequent requests."""
    from src.admin.persona_models.self_routes import get_persona_model_current_user
    from src.auth.dependencies import get_current_user

    client._app.dependency_overrides[get_current_user] = lambda: ctx  # type: ignore[attr-defined]
    client._app.dependency_overrides[get_persona_model_current_user] = lambda: ctx  # type: ignore[attr-defined]


def _patch_validator():
    """Patch the fail-closed validator to accept any non-empty model (for tests that need writes)."""

    async def _accept(
        db,
        *,
        org_id,
        principal_kind,
        canonical_principal_id,
        persona_key,
        model,
        account_id=None,
        region=None,
        principal_status=None,
        service_restriction_pattern_sets=None,
        policy_unavailable_reason=None,
    ):
        return model.strip()

    return patch("src.admin.persona_models.service.validate_model_for_persona", side_effect=_accept)


# ── AC-01: Table creation ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ac01_tables_created(engine):
    """AC-01: Tables exist after create_all with documented columns and uniqueness.

    This proves the SQLAlchemy model metadata is consistent and the constraints
    compile on SQLite.  It is NOT an Alembic upgrade/downgrade test — that
    requires Postgres and is a deployment check.
    """
    async with engine.begin() as conn:
        tables = await conn.run_sync(lambda sync_conn: sync_conn.dialect.get_table_names(sync_conn))

    assert "persona_model_preferences" in tables
    assert "service_principals" in tables
    assert "service_principal_aliases" in tables
    assert "persona_model_policy_settings" in tables


@pytest.mark.asyncio
async def test_ac01_down_migration_drops_tables():
    """AC-01: Tables are droppable (simulates down-migration)."""
    e = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with e.begin() as conn:
        # Drop in reverse dependency order
        await conn.run_sync(Base.metadata.drop_all)
    async with e.begin() as conn:
        tables = await conn.run_sync(lambda sync_conn: sync_conn.dialect.get_table_names(sync_conn))
    assert "persona_model_preferences" not in tables
    assert "service_principals" not in tables
    await e.dispose()


# ── AC-02: Save and list ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ac02_save_and_list(client: AsyncClient):
    """AC-02: Save mappings, then list.  Patched validator enables writes."""
    with _patch_validator():
        resp = await client.put("/me/persona-models/developer", json={"model": "us.anthropic.claude-opus-4-6"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["source"] == "principal-mapping"
        assert data["saved_model_id"] == "us.anthropic.claude-opus-4-6"
        assert data["revision"] == 1

        resp = await client.put("/me/persona-models/reviewer", json={"model": "us.anthropic.claude-sonnet-4-6"})
        assert resp.status_code == 200

    resp = await client.get("/me/persona-models")
    assert resp.status_code == 200
    body = resp.json()
    entries = {e["persona_key"]: e for e in body["entries"]}

    assert entries["developer"]["source"] == "principal-mapping"
    assert entries["developer"]["saved_model_id"] == "us.anthropic.claude-opus-4-6"
    assert entries["developer"]["class_default_status"] is None
    assert entries["developer"]["effective_is_candidate"] is False
    assert entries["reviewer"]["source"] == "principal-mapping"
    assert entries["architect"]["source"] == "system-default"
    assert entries["architect"]["effective_model_id"] == "us.anthropic.claude-sonnet-4-6"
    assert entries["architect"]["compatibility_class"] == "claude-agent-sdk"
    assert entries["architect"]["harness_contract_revision"] == "0.3.220"
    assert entries["architect"]["effective_is_candidate"] is False
    assert entries["architect"]["class_default_status"] == "proven"
    assert entries["agent-codex-reviewer"]["compatibility_class"] == "codex-sdk"
    assert entries["agent-codex-reviewer"]["harness_contract_revision"] == "0.155.1"
    assert entries["agent-codex-reviewer"]["effective_model_id"] is None
    assert entries["agent-codex-reviewer"]["effective_is_candidate"] is False
    assert entries["agent-codex-reviewer"]["class_default_status"] is None


@pytest.mark.asyncio
async def test_class_candidate_is_visible_but_never_reported_as_proven(client: AsyncClient, engine):
    """AC-05e: default proof state comes from the class-keyed server record."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        setting = await session.get(PersonaModelPolicySetting, "claude-agent-sdk")
        assert setting is not None
        setting.active_default_model_id = None
        setting.candidate_default_model_id = "us.anthropic.claude-sonnet-4-6"
        await session.commit()

    resp = await client.get("/me/persona-models")
    assert resp.status_code == 200
    architect = next(entry for entry in resp.json()["entries"] if entry["persona_key"] == "architect")
    assert architect["compatibility_class"] == "claude-agent-sdk"
    assert architect["harness_contract_revision"] == "0.3.220"
    assert architect["effective_model_id"] == "us.anthropic.claude-sonnet-4-6"
    assert architect["effective_is_candidate"] is True
    assert architect["class_default_status"] == "candidate"
    assert architect["class_default_status"] != "proven"


def test_class_default_projection_preserves_proven_candidate_and_absent_states():
    """AC-05e: absence is not silently upgraded to candidate or proven."""
    from src.admin.persona_models.service import project_class_default

    assert project_class_default(None) == (None, None)
    assert project_class_default(
        PersonaModelPolicySetting(
            compatibility_class="claude-agent-sdk",
            candidate_default_model_id="candidate-model",
        )
    ) == ("candidate-model", "candidate")
    assert project_class_default(
        PersonaModelPolicySetting(
            compatibility_class="claude-agent-sdk",
            candidate_default_model_id="next-candidate",
            active_default_model_id="proven-model",
        )
    ) == ("proven-model", "proven")


# ── AC-03: Bad persona, invalid kind, unknown principal ──────────────────────


@pytest.mark.asyncio
async def test_ac03_unknown_persona_refused(client: AsyncClient):
    """AC-03: Unknown persona key is refused."""
    resp = await client.put("/me/persona-models/nonexistent-persona", json={"model": "any-model"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["reason"] == "unknown_persona"


@pytest.mark.asyncio
async def test_ac03_non_configurable_persona_refused(client: AsyncClient):
    """AC-03: Non-configurable persona is refused."""
    resp = await client.put("/me/persona-models/pt-superpower", json={"model": "any-model"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["reason"] == "persona_not_configurable"


@pytest.mark.asyncio
async def test_ac03_empty_model_refused(client: AsyncClient):
    """AC-03: Empty model string is refused."""
    resp = await client.put("/me/persona-models/developer", json={"model": "   "})
    assert resp.status_code == 422


# ── AC-04: Self endpoint ignores injected target ─────────────────────────────


@pytest.mark.asyncio
async def test_ac04_self_endpoint_ignores_injected_target(client: AsyncClient, engine):
    """AC-04: Injecting a principal_id in the body does not write to that principal.

    Proves by asserting the OTHER principal's row is unchanged.
    """
    with _patch_validator():
        resp = await client.put("/me/persona-models/developer", json={"model": "model-for-user-a"})
        assert resp.status_code == 200

        # Inject principal_id naming another user — should be ignored
        resp = await client.put(
            "/me/persona-models/reviewer",
            json={"model": "model-injected", "principal_id": TEST_OTHER_USER_ID},
        )

    # The other user must have NO preferences
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        other_prefs = await session.scalars(select(PersonaModelPreference).where(PersonaModelPreference.principal_id == TEST_OTHER_USER_ID))
        assert list(other_prefs) == [], "Other user's row was modified — AC-04 violated"


@pytest.mark.asyncio
async def test_ac04_no_principal_path_params(client: AsyncClient):
    """AC-04: No self route has a principal-naming path parameter."""
    from src.admin.persona_models.self_routes import router

    forbidden_names = {"user_id", "principal_id", "canonical_principal_id", "service_account_id", "agent_name", "target"}
    for route in router.routes:
        if hasattr(route, "path"):
            params = set(re.findall(r"\{(\w+)\}", route.path))
            unexpected = params & forbidden_names
            assert not unexpected, f"Route {route.path} has forbidden principal-naming parameter: {unexpected}"


# ── AC-05: Cross-tenant isolation ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ac05_cross_tenant_read_refused(client: AsyncClient, engine):
    """AC-05: Tenant A cannot read mappings owned by tenant B."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ServicePrincipal(
                canonical_service_principal_id="sp-org-b",
                org_id=TEST_ORG_B,
                display_name="Org B Worker",
                status="active",
                approved_by="admin-b",
            )
        )
        await session.commit()

    _set_context(client, _admin_context())
    resp = await client.get("/service-principals/sp-org-b/persona-models")
    assert resp.status_code == 422  # principal_not_found because wrong tenant


@pytest.mark.asyncio
async def test_ac05_cross_tenant_write_refused(client: AsyncClient, engine):
    """AC-05: Cross-tenant write creates no row and no audit entry in tenant B."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ServicePrincipal(
                canonical_service_principal_id="sp-org-b-write",
                org_id=TEST_ORG_B,
                display_name="Org B Worker Write",
                status="active",
                approved_by="admin-b",
            )
        )
        await session.commit()

    _set_context(client, _admin_context())
    resp = await client.put(
        "/service-principals/sp-org-b-write/persona-models/developer",
        json={"model": "us.anthropic.claude-sonnet-4-6"},
    )
    assert resp.status_code == 422

    # No audit row in tenant B
    async with factory() as session:
        audit_rows = await session.scalars(select(AuditLog).where(AuditLog.org_id == TEST_ORG_B))
        assert list(audit_rows) == [], "Audit row created in tenant B — AC-05 violated"


# ── AC-06: Optimistic concurrency ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_ac06_stale_revision_conflict(client: AsyncClient):
    """AC-06: Second save with stale revision is refused with 409."""
    with _patch_validator():
        resp1 = await client.put("/me/persona-models/developer", json={"model": "model-v1"})
        assert resp1.status_code == 200
        assert resp1.json()["revision"] == 1

        resp2 = await client.put("/me/persona-models/developer", json={"model": "model-v2", "expected_revision": 1})
        assert resp2.status_code == 200
        assert resp2.json()["revision"] == 2

        # Stale revision — should get 409
        resp3 = await client.put("/me/persona-models/developer", json={"model": "model-v3", "expected_revision": 1})
        assert resp3.status_code == 409
        conflict = resp3.json()
        assert conflict["current_revision"] == 2
        assert conflict["current_model_id"] == "model-v2"


@pytest.mark.asyncio
async def test_ac06_create_only_conflict(client: AsyncClient):
    """AC-06: Create-only save (no expected_revision) fails if row exists."""
    with _patch_validator():
        resp1 = await client.put("/me/persona-models/architect", json={"model": "model-a"})
        assert resp1.status_code == 200

        resp2 = await client.put("/me/persona-models/architect", json={"model": "model-b"})
        assert resp2.status_code == 409


@pytest.mark.asyncio
async def test_ac06_concurrent_create_yields_one_row_and_a_conflict(concurrent_engine):
    """AC-06: Two SIMULTANEOUS create-only saves — one wins, the other gets a conflict.

    This is the test AC-06 actually asks for, and the sequential tests above
    cannot stand in for it. The defect it pins is real and was reproduced on this
    branch: because `set_preference` read "does a row exist" and then INSERTed as
    two separate steps, both callers saw an empty table, both INSERTed, and the
    loser surfaced an unmapped ``IntegrityError`` — an HTTP 500 — instead of the
    orderly 409 a stale-revision update already returned. One run left **zero**
    rows stored, because the losing caller's rollback discarded the winner too.

    Two details are load-bearing, and both are easy to get wrong:

    1. **A separate engine with real per-session connections.** The module's
       ``engine`` fixture uses ``StaticPool`` over ``:memory:``, which hands every
       session the *same* connection — so two "concurrent" sessions are one
       transaction and the interleaving under test cannot occur. Verified while
       writing this: against the shared-connection engine the winner itself
       reported zero stored rows. A file-backed database is what makes the two
       writers genuinely independent.
    2. **``asyncio.gather``, not sequential awaits.** Sequential calls exercise the
       already-covered "row exists" branch, never the interleaving.
    """
    factory = async_sessionmaker(concurrent_engine, expire_on_commit=False)

    from src.admin.persona_models import service as svc

    async def save(model: str):
        async with factory() as session:
            row = await svc.set_preference(
                session,
                org_id=TEST_ORG_A,
                principal_kind="human",
                principal_source="self",
                principal_id=TEST_USER_ID,
                persona_key="developer",
                model=model,
                expected_revision=None,
                actor_id=TEST_USER_ID,
                actor_source="self",
            )
            await session.commit()
            return row.canonical_model_id

    with _patch_validator():
        results = await asyncio.gather(save("model-A"), save("model-B"), return_exceptions=True)

    winners = [r for r in results if isinstance(r, str)]
    conflicts = [r for r in results if isinstance(r, svc.PreferenceConflictError)]
    unexpected = [r for r in results if not isinstance(r, str | svc.PreferenceConflictError)]

    assert unexpected == [], f"A concurrent create raised something other than a conflict: {unexpected!r}"
    assert len(winners) == 1, f"Expected exactly one successful create, got {winners!r}"
    assert len(conflicts) == 1, f"Expected exactly one conflict, got {conflicts!r}"

    # The conflict must report the row that actually won, so the caller has a
    # revision to retry against rather than an opaque failure.
    assert conflicts[0].row.canonical_model_id == winners[0]
    assert conflicts[0].row.revision == 1

    # Exactly one row survives, and it is the winner's.
    async with factory() as session:
        rows = list(
            await session.scalars(
                select(PersonaModelPreference).where(
                    PersonaModelPreference.org_id == TEST_ORG_A,
                    PersonaModelPreference.principal_id == TEST_USER_ID,
                    PersonaModelPreference.persona_key == "developer",
                )
            )
        )
    assert len(rows) == 1, f"Expected exactly one stored row, found {len(rows)}"
    assert rows[0].canonical_model_id == winners[0]
    assert rows[0].revision == 1


@pytest.mark.asyncio
async def test_ac06_unrelated_integrity_error_is_not_reported_as_conflict(concurrent_engine):
    """A non-uniqueness integrity failure must NOT be disguised as a 409.

    The race fix narrows on the scope-uniqueness violation specifically. Without
    that narrowing, any integrity defect — a CHECK refusal, a missing FK — would
    be reported to the caller as "someone else got there first", which is both
    wrong and would hide real bugs. Here a bad ``principal_kind`` violates
    ``ck_persona_pref_principal_kind``; it must propagate, not become a conflict.
    """
    factory = async_sessionmaker(concurrent_engine, expire_on_commit=False)

    from src.admin.persona_models import service as svc

    with _patch_validator():
        async with factory() as session:
            with pytest.raises(IntegrityError):
                await svc.set_preference(
                    session,
                    org_id=TEST_ORG_A,
                    principal_kind="not_a_valid_kind",
                    principal_source="self",
                    principal_id=TEST_USER_ID,
                    persona_key="developer",
                    model="model-x",
                    expected_revision=None,
                    actor_id=TEST_USER_ID,
                    actor_source="self",
                )


@pytest.mark.asyncio
async def test_ac06_kind_conflict(engine):
    """AC-06: One canonical ID cannot hold preferences as two different kinds.

    Tested at the service layer because this invariant cannot be observed
    through the HTTP route (routes derive kind from the token).
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            PersonaModelPreference(
                id=new_uuid(),
                org_id=TEST_ORG_A,
                principal_kind="human",
                principal_source="self",
                principal_id="dual-kind-test-id",
                persona_key="developer",
                canonical_model_id="model-x",
                revision=1,
                updated_by="actor-1",
                updated_by_source="self",
            )
        )
        await session.commit()

    from src.admin.persona_models.service import PreferenceRejectedError, set_preference

    with _patch_validator():
        async with factory() as session:
            with pytest.raises(PreferenceRejectedError, match="already has preferences as"):
                await set_preference(
                    session,
                    org_id=TEST_ORG_A,
                    principal_kind="service_account",
                    principal_source="agent_registry",
                    principal_id="dual-kind-test-id",
                    persona_key="reviewer",
                    model="model-y",
                    expected_revision=None,
                    actor_id="actor-2",
                    actor_source="agent_registry",
                )


# ── AC-07: Fail-closed validation ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_ac07_fail_closed_rejects_all_writes(client: AsyncClient):
    """AC-07: The integrated validator refuses unproven writes.

    This is the production code path — no patches.  No row is stored.
    """
    resp = await client.put("/me/persona-models/developer", json={"model": "global.anthropic.claude-opus-4-6-v1"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["reason"] == "probing_disabled"
    assert "evidence" in detail["message"].lower()


@pytest.mark.asyncio
async def test_pmm03_save_validation_receives_resolved_destination(client: AsyncClient):
    """The PMM-02 save path must validate against the caller's destination."""
    destination = AsyncMock(return_value=("111111111111", "eu-west-1"))
    validator = AsyncMock(return_value="global.anthropic.claude-opus-4-6-v1")

    with (
        patch(
            "src.admin.persona_models.catalogue_routes.resolve_effective_destination",
            destination,
        ),
        patch(
            "src.admin.persona_models.service.validate_model_for_persona",
            validator,
        ),
    ):
        resp = await client.put(
            "/me/persona-models/developer",
            json={"model": "global.anthropic.claude-opus-4-6-v1"},
        )

    assert resp.status_code == 200
    assert validator.await_args.kwargs["account_id"] == "111111111111"
    assert validator.await_args.kwargs["region"] == "eu-west-1"
    assert validator.await_args.kwargs["principal_status"] is None


@pytest.mark.asyncio
async def test_ac07_no_row_stored_on_rejection(client: AsyncClient, engine):
    """AC-07: A stored-but-unusable row is a failure of this AC."""
    resp = await client.put("/me/persona-models/developer", json={"model": "any-model-string"})
    assert resp.status_code == 422

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        rows = await session.scalars(select(PersonaModelPreference))
        assert list(rows) == [], "Row stored despite validation refusal — AC-07 violated"


# ── AC-08: Reset and audit ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ac08_reset_and_audit_survives(client: AsyncClient, engine):
    """AC-08: Reset removes row, default becomes effective, audit survives."""
    with _patch_validator():
        resp = await client.put("/me/persona-models/developer", json={"model": "us.anthropic.claude-opus-4-6"})
        assert resp.status_code == 200

    # Reset
    resp = await client.request("DELETE", "/me/persona-models/developer", json={"expected_revision": 1})
    assert resp.status_code == 200
    data = resp.json()
    assert data["removed"] is True
    assert data["source"] == "system-default"
    assert data["effective_model_id"] == "us.anthropic.claude-sonnet-4-6"
    assert data["compatibility_class"] == "claude-agent-sdk"
    assert data["harness_contract_revision"] == "0.3.220"
    assert data["effective_is_candidate"] is False
    assert data["class_default_status"] == "proven"

    # Audit trail survives
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        audit_rows = await session.scalars(
            select(AuditLog).where(
                AuditLog.org_id == TEST_ORG_A,
                AuditLog.event_type.in_(["persona_model_self_set", "persona_model_self_reset"]),
            )
        )
        events = {row.event_type for row in audit_rows}
        assert "persona_model_self_set" in events, "Save audit row missing"
        assert "persona_model_self_reset" in events, "Reset audit row missing"


@pytest.mark.asyncio
async def test_reset_response_names_candidate_class_default(client: AsyncClient, engine):
    """AC-05e: reset cannot launder a class candidate into proven status."""
    with _patch_validator():
        saved = await client.put(
            "/me/persona-models/developer",
            json={"model": "us.anthropic.claude-opus-4-6"},
        )
    assert saved.status_code == 200

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        setting = await session.get(PersonaModelPolicySetting, "claude-agent-sdk")
        assert setting is not None
        setting.active_default_model_id = None
        setting.candidate_default_model_id = "us.anthropic.claude-sonnet-4-6"
        await session.commit()

    response = await client.request(
        "DELETE",
        "/me/persona-models/developer",
        json={"expected_revision": 1},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["removed"] is True
    assert data["source"] == "system-default"
    assert data["effective_model_id"] == "us.anthropic.claude-sonnet-4-6"
    assert data["compatibility_class"] == "claude-agent-sdk"
    assert data["harness_contract_revision"] == "0.3.220"
    assert data["effective_is_candidate"] is True
    assert data["class_default_status"] == "candidate"


@pytest.mark.asyncio
async def test_ac08_audit_records_the_actual_model_transition(client: AsyncClient, engine):
    """AC-08: The audit must record *what changed*, with the right values.

    An earlier version of this test asserted only that keys were ``in details``,
    which passes no matter how wrong every value is — and it did pass while
    ``before_model`` was absent entirely, so nothing recorded what a change
    replaced. §5.5 requires before and after, because "who set what" is only half
    the question an audit answers; the other half is what it displaced.

    Two writes, so the second has a real predecessor to name.
    """
    with _patch_validator():
        first = await client.put("/me/persona-models/developer", json={"model": "model-one"})
        assert first.status_code == 200
        second = await client.put("/me/persona-models/developer", json={"model": "model-two", "expected_revision": 1})
        assert second.status_code == 200, second.text

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        rows = list(await session.scalars(select(AuditLog).where(AuditLog.event_type == "persona_model_self_set").order_by(AuditLog.created_at)))
    assert len(rows) == 2, f"Expected one audit row per write, got {len(rows)}"

    create, update = (r.details for r in rows)

    # First write created the row: nothing was displaced.
    assert create["before_model"] is None, "A first-time save must record before_model=None, not omit it"
    assert create["after_model"] == "model-one"
    assert create["revision"] == 1

    # Second write displaced the first: the trail must name both ends.
    assert update["before_model"] == "model-one", "The audit does not say what this change replaced"
    assert update["after_model"] == "model-two"
    assert update["revision"] == 2, "Revision in the audit must be the post-write revision"

    # Subject identity and provenance, per §5.5: the key, plus how it authenticated.
    for details in (create, update):
        assert details["principal_kind"] == "human"
        assert details["principal_id"] == TEST_USER_ID
        assert details["subject_key"] == TEST_USER_ID
        assert details["persona_key"] == "developer"
        assert details["actor_kind"] == "human"
        assert details["principal_source"] == "self"
        assert details["updated_by_source"] == "self"

    # actor_id must be the canonical users.id, never a raw subject (§5.5).
    assert all(r.actor_id == TEST_USER_ID for r in rows)
    assert all(r.actor_id != TEST_USER_COGNITO_SUB for r in rows), "Raw Cognito sub written to actor_id"


@pytest.mark.asyncio
async def test_ac08_reset_audit_records_what_was_removed(client: AsyncClient, engine):
    """AC-08: A reset must name the model it removed and its provenance.

    A reset deletes the row, so the audit entry is the *only* remaining record
    that the preference ever existed. If it omits ``before_model`` the history is
    unreconstructible; ``after_model`` is explicitly null rather than absent so a
    reader can tell "reverted to default" from "not recorded".
    """
    with _patch_validator():
        await client.put("/me/persona-models/developer", json={"model": "model-to-be-removed"})

    resp = await client.request("DELETE", "/me/persona-models/developer", json={"expected_revision": 1})
    assert resp.status_code == 200

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        row = await session.scalar(select(AuditLog).where(AuditLog.event_type == "persona_model_self_reset"))
        assert row is not None, "Reset audit row missing"
        remaining = list(await session.scalars(select(PersonaModelPreference)))

    assert remaining == [], "Reset left the preference row behind"
    details = row.details
    assert details["before_model"] == "model-to-be-removed", "Reset audit does not record what it removed"
    assert "after_model" in details and details["after_model"] is None
    assert details["principal_source"] == "self", "Reset audit dropped the provenance the set event carries"
    assert details["updated_by_source"] == "self"
    assert row.actor_id == TEST_USER_ID


@pytest.mark.asyncio
async def test_reset_requires_and_atomically_enforces_observed_revision(client: AsyncClient):
    """A destructive reset cannot race a newer mapping update."""
    with _patch_validator():
        created = await client.put("/me/persona-models/developer", json={"model": "model-one"})
        assert created.status_code == 200
        updated = await client.put(
            "/me/persona-models/developer",
            json={"model": "model-two", "expected_revision": 1},
        )
        assert updated.status_code == 200
        assert updated.json()["revision"] == 2

    blind = await client.delete("/me/persona-models/developer")
    assert blind.status_code == 409
    assert blind.json()["tenant_id"] == TEST_ORG_A
    assert blind.json()["current_revision"] == 2

    stale = await client.request(
        "DELETE",
        "/me/persona-models/developer",
        json={"expected_revision": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["current_revision"] == 2
    assert stale.json()["current_model_id"] == "model-two"

    still_current = await client.get("/me/persona-models/explain/developer")
    assert still_current.status_code == 200
    assert still_current.json()["saved_model_id"] == "model-two"
    assert still_current.json()["revision"] == 2

    reset = await client.request(
        "DELETE",
        "/me/persona-models/developer",
        json={"expected_revision": 2},
    )
    assert reset.status_code == 200
    assert reset.json()["tenant_id"] == TEST_ORG_A
    assert reset.json()["saved_model_id"] is None
    assert reset.json()["removed"] is True


@pytest.mark.asyncio
async def test_concurrent_reset_reports_only_the_atomic_delete_winner(concurrent_engine):
    """A reset that loses after its read returns False instead of claiming a delete."""
    factory = async_sessionmaker(concurrent_engine, expire_on_commit=False)
    from src.admin.persona_models import service as svc

    async with factory() as session:
        session.add(
            PersonaModelPreference(
                id=new_uuid(),
                org_id=TEST_ORG_A,
                principal_kind="human",
                principal_source="self",
                principal_id=TEST_USER_ID,
                persona_key="developer",
                canonical_model_id="model-one",
                revision=1,
                updated_by=TEST_USER_ID,
                updated_by_source="self",
            )
        )
        await session.commit()

    async with factory() as losing_session, factory() as winning_session:
        stale_row = await svc.get_preference(
            losing_session,
            org_id=TEST_ORG_A,
            principal_kind="human",
            principal_id=TEST_USER_ID,
            persona_key="developer",
        )
        assert stale_row is not None
        losing_session.expunge(stale_row)
        await losing_session.rollback()

        won = await svc.reset_preference(
            winning_session,
            org_id=TEST_ORG_A,
            principal_kind="human",
            principal_id=TEST_USER_ID,
            persona_key="developer",
            expected_revision=1,
        )
        await winning_session.commit()

        # Deterministically reproduce the interleaving: the loser already read
        # revision 1, but its atomic DELETE runs only after the winner committed.
        with patch.object(svc, "get_preference", AsyncMock(return_value=stale_row)):
            lost = await svc.reset_preference(
                losing_session,
                org_id=TEST_ORG_A,
                principal_kind="human",
                principal_id=TEST_USER_ID,
                persona_key="developer",
                expected_revision=1,
            )
        await losing_session.commit()

    assert won is True
    assert lost is False

    async with factory() as session:
        remaining = list(await session.scalars(select(PersonaModelPreference)))
    assert remaining == []


@pytest.mark.asyncio
async def test_preference_responses_name_the_authenticated_active_tenant(client: AsyncClient):
    """CLI-visible read shapes carry server-derived tenant context."""
    listed = await client.get("/me/persona-models")
    explained = await client.get("/me/persona-models/explain/developer")
    catalogued = await client.get("/me/persona-models/catalog", params={"persona_key": "developer"})

    assert listed.status_code == explained.status_code == catalogued.status_code == 200
    assert listed.json()["tenant_id"] == TEST_ORG_A
    assert explained.json()["tenant_id"] == TEST_ORG_A
    assert catalogued.json()["tenant_id"] == TEST_ORG_A


@pytest.mark.asyncio
async def test_refused_self_write_is_audited_without_a_raw_subject(client: AsyncClient, engine):
    """A refused write is audited, and never launders a raw subject into actor_id.

    An unregistered service caller is refused before any canonical ID exists, so
    the only identifier to hand is the raw per-auth-path subject. §5.5 reserves
    ``actor_id`` for canonical IDs exclusively — a column mixing the two cannot
    answer "everything this principal did". The subject is still captured, under a
    key that says what it is, so the attempt is investigable.
    """
    _set_context(client, _service_context(canonical_id="", alias_source=""))

    resp = await client.put("/me/persona-models/developer", json={"model": "some-model"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "unregistered_service_principal"

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        row = await session.scalar(select(AuditLog).where(AuditLog.event_type == "persona_model_self_set_rejected"))
        assert row is not None, "A refused write left no audit record"

    assert row.actor_id is None, "Raw subject written to actor_id, which §5.5 reserves for canonical IDs"
    assert row.details["unresolved_subject"] == TEST_SP_ALIAS_ID
    assert row.details["reason"] == "unregistered_service_principal"
    assert row.details["persona_key"] == "developer"


# ── AC-09: Service account self-management ───────────────────────────────────


@pytest.mark.asyncio
async def test_ac09_service_account_self_management(client: AsyncClient):
    """AC-09: Service account manages its own mapping through the self endpoint."""
    _set_context(client, _service_context())

    with _patch_validator():
        resp = await client.put("/me/persona-models/developer", json={"model": "us.anthropic.claude-sonnet-4-6"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "principal-mapping"
        assert data["saved_model_id"] == "us.anthropic.claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_ac09_service_account_cannot_admin(client: AsyncClient):
    """AC-09: Service account cannot use the admin endpoint."""
    _set_context(client, _service_context())

    resp = await client.put(
        f"/service-principals/{TEST_SP_CANONICAL_ID}/persona-models/developer",
        json={"model": "us.anthropic.claude-opus-4-6"},
    )
    assert resp.status_code == 403


# ── AC-10: Administration ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ac10_admin_in_tenant_succeeds(client: AsyncClient, engine):
    """AC-10: Org admin manages service principal in their tenant."""
    _set_context(client, _admin_context())

    resp = await client.get(f"/service-principals/{TEST_SP_CANONICAL_ID}/persona-models")
    assert resp.status_code == 200

    destination = AsyncMock(return_value=("222222222222", "us-west-2"))
    validator = AsyncMock(return_value="us.anthropic.claude-opus-4-6")
    with (
        patch(
            "src.admin.persona_models.catalogue_routes.resolve_effective_destination",
            destination,
        ),
        patch(
            "src.admin.persona_models.service.validate_model_for_persona",
            validator,
        ),
    ):
        resp = await client.put(
            f"/service-principals/{TEST_SP_CANONICAL_ID}/persona-models/developer",
            json={"model": "us.anthropic.claude-opus-4-6"},
        )
        assert resp.status_code == 200
        assert resp.json()["tenant_id"] == TEST_ORG_A

    blind_reset = await client.delete(f"/service-principals/{TEST_SP_CANONICAL_ID}/persona-models/developer")
    assert blind_reset.status_code == 409
    reset = await client.request(
        "DELETE",
        f"/service-principals/{TEST_SP_CANONICAL_ID}/persona-models/developer",
        json={"expected_revision": 1},
    )
    assert reset.status_code == 200
    assert reset.json()["tenant_id"] == TEST_ORG_A
    assert reset.json()["removed"] is True

    repeated_reset = await client.delete(f"/service-principals/{TEST_SP_CANONICAL_ID}/persona-models/developer")
    assert repeated_reset.status_code == 200
    assert repeated_reset.json()["removed"] is False

    routing_context = destination.await_args.args[1]
    assert routing_context.canonical_service_principal_id == TEST_SP_CANONICAL_ID
    assert routing_context.account_type == "service"
    assert routing_context.team_id == ""
    assert destination.await_args.kwargs["routing_user_id"] == ""
    assert validator.await_args.kwargs["account_id"] == "222222222222"
    assert validator.await_args.kwargs["region"] == "us-west-2"
    assert validator.await_args.kwargs["principal_status"] == "active"

    # Audit is distinct from self-changes
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        audit = await session.scalars(select(AuditLog).where(AuditLog.event_type == "persona_model_admin_set"))
        rows = list(audit)
        assert len(rows) >= 1
        details = rows[0].details
        assert details["actor_kind"] == "human_admin"
        assert details["subject_key"] == TEST_SP_CANONICAL_ID
        assert "before_model" in details
        assert "after_model" in details


@pytest.mark.asyncio
async def test_ac10_managed_catalogue_uses_target_service_principal(client: AsyncClient):
    """Managed catalogue and save evaluate the same target, never the admin."""
    _set_context(client, _admin_context())

    destination = AsyncMock(return_value=("222222222222", "us-west-2"))
    restriction_policy = AsyncMock(return_value=([["*sonnet*"]], None))
    catalogue = AsyncMock(return_value=[])
    with (
        patch(
            "src.admin.persona_models.catalogue_routes.resolve_effective_destination",
            destination,
        ),
        patch(
            "src.admin.persona_models.catalogue_service.build_model_catalogue",
            catalogue,
        ),
        patch(
            "src.admin.persona_models.catalogue_routes.resolve_managed_service_restriction_policy",
            restriction_policy,
        ),
    ):
        resp = await client.get(
            f"/service-principals/{TEST_SP_CANONICAL_ID}/persona-models/catalog",
            params={"persona_key": "developer"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "tenant_id": TEST_ORG_A,
        "persona_key": "developer",
        "compatibility_class": "claude-agent-sdk",
        "models": [],
    }
    routing_context = destination.await_args.args[1]
    assert routing_context.user_id == TEST_SP_CANONICAL_ID
    assert routing_context.canonical_service_principal_id == TEST_SP_CANONICAL_ID
    assert routing_context.account_type == "service"
    assert routing_context.team_id == ""
    assert routing_context.department_id == ""
    assert destination.await_args.kwargs["routing_user_id"] == ""
    assert catalogue.await_args.kwargs == {
        "persona_key": "developer",
        "account_id": "222222222222",
        "region": "us-west-2",
        "principal_kind": "service_account",
        "canonical_principal_id": TEST_SP_CANONICAL_ID,
        "principal_status": "active",
        "service_restriction_pattern_sets": [["*sonnet*"]],
        "policy_unavailable_reason": None,
        "tenant_allowed_patterns": None,
    }
    restriction_policy.assert_awaited_once()
    assert restriction_policy.await_args.kwargs["org_id"] == TEST_ORG_A
    assert restriction_policy.await_args.kwargs["canonical_service_principal_id"] == TEST_SP_CANONICAL_ID


@pytest.mark.asyncio
async def test_ac10_managed_catalogue_refuses_cross_tenant_target(client: AsyncClient, engine):
    """The projection cannot be used to inspect another tenant's principal."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ServicePrincipal(
                canonical_service_principal_id="sp-b-catalogue-test",
                org_id=TEST_ORG_B,
                display_name="Org B Catalogue Target",
                status="active",
                approved_by="admin-b",
            )
        )
        await session.commit()

    _set_context(client, _admin_context())
    resp = await client.get(
        "/service-principals/sp-b-catalogue-test/persona-models/catalog",
        params={"persona_key": "developer"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "principal_not_found"


@pytest.mark.asyncio
async def test_ac10_managed_catalogue_requires_human_org_admin(client: AsyncClient):
    """A service caller cannot inspect another principal's model projection."""
    _set_context(client, _service_context())
    resp = await client.get(
        f"/service-principals/{TEST_SP_CANONICAL_ID}/persona-models/catalog",
        params={"persona_key": "developer"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_ac10_admin_cross_tenant_refused(client: AsyncClient, engine):
    """AC-10: Admin in org A cannot administer service principals in org B."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ServicePrincipal(
                canonical_service_principal_id="sp-b-admin-test",
                org_id=TEST_ORG_B,
                display_name="Org B Worker Admin Test",
                status="active",
                approved_by="admin-b",
            )
        )
        await session.commit()

    _set_context(client, _admin_context())
    resp = await client.put(
        "/service-principals/sp-b-admin-test/persona-models/developer",
        json={"model": "us.anthropic.claude-sonnet-4-6"},
    )
    assert resp.status_code == 422  # principal_not_found (cross-tenant)

    # No row and no audit entry in the VICTIM tenant (AC-05/§5.6 wording), and no
    # preference written anywhere.
    async with factory() as session:
        assert list(await session.scalars(select(AuditLog).where(AuditLog.org_id == TEST_ORG_B))) == []
        assert list(await session.scalars(select(PersonaModelPreference))) == []


@pytest.mark.asyncio
async def test_admin_cross_tenant_attempt_is_audited_in_callers_tenant(client: AsyncClient, engine):
    """A cross-tenant administrative attempt must be audited — in the caller's tenant.

    This is the most suspicious act the administration surface allows: naming a
    principal that is not in your tenant is how a cross-tenant write would be
    attempted. It previously returned 422 and recorded **nothing anywhere**, so the
    attempt left no trace at all. §5.6 puts the record in the caller's tenant,
    where the suspicious act happened — never the target's, which would write into
    the victim's trail while still refusing.

    The sibling test above asserts the other half: tenant B stays clean.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ServicePrincipal(
                canonical_service_principal_id="sp-b-audit-test",
                org_id=TEST_ORG_B,
                display_name="Org B Worker",
                status="active",
                approved_by="admin-b",
            )
        )
        await session.commit()

    _set_context(client, _admin_context())
    for method, expected_event in (("put", "persona_model_admin_set_rejected"), ("delete", "persona_model_admin_reset_rejected")):
        path = "/service-principals/sp-b-audit-test/persona-models/developer"
        resp = await (client.put(path, json={"model": "some-model"}) if method == "put" else client.delete(path))
        assert resp.status_code == 422, f"{method} should be refused"

        async with factory() as session:
            row = await session.scalar(select(AuditLog).where(AuditLog.event_type == expected_event))
        assert row is not None, f"Cross-tenant {method} left no audit record"
        assert row.org_id == TEST_ORG_A, "Refusal must be audited in the caller's tenant, not the target's"
        assert row.actor_id == TEST_ADMIN_USER_ID, "actor_id must be the admin's canonical users.id"
        assert row.details["target_principal_id"] == "sp-b-audit-test"
        assert row.details["actor_kind"] == "human_admin"
        assert row.details["reason"]

    async with factory() as session:
        assert list(await session.scalars(select(AuditLog).where(AuditLog.org_id == TEST_ORG_B))) == []


# ── AC-11: Platform default not writable via self API ────────────────────────


@pytest.mark.asyncio
async def test_ac11_platform_default_not_writable_via_self(client: AsyncClient, engine):
    """AC-11: No request to the self surface can alter the platform default.

    An earlier version of this test asserted that no route *path* contained the
    substrings "policy" or "settings". That proves nothing: it passes for a route
    named ``/me/persona-models/{persona_key}`` that writes
    ``persona_model_policy_settings`` on every call, and it would keep passing
    while the platform default was being overwritten. What AC-11 requires is a
    property of behaviour, so this asserts the behaviour — attempt the mutations an
    attacker would actually try, then prove the row is byte-for-byte unchanged.

    ``persona_key`` is the only free parameter on the write path, so these probes
    aim it at the policy table: the literal class name, the sentinel used for
    platform-scoped audit rows, path traversal, and "default" itself.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)

    def _snapshot(row: PersonaModelPolicySetting) -> tuple:
        return (
            row.compatibility_class,
            row.candidate_default_model_id,
            row.active_default_model_id,
            row.revision,
            row.posture_revision,
            row.enforcement_posture,
            row.updated_by,
        )

    async with factory() as session:
        rows = list(await session.scalars(select(PersonaModelPolicySetting)))
        before = sorted(_snapshot(r) for r in rows)
    assert before, "Fixture must seed a platform default for this test to mean anything"

    probes = [
        "default",
        "claude-agent-sdk",
        "codex-sdk",
        "__platform__",
        "../claude-agent-sdk",
        "persona_model_policy_settings",
    ]

    # Patch the validator so a refusal cannot be what protects the row — we want
    # the write path to run as far as it can and still not touch the policy table.
    with _patch_validator():
        for probe in probes:
            resp = await client.put(f"/me/persona-models/{probe}", json={"model": "platform-default-override-attempt"})
            assert resp.status_code in (200, 404, 422), f"Unexpected status {resp.status_code} for persona_key={probe!r}"
            revision = resp.json().get("revision") if resp.status_code == 200 else None
            resp = await client.request(
                "DELETE",
                f"/me/persona-models/{probe}",
                json={"expected_revision": revision},
            )
            assert resp.status_code in (200, 404, 422), f"Unexpected status {resp.status_code} for persona_key={probe!r}"

    async with factory() as session:
        rows = list(await session.scalars(select(PersonaModelPolicySetting)))
        after = sorted(_snapshot(r) for r in rows)

    assert after == before, "A request to the self surface changed the platform default — AC-11 violated"
    assert len(rows) == len(before), "The self surface created or deleted a policy-settings row"


@pytest.mark.asyncio
async def test_persona_catalogue_is_enforced_by_the_store_not_the_validator(client: AsyncClient, engine):
    """An unknown or non-configurable persona key must be refused with the seam replaced.

    ``validate_model_for_persona`` is a temporary fail-closed seam that PMM-03
    (#5420) replaces, and its contract is to validate the **model**. The persona
    catalogue check originally lived only inside it, so a replacement honouring
    that contract exactly would silently drop the check. Verified before the fix:
    with a model-only validator, ``PUT /me/persona-models/default`` stored
    ``('default', 'm')`` and then raised out of ``build_explain`` as a 500.

    Which personas exist is an invariant of the store, so this drives the store
    through a replaced seam — the configuration PMM-03 will actually create.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)

    with _patch_validator():
        for bad_key, expected_reason in (
            ("default", "unknown_persona"),
            ("no-such-persona", "unknown_persona"),
            ("__platform__", "unknown_persona"),
        ):
            resp = await client.put(f"/me/persona-models/{bad_key}", json={"model": "some-model"})
            assert resp.status_code == 422, f"persona_key={bad_key!r} was not refused (got {resp.status_code})"
            assert resp.json()["detail"]["reason"] == expected_reason

            resp = await client.delete(f"/me/persona-models/{bad_key}")
            assert resp.status_code == 422, f"DELETE persona_key={bad_key!r} was not refused"

        async with factory() as session:
            rows = list(await session.scalars(select(PersonaModelPreference)))
        assert rows == [], f"Rows stored for personas absent from the catalogue: {[r.persona_key for r in rows]}"


@pytest.mark.asyncio
async def test_ac11_put_default_returns_404_or_422(client: AsyncClient):
    """AC-11: Attempting to PUT the platform default through self returns an error.

    'default' is not a known persona key, so the validator returns 422.
    """
    resp = await client.put("/me/persona-models/default", json={"model": "override-attempt"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["reason"] == "unknown_persona"


# ── Manageable service principals restricted to human admin ──────────────────


@pytest.mark.asyncio
async def test_manageable_principals_human_admin_only(client: AsyncClient):
    """Service callers cannot enumerate manageable-service-principals."""
    _set_context(client, _service_context())
    resp = await client.get("/me/persona-models/manageable-service-principals")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_manageable_principals_succeeds_for_admin(client: AsyncClient):
    """Human org-admin can enumerate manageable service principals."""
    _set_context(client, _admin_context())
    resp = await client.get("/me/persona-models/manageable-service-principals")
    assert resp.status_code == 200
    body = resp.json()
    principals = body["principals"]
    assert len(principals) >= 1
    sp = next(p for p in principals if p["canonical_service_principal_id"] == TEST_SP_CANONICAL_ID)
    assert "canonical_principal_id" not in sp
    assert sp["display_name"] == "Test Worker"


# ── Explain and list read endpoints ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_explain_not_configured(client: AsyncClient):
    """Explain returns system-default when no mapping exists."""
    resp = await client.get("/me/persona-models/explain/developer")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "system-default"
    assert data["effective_model_id"] == "us.anthropic.claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_explain_unknown_persona(client: AsyncClient):
    """Explain rejects unknown persona."""
    resp = await client.get("/me/persona-models/explain/nonexistent")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_reset_idempotent(client: AsyncClient):
    """Reset with nothing stored is idempotent."""
    resp = await client.delete("/me/persona-models/developer")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "system-default"
    assert data["removed"] is False


# ── Service-principal lifecycle (Gate 2) ────────────────────────────────────


@pytest.mark.asyncio
async def test_register_service_principal_happy_path(client: AsyncClient, engine):
    """Register a new service principal and its first alias. Audit recorded."""
    _set_context(client, _admin_context())

    resp = await client.post(
        "/service-principals/register",
        json={"display_name": "New Worker", "alias_source": "agent_registry", "alias_id": "new-agent-42"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["display_name"] == "New Worker"
    assert body["alias_source"] == "agent_registry"
    assert body["alias_id"] == "new-agent-42"
    assert body["status"] == "active"
    new_canonical = body["canonical_service_principal_id"]
    assert "canonical_principal_id" not in body
    assert new_canonical  # non-empty

    # Verify audit record
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        rows = list(await session.scalars(select(AuditLog).where(AuditLog.event_type == "service_principal_registered")))
        assert len(rows) >= 1
        details = rows[-1].details
        assert details["canonical_principal_id"] == new_canonical
        assert details["actor_kind"] == "human_admin"


@pytest.mark.asyncio
async def test_register_duplicate_active_alias_refused(client: AsyncClient):
    """Registering an alias that is already active is refused."""
    _set_context(client, _admin_context())

    resp = await client.post(
        "/service-principals/register",
        json={"display_name": "Dup Worker", "alias_source": "agent_registry", "alias_id": TEST_SP_ALIAS_ID},
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "alias_already_active"


@pytest.mark.asyncio
async def test_register_service_principal_service_caller_forbidden(client: AsyncClient):
    """Service callers cannot register service principals."""
    _set_context(client, _service_context())

    resp = await client.post(
        "/service-principals/register",
        json={"display_name": "Sneaky", "alias_source": "agent_registry", "alias_id": "sneaky-1"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_link_alias_happy_path(client: AsyncClient, engine):
    """Link an additional alias to an existing principal."""
    _set_context(client, _admin_context())

    resp = await client.post(
        f"/service-principals/{TEST_SP_CANONICAL_ID}/aliases",
        json={"alias_source": "sa_registration", "alias_id": "extra-sa-77"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["alias_source"] == "sa_registration"
    assert body["alias_id"] == "extra-sa-77"
    assert body["canonical_service_principal_id"] == TEST_SP_CANONICAL_ID
    assert body["is_active"] is True

    # Verify audit record
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        rows = list(await session.scalars(select(AuditLog).where(AuditLog.event_type == "service_principal_alias_linked")))
        assert len(rows) >= 1
        details = rows[-1].details
        assert details["canonical_principal_id"] == TEST_SP_CANONICAL_ID
        assert details["alias_id"] == "extra-sa-77"


@pytest.mark.asyncio
async def test_link_alias_unknown_principal_refused(client: AsyncClient):
    """Linking an alias to a non-existent principal is refused."""
    _set_context(client, _admin_context())

    resp = await client.post(
        "/service-principals/sp-does-not-exist/aliases",
        json={"alias_source": "agent_registry", "alias_id": "orphan-1"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["reason"] == "principal_not_found"


@pytest.mark.asyncio
async def test_revoke_alias_happy_path(client: AsyncClient, engine):
    """Revoke an active alias. Verify it becomes inactive and audit is recorded."""
    _set_context(client, _admin_context())

    # First, register a fresh principal so we can revoke without breaking other tests
    resp = await client.post(
        "/service-principals/register",
        json={"display_name": "Revoke Target", "alias_source": "sa_registration", "alias_id": "revoke-me-101"},
    )
    assert resp.status_code == 200
    new_canonical = resp.json()["canonical_service_principal_id"]

    # Get the alias row ID
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        alias = await session.scalar(
            select(ServicePrincipalAlias).where(
                ServicePrincipalAlias.canonical_service_principal_id == new_canonical,
                ServicePrincipalAlias.alias_id == "revoke-me-101",
            )
        )
        alias_row_id = alias.id

    # Revoke
    resp = await client.delete(f"/service-principals/{new_canonical}/aliases/{alias_row_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_active"] is False
    assert body["alias_id"] == "revoke-me-101"

    # Verify audit record
    async with factory() as session:
        rows = list(await session.scalars(select(AuditLog).where(AuditLog.event_type == "service_principal_alias_revoked")))
        assert len(rows) >= 1
        details = rows[-1].details
        assert details["canonical_principal_id"] == new_canonical
        assert details["alias_row_id"] == alias_row_id


@pytest.mark.asyncio
async def test_revoke_alias_not_found(client: AsyncClient):
    """Revoking a non-existent alias row returns 422."""
    _set_context(client, _admin_context())

    resp = await client.delete(f"/service-principals/{TEST_SP_CANONICAL_ID}/aliases/no-such-row-id")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["reason"] == "alias_not_found"


@pytest.mark.asyncio
async def test_reregister_after_revoke_creates_new_principal(client: AsyncClient, engine):
    """After revoking an alias, re-registering it creates a new canonical principal.

    Revoked aliases cannot be reactivated — this is a design invariant.
    """
    _set_context(client, _admin_context())

    # Register
    resp = await client.post(
        "/service-principals/register",
        json={"display_name": "Re-reg Target", "alias_source": "agent_registry", "alias_id": "rereg-agent-999"},
    )
    assert resp.status_code == 200
    first_canonical = resp.json()["canonical_service_principal_id"]

    # Get alias row ID and revoke
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        alias = await session.scalar(
            select(ServicePrincipalAlias).where(
                ServicePrincipalAlias.canonical_service_principal_id == first_canonical,
                ServicePrincipalAlias.alias_id == "rereg-agent-999",
            )
        )
        alias_row_id = alias.id

    resp = await client.delete(f"/service-principals/{first_canonical}/aliases/{alias_row_id}")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    # Re-register the same alias_id — should create a NEW canonical principal
    resp = await client.post(
        "/service-principals/register",
        json={"display_name": "Re-reg Target v2", "alias_source": "agent_registry", "alias_id": "rereg-agent-999"},
    )
    assert resp.status_code == 200
    second_canonical = resp.json()["canonical_service_principal_id"]

    # The two canonical IDs must differ
    assert second_canonical != first_canonical, "Re-registration must create a new canonical principal"


@pytest.mark.asyncio
async def test_non_admin_human_cannot_register(client: AsyncClient):
    """A non-admin human cannot register service principals."""
    _set_context(client, _human_context())  # non-admin

    resp = await client.post(
        "/service-principals/register",
        json={"display_name": "Nope", "alias_source": "agent_registry", "alias_id": "nope-1"},
    )
    # check_permission raises 403 for non-admin
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_non_admin_human_cannot_list_manageable_principals(client: AsyncClient):
    """A non-admin human cannot enumerate manageable service principals."""
    _set_context(client, _human_context())  # non-admin

    resp = await client.get("/me/persona-models/manageable-service-principals")
    assert resp.status_code == 403


# ── Auth carrier integration ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_caller_uses_canonical_service_principal_id(client: AsyncClient):
    """When canonical_service_principal_id is populated in the auth carrier,
    _resolve_caller uses it directly instead of re-resolving via alias registry.
    """
    # Create a service context with canonical_service_principal_id pre-populated
    ctx = _service_context()
    ctx_with_canonical = TokenContext(
        user_id=ctx.user_id,
        org_id=ctx.org_id,
        team_id=ctx.team_id,
        department_id=ctx.department_id,
        account_type=ctx.account_type,
        is_admin=ctx.is_admin,
        expires_at=ctx.expires_at,
        auth_source=ctx.auth_source,
        canonical_service_principal_id=TEST_SP_CANONICAL_ID,
        canonical_alias_source="agent_registry",
    )
    _set_context(client, ctx_with_canonical)

    # GET should resolve via the canonical ID directly
    resp = await client.get("/me/persona-models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["principal_kind"] == "service_account"
    assert body["principal_id"] == TEST_SP_CANONICAL_ID


@pytest.mark.asyncio
async def test_resolve_caller_refuses_empty_canonical(client: AsyncClient):
    """When canonical_service_principal_id is empty, _resolve_caller refuses
    the request — no fallback to raw subject resolution.
    """
    # Service context WITHOUT canonical_service_principal_id
    ctx = TokenContext(
        user_id=TEST_SP_ALIAS_ID,
        org_id=TEST_ORG_A,
        team_id="",
        department_id="",
        account_type="service",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        auth_source="iam",
        canonical_service_principal_id="",  # explicitly empty
    )
    _set_context(client, ctx)

    resp = await client.get("/me/persona-models")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["reason"] == "unregistered_service_principal"


# ── Alias-source vocabulary: schema, column and resolution must agree ────────


def test_registration_api_accepts_exactly_the_approved_alias_sources():
    """The request schema's vocabulary must equal the column's, in both directions.

    These were independently hand-written and drifted: the schema listed three of
    the five approved sources, so ``eventbridge`` and ``github_actions`` were
    rejected at the API boundary while the column accepted them and resolution
    looked for them — an administrator could never register one, and the feature
    was unreachable for those callers by construction. The schema now derives from
    ``ALIAS_SOURCES``; this pins that it stays derived, and the *rejects* half
    matters just as much, since widening to ``str`` would satisfy the accepts half
    while letting a CHECK-violating value through as a 500.
    """
    from pydantic import ValidationError

    from src.admin.persona_models.schemas import LinkAliasRequest, RegisterServicePrincipalRequest
    from src.shared.models.persona_models import ALIAS_SOURCES

    for source in ALIAS_SOURCES:
        RegisterServicePrincipalRequest(display_name="d", alias_source=source, alias_id="a")
        LinkAliasRequest(alias_source=source, alias_id="a")

    for bad in ("oauth_client", "", "sa_registration ", "AGENT_REGISTRY", "'; drop table --"):
        with pytest.raises(ValidationError):
            RegisterServicePrincipalRequest(display_name="d", alias_source=bad, alias_id="a")
        with pytest.raises(ValidationError):
            LinkAliasRequest(alias_source=bad, alias_id="a")


def test_every_approved_alias_source_is_either_self_auth_or_admin_only():
    """Every approved alias source is either reachable via a self-auth adapter
    (``_stamp_trusted_alias_source``) or is explicitly admin-registrable-only.

    Under exact-source resolution there is no multi-source lookup map.  Instead,
    each auth path stamps exactly one trusted ``alias_source`` and resolution
    queries only that source.  Sources without a self-auth adapter
    (``sa_registration``, ``eventbridge``, ``github_actions``) are registrable by
    an admin and usable in admin-managed resolution, but have no self-service
    identity path — which is correct by design.

    This test is the structural guard that every approved source has a documented
    disposition so a newly added source cannot be silently unreachable.
    """
    from src.shared.models.persona_models import ALIAS_SOURCES

    # Sources that _stamp_trusted_alias_source maps to (auth_source → alias_source)
    self_auth_sources = {"agent_registry", "cognito_m2m"}
    # Sources registrable by admin but with no self-auth adapter (yet)
    admin_only_sources = {"sa_registration", "eventbridge", "github_actions"}

    documented = self_auth_sources | admin_only_sources
    assert documented == set(ALIAS_SOURCES), (
        f"Undocumented approved sources: {set(ALIAS_SOURCES) - documented}; stale documented sources: {documented - set(ALIAS_SOURCES)}"
    )


# ── Alias-source resolution: every approved source must be reachable ─────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("alias_source", "auth_source"),
    [
        ("agent_registry", "iam"),
        ("cognito_m2m", "jwt"),
        ("sa_registration", "jwt"),
        ("eventbridge", "jwt"),
        ("github_actions", "jwt"),
    ],
)
async def test_every_approved_alias_source_resolves(engine, alias_source: str, auth_source: str):
    """Each of the five approved alias sources must resolve to its principal.

    The defect this pins made three of the five **unreachable**: resolution mapped
    ``auth_source`` to a single alias source (``iam``→``agent_registry``, everything
    else→``sa_registration``), but four approved sources arrive as ``jwt``. So an
    administrator could register a ``cognito_m2m``, ``eventbridge`` or
    ``github_actions`` caller, the registry would report it active, and the lookup
    would never match it. Paired with the (correct) no-fallback refusal on the self
    routes, that is a permanent refusal for a correctly-registered caller — the
    inert-config class reached through resolution rather than schema.

    Asserting the returned source, not just the ID, is the other half: the auth
    layer stamps this value onto the context as provenance.
    """
    from src.admin.persona_models.service import resolve_by_exact_source

    canonical = f"sp-{alias_source}"
    subject = f"subject-{alias_source}"

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ServicePrincipal(
                canonical_service_principal_id=canonical,
                org_id=TEST_ORG_A,
                display_name=f"{alias_source} caller",
                status="active",
                approved_by=TEST_ADMIN_USER_ID,
            )
        )
        session.add(
            ServicePrincipalAlias(
                id=new_uuid(),
                canonical_service_principal_id=canonical,
                org_id=TEST_ORG_A,
                alias_source=alias_source,
                alias_id=subject,
                is_active=True,
                registered_by=TEST_ADMIN_USER_ID,
            )
        )
        await session.commit()

    async with factory() as session:
        resolved_id, resolved_source = await resolve_by_exact_source(session, alias_source=alias_source, alias_id=subject, org_id=TEST_ORG_A)

    assert resolved_id == canonical, f"An active {alias_source} alias did not resolve — the caller is unreachable"
    assert resolved_source == alias_source, "Resolution must report the source it matched, not one inferred from auth_source"


@pytest.mark.asyncio
async def test_resolution_reports_matched_source_not_inferred_source(engine):
    """A legacy ``sa_registration`` caller must not be labelled ``cognito_m2m``.

    Both arrive as ``auth_source="jwt"``, so any label derived from ``auth_source``
    is a guess. This asserts the *stored* provenance follows the matched row, since
    ``principal_source`` exists precisely to record where an identity came from and
    a wrong value there is silent misattribution.
    """
    from src.admin.persona_models.service import derive_principal_source, resolve_by_exact_source

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ServicePrincipal(
                canonical_service_principal_id="sp-legacy",
                org_id=TEST_ORG_A,
                display_name="Legacy service account",
                status="active",
                approved_by=TEST_ADMIN_USER_ID,
            )
        )
        session.add(
            ServicePrincipalAlias(
                id=new_uuid(),
                canonical_service_principal_id="sp-legacy",
                org_id=TEST_ORG_A,
                alias_source="sa_registration",
                alias_id="legacy-subject",
                is_active=True,
                registered_by=TEST_ADMIN_USER_ID,
            )
        )
        await session.commit()

    # Exact-source resolution: sa_registration alias resolves when queried with its own source
    async with factory() as session:
        resolved_id, resolved_source = await resolve_by_exact_source(
            session, alias_source="sa_registration", alias_id="legacy-subject", org_id=TEST_ORG_A
        )

    assert resolved_id == "sp-legacy"
    assert resolved_source == "sa_registration"
    # And the value actually written to `principal_source` follows the match.
    assert derive_principal_source("service", "jwt", resolved_source) == "sa_registration"


@pytest.mark.asyncio
async def test_same_string_two_sources_each_resolves_own(engine):
    """Same alias_id under two sources for two principals: each source resolves its own.

    Active-alias uniqueness is scoped to ``(org_id, alias_source, alias_id)``, so
    this state is legal. With exact-source resolution, querying ``cognito_m2m`` gets
    principal A and querying ``sa_registration`` gets principal B — no ambiguity,
    no cross-namespace misbinding.
    """
    from src.admin.persona_models.service import resolve_by_exact_source

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        for canonical, alias_source in (("sp-amb-1", "cognito_m2m"), ("sp-amb-2", "sa_registration")):
            session.add(
                ServicePrincipal(
                    canonical_service_principal_id=canonical,
                    org_id=TEST_ORG_A,
                    display_name=canonical,
                    status="active",
                    approved_by=TEST_ADMIN_USER_ID,
                )
            )
            session.add(
                ServicePrincipalAlias(
                    id=new_uuid(),
                    canonical_service_principal_id=canonical,
                    org_id=TEST_ORG_A,
                    alias_source=alias_source,
                    alias_id="contested-subject",
                    is_active=True,
                    registered_by=TEST_ADMIN_USER_ID,
                )
            )
        await session.commit()

    # cognito_m2m resolves to sp-amb-1
    async with factory() as session:
        resolved_id, resolved_source = await resolve_by_exact_source(
            session, alias_source="cognito_m2m", alias_id="contested-subject", org_id=TEST_ORG_A
        )
    assert resolved_id == "sp-amb-1"
    assert resolved_source == "cognito_m2m"

    # sa_registration resolves to sp-amb-2
    async with factory() as session:
        resolved_id, resolved_source = await resolve_by_exact_source(
            session, alias_source="sa_registration", alias_id="contested-subject", org_id=TEST_ORG_A
        )
    assert resolved_id == "sp-amb-2"
    assert resolved_source == "sa_registration"


@pytest.mark.asyncio
async def test_revoked_alias_does_not_resolve(engine):
    """A revoked alias must stop resolving, and must not be reported as a source.

    Revocation keeps the row so history survives; what must change is that it no
    longer resolves. Without this the withdrawal would be cosmetic.
    """
    from src.admin.persona_models.service import resolve_by_exact_source

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ServicePrincipal(
                canonical_service_principal_id="sp-revoked",
                org_id=TEST_ORG_A,
                display_name="Revoked caller",
                status="active",
                approved_by=TEST_ADMIN_USER_ID,
            )
        )
        session.add(
            ServicePrincipalAlias(
                id=new_uuid(),
                canonical_service_principal_id="sp-revoked",
                org_id=TEST_ORG_A,
                alias_source="cognito_m2m",
                alias_id="revoked-subject",
                is_active=False,
                registered_by=TEST_ADMIN_USER_ID,
                revoked_at=datetime.now(UTC),
                revoked_by=TEST_ADMIN_USER_ID,
            )
        )
        await session.commit()

    async with factory() as session:
        resolved_id, resolved_source = await resolve_by_exact_source(
            session, alias_source="cognito_m2m", alias_id="revoked-subject", org_id=TEST_ORG_A
        )

    assert resolved_id is None
    assert resolved_source == ""


@pytest.mark.asyncio
async def test_resolution_is_tenant_scoped(engine):
    """A subject registered in one tenant must not resolve for another."""
    from src.admin.persona_models.service import resolve_by_exact_source

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ServicePrincipal(
                canonical_service_principal_id="sp-tenant-b",
                org_id=TEST_ORG_B,
                display_name="Tenant B caller",
                status="active",
                approved_by=TEST_ADMIN_USER_ID,
            )
        )
        session.add(
            ServicePrincipalAlias(
                id=new_uuid(),
                canonical_service_principal_id="sp-tenant-b",
                org_id=TEST_ORG_B,
                alias_source="cognito_m2m",
                alias_id="shared-subject-name",
                is_active=True,
                registered_by=TEST_ADMIN_USER_ID,
            )
        )
        await session.commit()

    async with factory() as session:
        resolved_id, _ = await resolve_by_exact_source(session, alias_source="cognito_m2m", alias_id="shared-subject-name", org_id=TEST_ORG_A)

    assert resolved_id is None, "Tenant A resolved a tenant-B alias — cross-tenant identity leak"


# ── Lifecycle transitions ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lifecycle_active_to_suspended(engine, seed_data):
    """Valid transition: active → suspended."""
    from src.admin.persona_models.service import transition_service_principal_status

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        sp = await transition_service_principal_status(session, canonical_id=TEST_SP_CANONICAL_ID, org_id=TEST_ORG_A, new_status="suspended")
        assert sp.status == "suspended"
        await session.commit()


@pytest.mark.asyncio
async def test_lifecycle_suspended_to_retired(engine, seed_data):
    """Valid transition: suspended → retired (terminal)."""
    from src.admin.persona_models.service import transition_service_principal_status

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await transition_service_principal_status(session, canonical_id=TEST_SP_CANONICAL_ID, org_id=TEST_ORG_A, new_status="suspended")
        await session.commit()

    async with factory() as session:
        sp = await transition_service_principal_status(session, canonical_id=TEST_SP_CANONICAL_ID, org_id=TEST_ORG_A, new_status="retired")
        assert sp.status == "retired"
        await session.commit()


@pytest.mark.asyncio
async def test_lifecycle_retired_is_terminal(engine, seed_data):
    """Invalid transition: retired → active is refused."""
    from src.admin.persona_models.service import PreferenceRejectedError, transition_service_principal_status

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await transition_service_principal_status(session, canonical_id=TEST_SP_CANONICAL_ID, org_id=TEST_ORG_A, new_status="retired")
        await session.commit()

    async with factory() as session:
        with pytest.raises(PreferenceRejectedError, match="Cannot transition"):
            await transition_service_principal_status(session, canonical_id=TEST_SP_CANONICAL_ID, org_id=TEST_ORG_A, new_status="active")


@pytest.mark.asyncio
async def test_lifecycle_invalid_transition_refused(engine, seed_data):
    """Invalid transition: active → active is refused (no self-transition)."""
    from src.admin.persona_models.service import PreferenceRejectedError, transition_service_principal_status

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        with pytest.raises(PreferenceRejectedError, match="Cannot transition"):
            await transition_service_principal_status(session, canonical_id=TEST_SP_CANONICAL_ID, org_id=TEST_ORG_A, new_status="active")


# ── Cross-namespace isolation (decisive negative tests) ────────────────────


@pytest.mark.asyncio
async def test_cognito_m2m_cannot_resolve_sa_registration_alias(engine):
    """Decisive negative: sa_registration alias with alias_id X must NOT resolve
    when queried with cognito_m2m as the trusted source — even though the string matches.

    This is the cross-namespace misbinding the multi-source lookup allowed.
    """
    from src.admin.persona_models.service import resolve_by_exact_source

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ServicePrincipal(
                canonical_service_principal_id="sp-sa-only",
                org_id=TEST_ORG_A,
                display_name="SA-only principal",
                status="active",
                approved_by=TEST_ADMIN_USER_ID,
            )
        )
        session.add(
            ServicePrincipalAlias(
                id=new_uuid(),
                canonical_service_principal_id="sp-sa-only",
                org_id=TEST_ORG_A,
                alias_source="sa_registration",
                alias_id="shared-string-X",
                is_active=True,
                registered_by=TEST_ADMIN_USER_ID,
            )
        )
        await session.commit()

    # A Cognito M2M caller with client_id "shared-string-X" MUST NOT resolve sp-sa-only
    async with factory() as session:
        resolved_id, _ = await resolve_by_exact_source(session, alias_source="cognito_m2m", alias_id="shared-string-X", org_id=TEST_ORG_A)
    assert resolved_id is None, "cognito_m2m resolved a sa_registration alias — cross-namespace misbinding"


@pytest.mark.asyncio
async def test_agent_registry_cannot_resolve_cognito_m2m_alias(engine):
    """Inverse negative: cognito_m2m alias must NOT resolve via agent_registry."""
    from src.admin.persona_models.service import resolve_by_exact_source

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ServicePrincipal(
                canonical_service_principal_id="sp-cognito-only",
                org_id=TEST_ORG_A,
                display_name="Cognito-only principal",
                status="active",
                approved_by=TEST_ADMIN_USER_ID,
            )
        )
        session.add(
            ServicePrincipalAlias(
                id=new_uuid(),
                canonical_service_principal_id="sp-cognito-only",
                org_id=TEST_ORG_A,
                alias_source="cognito_m2m",
                alias_id="shared-string-Y",
                is_active=True,
                registered_by=TEST_ADMIN_USER_ID,
            )
        )
        await session.commit()

    # An agent_registry caller with agent_name "shared-string-Y" MUST NOT resolve
    async with factory() as session:
        resolved_id, _ = await resolve_by_exact_source(session, alias_source="agent_registry", alias_id="shared-string-Y", org_id=TEST_ORG_A)
    assert resolved_id is None, "agent_registry resolved a cognito_m2m alias — cross-namespace misbinding"


@pytest.mark.asyncio
async def test_trusted_source_stamping():
    """_stamp_trusted_alias_source sets the correct source per auth path."""
    from src.auth.dependencies import _stamp_trusted_alias_source

    iam_ctx = TokenContext(
        user_id="agent-1",
        org_id="org-1",
        team_id="",
        department_id="",
        account_type="service",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        auth_source="iam",
    )
    enriched = _stamp_trusted_alias_source(iam_ctx)
    assert enriched.canonical_alias_source == "agent_registry"

    jwt_ctx = TokenContext(
        user_id="client-1",
        org_id="org-1",
        team_id="",
        department_id="",
        account_type="service",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        auth_source="jwt",
    )
    enriched = _stamp_trusted_alias_source(jwt_ctx)
    assert enriched.canonical_alias_source == "cognito_m2m"

    human_ctx = TokenContext(
        user_id="user-1",
        org_id="org-1",
        team_id="t",
        department_id="d",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        auth_source="jwt",
    )
    enriched = _stamp_trusted_alias_source(human_ctx)
    assert enriched.canonical_alias_source == "", "Human callers must not get a stamped source"


# ── Lifecycle status API ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lifecycle_api_suspend(client: AsyncClient, engine):
    """PATCH /service-principals/{id}/status transitions to suspended with audit."""
    _set_context(client, _admin_context())

    resp = await client.patch(
        f"/service-principals/{TEST_SP_CANONICAL_ID}/status",
        json={"status": "suspended"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["previous_status"] == "active"
    assert body["status"] == "suspended"
    assert body["canonical_service_principal_id"] == TEST_SP_CANONICAL_ID

    # Verify audit
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        rows = list(await session.scalars(select(AuditLog).where(AuditLog.event_type == "service_principal_status_changed")))
        assert len(rows) >= 1
        details = rows[-1].details
        assert details["previous_status"] == "active"
        assert details["new_status"] == "suspended"
        assert details["actor_kind"] == "human_admin"


@pytest.mark.asyncio
async def test_lifecycle_api_reactivate(client: AsyncClient):
    """PATCH suspended → active."""
    _set_context(client, _admin_context())

    resp = await client.patch(
        f"/service-principals/{TEST_SP_CANONICAL_ID}/status",
        json={"status": "suspended"},
    )
    assert resp.status_code == 200

    resp = await client.patch(
        f"/service-principals/{TEST_SP_CANONICAL_ID}/status",
        json={"status": "active"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


@pytest.mark.asyncio
async def test_lifecycle_api_retire_is_terminal(client: AsyncClient):
    """PATCH to retired, then attempt active — should fail."""
    _set_context(client, _admin_context())

    resp = await client.patch(
        f"/service-principals/{TEST_SP_CANONICAL_ID}/status",
        json={"status": "retired"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "retired"

    resp = await client.patch(
        f"/service-principals/{TEST_SP_CANONICAL_ID}/status",
        json={"status": "active"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "invalid_status_transition"


@pytest.mark.asyncio
async def test_lifecycle_api_service_caller_forbidden(client: AsyncClient):
    """Service callers cannot change lifecycle status."""
    _set_context(client, _service_context())

    resp = await client.patch(
        f"/service-principals/{TEST_SP_CANONICAL_ID}/status",
        json={"status": "suspended"},
    )
    assert resp.status_code == 403


# ── Lifecycle gates ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_link_alias_refused_on_suspended_principal(engine, seed_data):
    """Suspended principals refuse new aliases."""
    from src.admin.persona_models.service import PreferenceRejectedError, link_alias, transition_service_principal_status

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await transition_service_principal_status(session, canonical_id=TEST_SP_CANONICAL_ID, org_id=TEST_ORG_A, new_status="suspended")
        await session.commit()

    async with factory() as session:
        with pytest.raises(PreferenceRejectedError, match="Only active principals"):
            await link_alias(
                session,
                canonical_id=TEST_SP_CANONICAL_ID,
                org_id=TEST_ORG_A,
                alias_source="cognito_m2m",
                alias_id="new-alias-for-suspended",
                registered_by=TEST_ADMIN_USER_ID,
            )


@pytest.mark.asyncio
async def test_link_alias_refused_on_retired_principal(engine, seed_data):
    """Retired principals refuse new aliases."""
    from src.admin.persona_models.service import PreferenceRejectedError, link_alias, transition_service_principal_status

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await transition_service_principal_status(session, canonical_id=TEST_SP_CANONICAL_ID, org_id=TEST_ORG_A, new_status="retired")
        await session.commit()

    async with factory() as session:
        with pytest.raises(PreferenceRejectedError, match="Only active principals"):
            await link_alias(
                session,
                canonical_id=TEST_SP_CANONICAL_ID,
                org_id=TEST_ORG_A,
                alias_source="cognito_m2m",
                alias_id="new-alias-for-retired",
                registered_by=TEST_ADMIN_USER_ID,
            )


@pytest.mark.asyncio
async def test_admin_preference_set_refused_on_retired_principal(client: AsyncClient, engine):
    """Retired principals refuse preference mutations via the admin endpoint."""
    _set_context(client, _admin_context())

    # Retire
    resp = await client.patch(
        f"/service-principals/{TEST_SP_CANONICAL_ID}/status",
        json={"status": "retired"},
    )
    assert resp.status_code == 200

    # Attempt preference set — should be refused
    with _patch_validator():
        resp = await client.put(
            f"/service-principals/{TEST_SP_CANONICAL_ID}/persona-models/developer",
            json={"model": "us.anthropic.claude-opus-4-6"},
        )
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "principal_retired"


# ── Schema additions coverage ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_eventbridge_alias(client: AsyncClient):
    """eventbridge alias source accepted by the register endpoint."""
    _set_context(client, _admin_context())

    resp = await client.post(
        "/service-principals/register",
        json={"display_name": "EB Worker", "alias_source": "eventbridge", "alias_id": "eb-rule-123"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["alias_source"] == "eventbridge"


@pytest.mark.asyncio
async def test_register_github_actions_alias(client: AsyncClient):
    """github_actions alias source accepted by the register endpoint."""
    _set_context(client, _admin_context())

    resp = await client.post(
        "/service-principals/register",
        json={"display_name": "GHA Worker", "alias_source": "github_actions", "alias_id": "gha-runner-456"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["alias_source"] == "github_actions"


@pytest.mark.asyncio
async def test_link_eventbridge_alias(client: AsyncClient):
    """eventbridge accepted as a linkable alias source."""
    _set_context(client, _admin_context())

    resp = await client.post(
        f"/service-principals/{TEST_SP_CANONICAL_ID}/aliases",
        json={"alias_source": "eventbridge", "alias_id": "eb-link-789"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["alias_source"] == "eventbridge"


# ── Corrupt / cross-tenant alias isolation ─────────────────────────────────


@pytest.mark.asyncio
async def test_corrupt_alias_cross_tenant_isolation(engine):
    """An alias in tenant B with the same source+id as one in tenant A
    must not resolve when queried for tenant A."""
    from src.admin.persona_models.service import resolve_by_exact_source

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        # Register in tenant B
        session.add(
            ServicePrincipal(
                canonical_service_principal_id="sp-b-corrupt",
                org_id=TEST_ORG_B,
                display_name="B corrupt test",
                status="active",
                approved_by="admin-b",
            )
        )
        session.add(
            ServicePrincipalAlias(
                id=new_uuid(),
                canonical_service_principal_id="sp-b-corrupt",
                org_id=TEST_ORG_B,
                alias_source="agent_registry",
                alias_id="shared-corrupt-agent",
                is_active=True,
                registered_by="admin-b",
            )
        )
        await session.commit()

    # Query in tenant A — must not resolve
    async with factory() as session:
        resolved_id, _ = await resolve_by_exact_source(session, alias_source="agent_registry", alias_id="shared-corrupt-agent", org_id=TEST_ORG_A)
    assert resolved_id is None, "Cross-tenant alias resolved — isolation broken"


@pytest.mark.asyncio
async def test_suspension_actually_blocks_the_suspended_caller(client: AsyncClient):
    """Suspending a principal must stop it using the feature, not just change a column.

    ``test_lifecycle_api_suspend`` above proves the route returns 200, persists
    ``suspended`` and audits it. None of that proves the status is *enforced*: every
    preference path filters on ``status == "active"``, and if that filter were dropped
    the transition tests would all still pass while a suspended principal kept working.
    This drives the same caller either side of the transition, so the assertion is the
    consequence rather than the column.
    """
    # Active: the service caller can read and write its own preferences.
    _set_context(client, _service_context())
    assert (await client.get("/me/persona-models")).status_code == 200

    _set_context(client, _admin_context())
    assert (await client.patch(f"/service-principals/{TEST_SP_CANONICAL_ID}/status", json={"status": "suspended"})).status_code == 200

    # Suspended: the same caller is refused, with the reason that names why.
    _set_context(client, _service_context())
    resp = await client.get("/me/persona-models")
    assert resp.status_code == 422, "A suspended principal could still read preferences — suspension is cosmetic"
    assert resp.json()["detail"]["reason"] == "principal_not_active"

    with _patch_validator():
        resp = await client.put("/me/persona-models/developer", json={"model": "some-model"})
    assert resp.status_code == 422, "A suspended principal could still WRITE a preference"
    assert resp.json()["detail"]["reason"] == "principal_not_active"


@pytest.mark.asyncio
async def test_cross_tenant_status_transition_refused_without_touching_victim(client: AsyncClient, engine):
    """An admin must not drive the lifecycle of a principal in another tenant.

    The lifecycle route is a tenant-isolation surface like every other admin verb, so
    §5.6 applies: refuse, audit in the CALLER's tenant, and leave the victim's trail
    empty. Suspending or retiring another tenant's principal would be a denial-of-
    service against it, which is why this is asserted against the victim's stored row
    and not only against the status code.

    Scope note: the tenant predicate exists in both ``validate_target_service_principal``
    and ``transition_service_principal_status``, so this test survives the removal of
    either one and fails only when both go. That is deliberate — it pins the isolation
    *property* rather than one line, and the redundancy is defence in depth worth
    keeping. It does mean this test alone will not tell you which layer regressed.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ServicePrincipal(
                canonical_service_principal_id="sp-b-status",
                org_id=TEST_ORG_B,
                display_name="Org B Worker",
                status="active",
                approved_by="admin-b",
            )
        )
        await session.commit()

    _set_context(client, _admin_context())
    resp = await client.patch("/service-principals/sp-b-status/status", json={"status": "retired"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "principal_not_found"

    async with factory() as session:
        victim = await session.scalar(select(ServicePrincipal).where(ServicePrincipal.canonical_service_principal_id == "sp-b-status"))
        victim_audit = list(await session.scalars(select(AuditLog).where(AuditLog.org_id == TEST_ORG_B)))
    assert victim.status == "active", "Cross-tenant transition mutated another tenant's principal"
    assert victim_audit == [], "Refusal wrote an audit row into the victim's tenant (§5.6)"


@pytest.mark.asyncio
async def test_refused_status_transition_is_audited(client: AsyncClient, engine):
    """A refused lifecycle transition must leave a trace, like every other refusal here.

    The two refusals reachable on this route are an attempt to reinstate a retired
    principal and a cross-tenant attempt. Both are precisely what an auditor would
    want to see, and both previously raised 422 while recording nothing — the set and
    reset handlers on the same surface already audit their refusals.
    """
    _set_context(client, _admin_context())
    assert (await client.patch(f"/service-principals/{TEST_SP_CANONICAL_ID}/status", json={"status": "retired"})).status_code == 200
    assert (await client.patch(f"/service-principals/{TEST_SP_CANONICAL_ID}/status", json={"status": "active"})).status_code == 422

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        refusal = await session.scalar(select(AuditLog).where(AuditLog.event_type == "service_principal_status_transition_rejected"))
    assert refusal is not None, "A refused lifecycle transition was not audited"
    # actor_id is the admin's canonical ID, never a raw per-auth-path subject (§5.5).
    assert refusal.actor_id == TEST_ADMIN_USER_ID
    assert refusal.org_id == TEST_ORG_A
    assert refusal.details["requested_status"] == "active"
    assert refusal.details["reason"] == "invalid_status_transition"
    assert refusal.details["target_principal_id"] == TEST_SP_CANONICAL_ID


@pytest.mark.asyncio
async def test_cross_namespace_misbinding_is_refused_end_to_end(engine):
    """The stamp and the resolver must be correct *together*, not just separately.

    ``test_trusted_source_stamping`` proves each auth path stamps the right source.
    ``test_cognito_m2m_cannot_resolve_sa_registration_alias`` and its inverse prove the
    resolver is exact. Neither composes them: both negative tests pass ``alias_source``
    in by hand, so they assume the stamp is correct rather than using it. Swap the two
    branches of ``_stamp_trusted_alias_source`` and every one of them still passes,
    while a Cognito caller binds to an ``agent_registry`` identity — the precise
    cross-namespace misbinding this refactor exists to prevent.

    This drives the real chain: build the context through the stamping function, then
    resolve through the real dependency, and assert the identity that comes back.
    """
    from src.admin.persona_models.self_routes import get_persona_model_current_user
    from src.auth.dependencies import _stamp_trusted_alias_source

    factory = async_sessionmaker(engine, expire_on_commit=False)
    # The same string registered under both namespaces, owned by different principals.
    async with factory() as session:
        for canonical, source in (("sp-agent-ns", "agent_registry"), ("sp-cognito-ns", "cognito_m2m")):
            session.add(
                ServicePrincipal(
                    canonical_service_principal_id=canonical,
                    org_id=TEST_ORG_A,
                    display_name=canonical,
                    status="active",
                    approved_by=TEST_ADMIN_USER_ID,
                )
            )
            session.add(
                ServicePrincipalAlias(
                    id=new_uuid(),
                    canonical_service_principal_id=canonical,
                    org_id=TEST_ORG_A,
                    alias_source=source,
                    alias_id="collide-me",
                    is_active=True,
                    registered_by=TEST_ADMIN_USER_ID,
                )
            )
        await session.commit()

    # auth_source → the canonical principal that path must land on, and only that one.
    expected = {"iam": "sp-agent-ns", "jwt": "sp-cognito-ns"}
    for auth_source, must_resolve_to in expected.items():
        ctx = _stamp_trusted_alias_source(
            TokenContext(
                user_id="collide-me",
                org_id=TEST_ORG_A,
                team_id="team-1",
                department_id="dept-1",
                account_type="service",
                is_admin=False,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                auth_source=auth_source,
            )
        )
        async with factory() as session:
            enriched = await get_persona_model_current_user(ctx, session)
        other = (set(expected.values()) - {must_resolve_to}).pop()
        assert enriched.canonical_service_principal_id != other, (
            f"auth_source={auth_source!r} bound to {other!r} — cross-namespace misbinding through the real auth chain"
        )
        assert enriched.canonical_service_principal_id == must_resolve_to


# ── Real upstream auth path tests (review item 2) ────────────────────────
#
# These tests exercise the REAL ``get_current_user`` and
# ``get_persona_model_current_user`` dependency chain — not the overrides
# the other tests use.  They prove the identity fields that arrive on
# ``TokenContext`` from upstream auth (IAM/Agent Registry and Cognito M2M)
# and that those fields resolve correctly through the PMM enrichment dependency.


class TestRealIamAgentRegistryAuth:
    """Prove Agent Registry / SigV4 auth stamps ``agent_registry`` and uses ``agent_name``.

    The request path is: X-Caller-Identity header → parse_assumed_role_arn →
    registry lookup → agent_entry_to_token_context → _stamp_trusted_alias_source.
    """

    @staticmethod
    def _request(caller_identity: str):
        headers = {"x-caller-identity": caller_identity, "x-adp-edge-provenance": "test-edge-provenance"}

        class _State:
            pass

        request = MagicMock(spec=Request)
        request.headers = Headers(headers)
        request.state = _State()
        return request

    @staticmethod
    def _trust_enabled():
        settings = MagicMock()
        settings.trust_apigw_headers = True
        settings.apigw_provenance_secret = "test-edge-provenance"
        return settings

    @pytest.mark.asyncio
    async def test_iam_auth_stamps_agent_registry_source(self):
        """After the real get_current_user IAM path, canonical_alias_source is agent_registry."""
        entry = {
            "agent_name": "my-worker-agent",
            "org_id": TEST_ORG_A,
            "team_id": "team-1",
            "scope": "",
            "requires_run_identity": False,
            "credential_scopes": [],
        }
        registry = MagicMock()
        registry.get_agent_by_role_arn.return_value = entry

        with (
            patch("src.auth.dependencies.get_settings", return_value=self._trust_enabled()),
            patch("src.auth.agent_registry.get_agent_registry_service", return_value=registry),
        ):
            ctx = await get_current_user(
                self._request("arn:aws:sts::123456789012:assumed-role/agent-role/session"),
                authorization=None,
            )

        assert ctx.account_type == "service"
        assert ctx.auth_source == "iam"
        assert ctx.user_id == "my-worker-agent"
        assert ctx.canonical_alias_source == "agent_registry"

    @pytest.mark.asyncio
    async def test_iam_auth_resolves_through_pmm_dependency(self, engine):
        """An agent_registry alias resolves through get_persona_model_current_user.

        Builds a real DB session, seeds the alias row, and passes the context from
        get_current_user through the enrichment dependency.
        """
        from src.admin.persona_models.self_routes import get_persona_model_current_user

        canonical = "sp-iam-resolve"
        agent_name = "iam-resolve-agent"

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(
                ServicePrincipal(
                    canonical_service_principal_id=canonical,
                    org_id=TEST_ORG_A,
                    display_name="IAM resolver",
                    status="active",
                    approved_by=TEST_ADMIN_USER_ID,
                )
            )
            session.add(
                ServicePrincipalAlias(
                    id=new_uuid(),
                    canonical_service_principal_id=canonical,
                    org_id=TEST_ORG_A,
                    alias_source="agent_registry",
                    alias_id=agent_name,
                    is_active=True,
                    registered_by=TEST_ADMIN_USER_ID,
                )
            )
            await session.commit()

        iam_ctx = TokenContext(
            user_id=agent_name,
            org_id=TEST_ORG_A,
            team_id="",
            department_id="",
            account_type="service",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            auth_source="iam",
            canonical_alias_source="agent_registry",
        )

        async with factory() as session:
            enriched = await get_persona_model_current_user(iam_ctx, session)

        assert enriched.canonical_service_principal_id == canonical
        assert enriched.canonical_alias_source == "agent_registry"


class TestRealCognitoM2MAuth:
    """Prove Cognito M2M auth uses ``client_id`` (not ``sub``) through the real
    ``get_current_user()`` dependency — not by calling helpers directly.

    The request path is: Authorization header → _get_cognito_validator().validate_token
    → _parse_claims → _cognito_claims_to_context → _stamp_trusted_alias_source.
    """

    @staticmethod
    def _request():
        """A mock Request with an Authorization header and no IAM identity."""

        class _State:
            pass

        request = MagicMock(spec=Request)
        request.headers = Headers({"authorization": "Bearer fake-m2m-token"})
        request.state = _State()
        return request

    @staticmethod
    def _mock_validator(payload: dict):
        """Parse a raw Cognito payload, then return it from the mocked verifier."""
        validator = MagicMock(spec=CognitoJWTValidator)
        validator.validate_token.return_value = CognitoJWTValidator._parse_claims(validator, payload)
        return validator

    @staticmethod
    def _m2m_payload(*, client_id: str, sub: str = "sub-differs-from-client", org_id: str = TEST_ORG_A):
        """A coherent client_credentials token payload where client_id != sub."""
        return {
            "sub": sub,
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test",
            "client_id": client_id,
            "token_use": "access",
            "exp": 9999999999,
            "iat": 1000000000,
            # Real Cognito keys: client_credentials access tokens have no
            # username, and custom claims retain their ``custom:`` prefix.
            "custom:account_type": "service",
            "custom:org_id": org_id,
        }

    @pytest.mark.asyncio
    async def test_service_token_uses_client_id_not_sub(self):
        """Through get_current_user, user_id must be client_id (not sub)."""
        payload = self._m2m_payload(client_id="my-client-id-123", sub="different-sub")
        validator = self._mock_validator(payload)

        settings = MagicMock()
        settings.trust_apigw_headers = False

        with (
            patch("src.auth.dependencies.get_settings", return_value=settings),
            patch("src.auth.dependencies._get_cognito_validator", return_value=validator),
        ):
            ctx = await get_current_user(self._request(), authorization="Bearer fake-m2m-token")

        assert ctx.user_id == "my-client-id-123", "user_id should be client_id, not sub"
        assert ctx.user_id != "different-sub", "user_id must not be sub"
        assert ctx.account_type == "service"
        assert ctx.canonical_alias_source == "cognito_m2m"
        assert validator.validate_token.return_value.username == ""

    @pytest.mark.asyncio
    async def test_service_token_without_client_id_returns_401(self):
        """An incoherent service token (no client_id) must get 401 — not 500.

        Before the HTTPException re-raise fix, the generic ``except Exception``
        swallowed the 401 from _cognito_claims_to_context and returned 500.
        """
        payload = self._m2m_payload(client_id="will-be-emptied")
        payload["client_id"] = ""
        validator = self._mock_validator(payload)

        settings = MagicMock()
        settings.trust_apigw_headers = False

        with (
            patch("src.auth.dependencies.get_settings", return_value=settings),
            patch("src.auth.dependencies._get_cognito_validator", return_value=validator),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(self._request(), authorization="Bearer fake-m2m-token")

        assert exc_info.value.status_code == 401, "Must be 401, not 500"
        assert exc_info.value.detail["error"] == "incoherent_service_token"

    @pytest.mark.asyncio
    async def test_human_token_still_uses_sub(self):
        """Human tokens must not be affected by the client_id change."""
        payload = {
            "sub": "human-sub-456",
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test",
            "client_id": "web-client",
            "token_use": "access",
            "exp": 9999999999,
            "iat": 1000000000,
            "username": "user@example.com",
            "custom:account_type": "human",
            "custom:org_id": TEST_ORG_A,
        }
        validator = self._mock_validator(payload)

        settings = MagicMock()
        settings.trust_apigw_headers = False

        with (
            patch("src.auth.dependencies.get_settings", return_value=settings),
            patch("src.auth.dependencies._get_cognito_validator", return_value=validator),
        ):
            ctx = await get_current_user(self._request(), authorization="Bearer fake-m2m-token")

        assert ctx.user_id == "human-sub-456", "Human user_id must remain sub"

    @pytest.mark.asyncio
    async def test_cognito_m2m_stamps_alias_source(self):
        """Through get_current_user, a service JWT caller has canonical_alias_source=cognito_m2m."""
        payload = self._m2m_payload(client_id="m2m-client-99")
        validator = self._mock_validator(payload)

        settings = MagicMock()
        settings.trust_apigw_headers = False

        with (
            patch("src.auth.dependencies.get_settings", return_value=settings),
            patch("src.auth.dependencies._get_cognito_validator", return_value=validator),
        ):
            ctx = await get_current_user(self._request(), authorization="Bearer fake-m2m-token")

        assert ctx.canonical_alias_source == "cognito_m2m"
        assert ctx.user_id == "m2m-client-99"

    @pytest.mark.asyncio
    async def test_cognito_m2m_resolves_through_pmm_dependency(self, engine):
        """Full chain: get_current_user (mocked JWT) → get_persona_model_current_user (real DB).

        Seeds a cognito_m2m alias row and proves the context from get_current_user
        resolves to the correct canonical principal through the real enrichment dependency.
        """
        from src.admin.persona_models.self_routes import get_persona_model_current_user

        canonical = "sp-m2m-resolve"
        client_id = "m2m-client-resolve-42"

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(
                ServicePrincipal(
                    canonical_service_principal_id=canonical,
                    org_id=TEST_ORG_A,
                    display_name="M2M resolver",
                    status="active",
                    approved_by=TEST_ADMIN_USER_ID,
                )
            )
            session.add(
                ServicePrincipalAlias(
                    id=new_uuid(),
                    canonical_service_principal_id=canonical,
                    org_id=TEST_ORG_A,
                    alias_source="cognito_m2m",
                    alias_id=client_id,
                    is_active=True,
                    registered_by=TEST_ADMIN_USER_ID,
                )
            )
            await session.commit()

        # Step 1: get_current_user with mocked JWT validator
        payload = self._m2m_payload(client_id=client_id)
        validator = self._mock_validator(payload)
        settings = MagicMock()
        settings.trust_apigw_headers = False

        with (
            patch("src.auth.dependencies.get_settings", return_value=settings),
            patch("src.auth.dependencies._get_cognito_validator", return_value=validator),
        ):
            ctx = await get_current_user(self._request(), authorization="Bearer fake-m2m-token")

        assert ctx.user_id == client_id
        assert ctx.canonical_alias_source == "cognito_m2m"

        # Step 2: feed the real context through get_persona_model_current_user with a real DB session
        async with factory() as session:
            enriched = await get_persona_model_current_user(ctx, session)

        assert enriched.canonical_service_principal_id == canonical
        assert enriched.canonical_alias_source == "cognito_m2m"


class TestCrossNamespaceNegativesThroughDependency:
    """Decisive negatives through the real get_persona_model_current_user dependency.

    DB has ``(org, sa_registration, X) -> A``. A Cognito M2M caller with
    ``client_id == X`` must NOT resolve A. And the inverse.
    """

    @pytest.mark.asyncio
    async def test_cognito_m2m_caller_does_not_resolve_sa_registration_alias(self, engine):
        """DB only has (org, sa_registration, X) → A. Cognito M2M with client_id=X must remain unregistered."""
        from src.admin.persona_models.self_routes import get_persona_model_current_user

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(
                ServicePrincipal(
                    canonical_service_principal_id="sp-sa-negative",
                    org_id=TEST_ORG_A,
                    display_name="SA negative",
                    status="active",
                    approved_by=TEST_ADMIN_USER_ID,
                )
            )
            session.add(
                ServicePrincipalAlias(
                    id=new_uuid(),
                    canonical_service_principal_id="sp-sa-negative",
                    org_id=TEST_ORG_A,
                    alias_source="sa_registration",
                    alias_id="contested-x",
                    is_active=True,
                    registered_by=TEST_ADMIN_USER_ID,
                )
            )
            await session.commit()

        # Cognito M2M caller arrives with client_id "contested-x" — MUST NOT resolve
        m2m_ctx = TokenContext(
            user_id="contested-x",
            org_id=TEST_ORG_A,
            team_id="",
            department_id="",
            account_type="service",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            auth_source="jwt",
            canonical_alias_source="cognito_m2m",
        )

        async with factory() as session:
            enriched = await get_persona_model_current_user(m2m_ctx, session)

        assert enriched.canonical_service_principal_id == "", (
            "Cognito M2M caller resolved an sa_registration alias through the dependency — cross-namespace misbinding"
        )

    @pytest.mark.asyncio
    async def test_iam_caller_does_not_resolve_cognito_m2m_alias(self, engine):
        """DB only has (org, cognito_m2m, Y) → B. IAM caller with agent_name=Y must remain unregistered."""
        from src.admin.persona_models.self_routes import get_persona_model_current_user

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(
                ServicePrincipal(
                    canonical_service_principal_id="sp-m2m-negative",
                    org_id=TEST_ORG_A,
                    display_name="M2M negative",
                    status="active",
                    approved_by=TEST_ADMIN_USER_ID,
                )
            )
            session.add(
                ServicePrincipalAlias(
                    id=new_uuid(),
                    canonical_service_principal_id="sp-m2m-negative",
                    org_id=TEST_ORG_A,
                    alias_source="cognito_m2m",
                    alias_id="contested-y",
                    is_active=True,
                    registered_by=TEST_ADMIN_USER_ID,
                )
            )
            await session.commit()

        # IAM caller arrives with agent_name "contested-y" — MUST NOT resolve
        iam_ctx = TokenContext(
            user_id="contested-y",
            org_id=TEST_ORG_A,
            team_id="",
            department_id="",
            account_type="service",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            auth_source="iam",
            canonical_alias_source="agent_registry",
        )

        async with factory() as session:
            enriched = await get_persona_model_current_user(iam_ctx, session)

        assert enriched.canonical_service_principal_id == "", (
            "IAM caller resolved a cognito_m2m alias through the dependency — cross-namespace misbinding"
        )

    @pytest.mark.asyncio
    async def test_same_string_two_sources_each_resolves_own_through_dependency(self, engine):
        """Same alias_id under two sources for two principals. Each auth path resolves its own."""
        from src.admin.persona_models.self_routes import get_persona_model_current_user

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            for canonical, alias_source in (("sp-dual-1", "cognito_m2m"), ("sp-dual-2", "agent_registry")):
                session.add(
                    ServicePrincipal(
                        canonical_service_principal_id=canonical,
                        org_id=TEST_ORG_A,
                        display_name=canonical,
                        status="active",
                        approved_by=TEST_ADMIN_USER_ID,
                    )
                )
                session.add(
                    ServicePrincipalAlias(
                        id=new_uuid(),
                        canonical_service_principal_id=canonical,
                        org_id=TEST_ORG_A,
                        alias_source=alias_source,
                        alias_id="dual-contested",
                        is_active=True,
                        registered_by=TEST_ADMIN_USER_ID,
                    )
                )
            await session.commit()

        # Cognito M2M → sp-dual-1
        m2m_ctx = TokenContext(
            user_id="dual-contested",
            org_id=TEST_ORG_A,
            team_id="",
            department_id="",
            account_type="service",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            auth_source="jwt",
            canonical_alias_source="cognito_m2m",
        )
        async with factory() as session:
            enriched_m2m = await get_persona_model_current_user(m2m_ctx, session)
        assert enriched_m2m.canonical_service_principal_id == "sp-dual-1"

        # IAM → sp-dual-2
        iam_ctx = TokenContext(
            user_id="dual-contested",
            org_id=TEST_ORG_A,
            team_id="",
            department_id="",
            account_type="service",
            is_admin=False,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            auth_source="iam",
            canonical_alias_source="agent_registry",
        )
        async with factory() as session:
            enriched_iam = await get_persona_model_current_user(iam_ctx, session)
        assert enriched_iam.canonical_service_principal_id == "sp-dual-2"


# ── Admin alias tenant isolation (review item 4) ─────────────────────────


@pytest.mark.asyncio
async def test_admin_alias_lookup_is_tenant_scoped(client: AsyncClient, engine, seed_data):
    """The admin set_preference route's alias lookup must filter by org_id.

    This test seeds a corrupt cross-tenant alias against the SAME canonical
    target ID that the admin route will look up. If the production ``org_id``
    predicate were removed, the route would select the org-B alias (which has
    a different source) and the audit trail would record the wrong provenance.

    The test asserts through the real route, not by repeating the query in the
    test body — so removing the predicate from routes.py causes a detectable
    provenance failure, not a silently-passing repeat of the defective query.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # Seed a corrupt cross-tenant alias on the SAME canonical principal,
    # with a DIFFERENT alias_source so its provenance is distinguishable.
    # This simulates a data-corruption scenario where an alias row has
    # the wrong org_id — the org_id predicate is the defence against this.
    async with factory() as session:
        session.add(
            ServicePrincipalAlias(
                id=new_uuid(),
                canonical_service_principal_id=TEST_SP_CANONICAL_ID,
                org_id=TEST_ORG_B,
                alias_source="eventbridge",
                alias_id="leaked-alias",
                is_active=True,
                registered_by="admin-b",
            )
        )
        await session.commit()

    # Hit the admin set-preference route as the org-A admin.
    # The fail-closed validator will refuse the write (expected), but the route
    # still executes the alias lookup and validation check BEFORE the validator.
    # We need to verify the alias lookup fetches the org-A alias, not the org-B one.
    _set_context(client, _admin_context())

    with _patch_validator():
        resp = await client.put(
            f"/service-principals/{TEST_SP_CANONICAL_ID}/persona-models/developer",
            json={"model": "us.anthropic.claude-sonnet-4-6"},
        )

    # The patched validator accepts this model, so anything except success is a
    # product defect rather than an acceptable alternate outcome.
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"

    # Verify the audit trail records the exact org-A alias source.
    async with factory() as session:
        audit = (
            await session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.event_type == "persona_model_admin_set",
                    AuditLog.org_id == TEST_ORG_A,
                )
                .order_by(AuditLog.created_at.desc())
            )
        ).first()
    assert audit is not None, "Admin set must be audited"
    assert audit.details.get("principal_source") == "agent_registry", "The admin route must record the in-tenant org-A alias provenance exactly"


# ── Manageable-principal provenance (review item 5) ──────────────────────


@pytest.mark.asyncio
async def test_manageable_principals_reports_truthful_source(engine, seed_data):
    """Every approved alias source must be reported truthfully, not bucketed
    into a fallback label.

    Specifically: eventbridge and github_actions must not appear as
    "service-accounts", which was the previous fallback.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # Register principals with each non-default alias source
    async with factory() as session:
        for alias_source, alias_id, canonical in (
            ("eventbridge", "eb-rule-truth", "sp-eb-truth"),
            ("github_actions", "gha-runner-truth", "sp-gha-truth"),
            ("cognito_m2m", "m2m-client-truth", "sp-m2m-truth"),
        ):
            session.add(
                ServicePrincipal(
                    canonical_service_principal_id=canonical,
                    org_id=TEST_ORG_A,
                    display_name=f"{alias_source} principal",
                    status="active",
                    approved_by=TEST_ADMIN_USER_ID,
                )
            )
            session.add(
                ServicePrincipalAlias(
                    id=new_uuid(),
                    canonical_service_principal_id=canonical,
                    org_id=TEST_ORG_A,
                    alias_source=alias_source,
                    alias_id=alias_id,
                    is_active=True,
                    registered_by=TEST_ADMIN_USER_ID,
                )
            )
        await session.commit()

    from src.admin.persona_models import service as svc

    async with factory() as session:
        result = await svc.list_manageable_service_principals(session, org_id=TEST_ORG_A)

    by_id = {p["canonical_service_principal_id"]: p for p in result}
    assert by_id["sp-eb-truth"]["source"] == "eventbridge", "eventbridge mislabelled"
    assert by_id["sp-gha-truth"]["source"] == "github-actions", "github_actions mislabelled"
    assert by_id["sp-m2m-truth"]["source"] == "cognito-client", "cognito_m2m mislabelled"

    # The seed data's agent_registry alias should also be truthful
    assert by_id[TEST_SP_CANONICAL_ID]["source"] == "agent-registry"


def _cost_row(*, owner_kind: str, owner_id: str, org_id: str = TEST_ORG_A, chain_id: str = "chain-1", amount: str = "1.000000"):
    return UsageLog(
        id=new_uuid(),
        org_id=org_id,
        department_id="",
        team_id="",
        user_id="metered-worker",
        account_type="service",
        model="global.anthropic.claude-sonnet-4-6",
        input_tokens=10,
        output_tokens=5,
        cost_usd=Decimal(amount),
        latency_ms=1,
        status_code=200,
        persona_key="architect",
        preference_owner_kind=owner_kind,
        preference_owner_id=owner_id,
        chain_id=chain_id,
        pricing_confidence="verified",
        pricing_source_kind="database",
        pricing_generation_id=1,
        pricing_pointer_revision=1,
        pricing_snapshot_version="v1",
        pricing_policy_version=1,
    )


@pytest.fixture
async def cost_client(engine, seed_data):
    """Minimal real FastAPI surface, isolated from unrelated legacy routers."""
    from src.admin.persona_models.routes import router as admin_router
    from src.admin.persona_models.self_routes import get_persona_model_current_user
    from src.admin.persona_models.self_routes import router as self_router
    from src.shared.database import get_db

    app = FastAPI()
    app.include_router(self_router)
    app.include_router(admin_router)

    async def override_get_db():
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    context = _human_context()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: context
    app.dependency_overrides[get_persona_model_current_user] = lambda: context
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        http._app = app  # type: ignore[attr-defined]
        yield http


async def test_cost_route_derives_self_owner_and_ignores_injected_owner(cost_client: AsyncClient, engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                _cost_row(owner_kind="human", owner_id=TEST_USER_ID, amount="1.250000"),
                _cost_row(owner_kind="human", owner_id=TEST_OTHER_USER_ID, amount="99.000000"),
            ]
        )
        await session.commit()

    response = await cost_client.get(f"/me/persona-models/costs?principal_id={TEST_OTHER_USER_ID}")
    assert response.status_code == 200
    body = response.json()
    assert (body["principal_kind"], body["principal_id"]) == ("human", TEST_USER_ID)
    assert Decimal(body["amount_usd"]) == Decimal("1.250000")
    assert body["principal_dimension"] == "preference_owner"
    assert "invoice reconciliation is not established" in body["caveat"]


async def test_cost_route_managed_principal_is_admin_and_chain_scoped(cost_client: AsyncClient, engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                _cost_row(owner_kind="service_account", owner_id=TEST_SP_CANONICAL_ID, chain_id="wanted", amount="2.000000"),
                _cost_row(owner_kind="service_account", owner_id=TEST_SP_CANONICAL_ID, chain_id="other", amount="9.000000"),
            ]
        )
        await session.commit()

    _set_context(cost_client, _admin_context())
    response = await cost_client.get(f"/service-principals/{TEST_SP_CANONICAL_ID}/persona-models/costs?chain_id=wanted")
    assert response.status_code == 200
    body = response.json()
    assert body["principal_id"] == TEST_SP_CANONICAL_ID
    assert body["chain_id"] == "wanted"
    assert Decimal(body["amount_usd"]) == Decimal("2.000000")

    _set_context(cost_client, _service_context())
    assert (await cost_client.get(f"/service-principals/{TEST_SP_CANONICAL_ID}/persona-models/costs")).status_code == 403


async def test_cost_route_rejects_cross_tenant_or_unknown_managed_target(cost_client: AsyncClient, engine):
    other_id = "sp-other-tenant-cost"
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ServicePrincipal(
                canonical_service_principal_id=other_id,
                org_id=TEST_ORG_B,
                display_name="Other tenant",
                status="active",
                approved_by="other-admin",
            )
        )
        await session.commit()

    _set_context(cost_client, _admin_context())
    cross_tenant = await cost_client.get(f"/service-principals/{other_id}/persona-models/costs")
    unknown = await cost_client.get("/service-principals/no-such-principal/persona-models/costs")
    assert cross_tenant.status_code == 422
    assert unknown.status_code == 422


@pytest.mark.parametrize("managed", [False, True])
async def test_empty_cost_routes_retain_class_default_context(cost_client, managed):
    if managed:
        _set_context(cost_client, _admin_context())
        path = f"/service-principals/{TEST_SP_CANONICAL_ID}/persona-models/costs"
    else:
        path = "/me/persona-models/costs"
    response = await cost_client.get(path)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "unknown" and result["amount_usd"] is None
    assert result["preferences"]
    assert all(entry["compatibility_class"] and entry["source"] == "system-default" for entry in result["preferences"])
    assert all("class_default_status" in entry for entry in result["preferences"])


@pytest.mark.asyncio
async def test_cli11_registration_receipt_and_revision_readback(client: AsyncClient, engine):
    _set_context(client, _admin_context())
    body = {
        "display_name": "CLI fixture",
        "alias_source": "eventbridge",
        "alias_id": "fixture-event",
        "operation_id": "56240000-0000-4000-8000-000000000001",
    }
    first = await client.post("/service-principals/register", json=body)
    assert first.status_code == 200, first.text
    retry = await client.post("/service-principals/register", json=body)
    assert retry.status_code == 200 and retry.json() == first.json()
    changed = await client.post("/service-principals/register", json={**body, "display_name": "Changed"})
    assert changed.status_code == 409
    principal = first.json()["canonical_service_principal_id"]
    path = f"/service-principals/{principal}"
    before = (await client.get(path + "/identity")).json()
    linked = await client.post(
        path + "/aliases", json={"alias_source": "github_actions", "alias_id": "second", "expected_revision": before["revision"]}
    )
    assert linked.status_code == 200, linked.text
    stale = await client.patch(path + "/status", json={"status": "retired", "expected_revision": before["revision"]})
    assert stale.status_code == 409
    after = (await client.get(path + "/identity")).json()
    retired = await client.patch(path + "/status", json={"status": "retired", "expected_revision": after["revision"]})
    assert retired.status_code == 200
    assert (await client.get(path + "/identity")).json()["status"] == "retired"
    # A later replay cannot remint or reactivate the retired canonical identity.
    replay = await client.post("/service-principals/register", json=body)
    assert replay.status_code == 200 and replay.json()["canonical_service_principal_id"] == principal
    assert (await client.get(path + "/identity")).json()["status"] == "retired"


@pytest.mark.asyncio
async def test_cli11_identity_snapshot_requires_human_admin_and_tenant(client: AsyncClient):
    _set_context(client, _service_context())
    assert (await client.get(f"/service-principals/{TEST_SP_CANONICAL_ID}/identity")).status_code == 403
    _set_context(client, _admin_context(org_id="foreign"))
    assert (await client.get(f"/service-principals/{TEST_SP_CANONICAL_ID}/identity")).status_code in {403, 404}
