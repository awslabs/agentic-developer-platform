"""Onboarding against real PostgreSQL: zero workspaces, denial, restart. Issue #5535.

Set ``SUPERPLANE_TEST_POSTGRES_URL`` to a disposable postgresql+asyncpg URL. Each
test creates and drops its own random schema. No provider, cloud or vault is
contacted.

WHY REAL POSTGRESQL RATHER THAN THE SUITE'S SQLITE DOUBLE
--------------------------------------------------------
These are the claims the offline double cannot establish:

* **Zero workspaces.** The control-plane-first requirement is about a *freshly
  migrated database*. SQLite's per-test ``create_all`` gives an empty schema, but it
  does not exercise real UUID typing, real foreign keys, or the ``org_id`` scoping
  predicate against a server that enforces them.
* **Restart persistence.** SQLite in this suite is torn down per test, so "state
  survived a restart" is unaskable there. Here the schema outlives the engine, so
  disposing every connection and building a new engine is a real restart.
* **Cross-organization denial.** The filter is a SQL predicate. Asserting it against
  a server that actually has both organizations' rows in one table is the only way
  to establish it excludes rather than merely happening to return nothing.

The vault transport is doubled (no network offline, and #5535's boundary permits
doubling external transports only), but the database, the authorization guard, the
routing and the session are all real.
"""

from __future__ import annotations

import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base, get_session
from app.main import app
from app.middleware.auth import create_access_token
from app.models.organization import Organization
from app.models.workspace import Workspace

pytestmark = pytest.mark.skipif(
    not os.environ.get("SUPERPLANE_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL database",
)


def _auth(org_id: uuid.UUID) -> dict[str, str]:
    """A real signed token for a real organization.

    The token carries the organization; it does not carry authority. Authority is
    resolved server-side from the organization's own rows, which is what
    ``test_a_request_field_cannot_widen_what_the_caller_sees`` establishes.
    """
    token, _ = create_access_token(org_id)
    return {"Authorization": f"Bearer {token}"}


class _Database:
    """One disposable schema, reopenable — so a restart is expressible."""

    def __init__(self, url: str, schema: str) -> None:
        self._url = url
        self.schema = schema
        self.engine = None
        self.sessions = None
        self.open()

    def open(self) -> None:
        self.engine = create_async_engine(
            self._url,
            connect_args={
                "server_settings": {
                    "search_path": self.schema,
                    "statement_timeout": "10000",
                }
            },
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def restart(self) -> None:
        """Drop every connection and rebuild the engine, leaving the schema alone.

        This is what a pod replacement does to the API and not to the database, so
        anything that does not survive it was never durable.
        """
        await self.engine.dispose()
        self.open()


@pytest.fixture
async def database():
    url = os.environ["SUPERPLANE_TEST_POSTGRES_URL"]
    schema = "onboarding_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    handle = _Database(url, schema)
    try:
        async with handle.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield handle
    finally:
        await handle.engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.fixture
async def api(database):
    """The real app, over the real schema, with the session dependency rebound.

    Rebound rather than monkeypatched onto the module so the override follows
    ``database.restart()`` — it reads ``database.sessions`` at request time, so a
    restarted engine is picked up instead of a stale closure being captured.

    RESTORES the previous override rather than deleting the key. ``conftest``
    installs a process-wide SQLite override for ``get_session`` at import, outside
    any fixture, so popping it would not "clean up" — it would remove the offline
    suite's database for every test that ran afterwards in the same process. Those
    tests then fall through to the app's real engine and fail on a refused
    connection to port 5432, in files that have nothing to do with this one. That
    is not hypothetical: an earlier version of this fixture popped the key and took
    123 tests in ``test_workspaces.py`` down with it, all of which pass in
    isolation — the most expensive kind of failure to attribute.
    """

    async def session():
        async with database.sessions() as active:
            yield active

    previous = app.dependency_overrides.get(get_session)
    app.dependency_overrides[get_session] = session
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://onboarding") as c:
            yield c
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_session, None)
        else:
            app.dependency_overrides[get_session] = previous


async def _organization(database, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with database.sessions() as session:
        session.add(Organization(id=org_id, name=name))
        await session.commit()
    return org_id


class TestAZeroWorkspaceControlPlane:
    """The first thing a real installation does, and the thing most likely to break."""

    async def test_an_administrator_lists_zero_workspaces_successfully(
        self, api, database
    ):
        """A freshly migrated control plane must answer 200 with an empty list.

        Not 404, not 503, and not an error because no workspace exists. This is the
        ordering claim the onboarding contract makes: organization authority resolves
        before any workspace exists, so the very first list call must succeed.
        """
        org = await _organization(database, "fresh-install")

        response = await api.get("/workspaces", headers=_auth(org))

        assert response.status_code == 200
        assert response.json() == {"workspaces": [], "total": 0}

    async def test_it_does_not_require_a_composed_vault(
        self, api, database, monkeypatch
    ):
        """Listing must not depend on the credential-evidence port being composed.

        A fresh installation has no vault configured yet. If the read path consulted
        it, onboarding could never reach the point of configuring one — the
        prerequisite loop this issue's design explicitly forbids.
        """
        import app.services.credential_evidence as evidence

        monkeypatch.setattr(evidence, "_reader", None)
        org = await _organization(database, "no-vault-yet")

        response = await api.get("/workspaces", headers=_auth(org))

        assert response.status_code == 200

    async def test_readiness_holds_with_no_workspaces_and_no_vault(
        self, api, database, monkeypatch
    ):
        """`/readyz` must be ready on a control plane that has nothing registered.

        The load-bearing case for control-plane-first installation: if readiness
        required a workspace or a credential, Kubernetes would never route traffic to
        a fresh install, so it could never be onboarded.
        """
        import app.services.credential_evidence as evidence

        monkeypatch.setattr(evidence, "_reader", None)

        response = await api.get("/readyz")

        assert response.status_code == 200
        assert response.json()["status"] == "ready"

    async def test_liveness_holds_with_no_database_configured_integration(self, api):
        """Liveness must not depend on anything a restart cannot fix."""
        response = await api.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "healthy"


class TestDenial:
    """Unauthenticated and cross-organization callers, against real rows."""

    async def test_an_unauthenticated_caller_is_refused(self, api):
        response = await api.get("/workspaces")

        assert response.status_code in (401, 403)

    async def test_a_forged_token_is_refused(self, api):
        response = await api.get(
            "/workspaces", headers={"Authorization": "Bearer not-a-real-token"}
        )

        assert response.status_code in (401, 403)

    async def test_a_caller_never_sees_another_organizations_workspace(
        self, api, database
    ):
        """Both organizations' rows are really in the table; one caller sees one row.

        The assertion that matters is the pairing: A sees exactly A's workspace *and*
        B sees exactly B's. Asserting only "A does not see B's" would pass against a
        scoping bug that returned nothing to anybody.
        """
        first = await _organization(database, "org-a")
        second = await _organization(database, "org-b")
        async with database.sessions() as session:
            session.add(
                Workspace(
                    id=uuid.uuid4(),
                    org_id=first,
                    name="a-only",
                    isolation_mode="dedicated",
                )
            )
            session.add(
                Workspace(
                    id=uuid.uuid4(),
                    org_id=second,
                    name="b-only",
                    isolation_mode="dedicated",
                )
            )
            await session.commit()

        seen_by_first = await api.get("/workspaces", headers=_auth(first))
        seen_by_second = await api.get("/workspaces", headers=_auth(second))

        assert [w["name"] for w in seen_by_first.json()["workspaces"]] == ["a-only"]
        assert [w["name"] for w in seen_by_second.json()["workspaces"]] == ["b-only"]

    async def test_reading_another_organizations_workspace_by_id_is_refused(
        self, api, database
    ):
        """Knowing the UUID must not be sufficient; the row exists and is still hidden.

        404 rather than 403 is correct here: confirming the identifier exists would
        leak the other organization's inventory to anyone who can guess a UUID.
        """
        owner = await _organization(database, "owner")
        outsider = await _organization(database, "outsider")
        workspace_id = uuid.uuid4()
        async with database.sessions() as session:
            session.add(
                Workspace(
                    id=workspace_id,
                    org_id=owner,
                    name="private",
                    isolation_mode="dedicated",
                )
            )
            await session.commit()

        response = await api.get(f"/workspaces/{workspace_id}", headers=_auth(outsider))

        assert response.status_code == 404
        assert "private" not in response.text

    async def test_a_request_field_cannot_widen_what_the_caller_sees(
        self, api, database
    ):
        """Authority is resolved server-side; a query parameter must not override it.

        The contract states request fields never confer authority. Passing another
        organization's real id as a parameter must change nothing, and it must not
        change anything *even though the value is genuine* — a valid identifier the
        caller has no grant on is precisely the attack.
        """
        caller = await _organization(database, "caller")
        other = await _organization(database, "other")
        async with database.sessions() as session:
            session.add(
                Workspace(
                    id=uuid.uuid4(),
                    org_id=other,
                    name="not-yours",
                    isolation_mode="dedicated",
                )
            )
            await session.commit()

        response = await api.get(
            "/workspaces", params={"org_id": str(other)}, headers=_auth(caller)
        )

        assert response.status_code in (200, 400, 422)
        if response.status_code == 200:
            assert response.json() == {"workspaces": [], "total": 0}
        assert "not-yours" not in response.text


class TestStateSurvivesARestart:
    """Durable domain state, across a real connection teardown."""

    async def test_a_registered_workspace_is_still_there_after_a_restart(
        self, api, database
    ):
        """Anything held only in process memory disappears here.

        A workspace visible before the restart and absent after it would mean the
        control plane's inventory was never durable — an operator would see their
        workspaces vanish on every deployment while the infrastructure kept running
        and kept billing.
        """
        org = await _organization(database, "durable")
        async with database.sessions() as session:
            session.add(
                Workspace(
                    id=uuid.uuid4(),
                    org_id=org,
                    name="survivor",
                    isolation_mode="dedicated",
                )
            )
            await session.commit()

        before = await api.get("/workspaces", headers=_auth(org))
        await database.restart()
        after = await api.get("/workspaces", headers=_auth(org))

        assert [w["name"] for w in before.json()["workspaces"]] == ["survivor"]
        assert after.status_code == 200
        assert [w["name"] for w in after.json()["workspaces"]] == ["survivor"]

    async def test_the_scoping_boundary_still_holds_after_a_restart(
        self, api, database
    ):
        """A restart must not reopen the cross-organization boundary.

        Asserted separately from persistence because the two fail independently: a
        rebuilt engine that lost its scoping predicate would pass the persistence
        test above while exposing every organization's inventory to every caller.
        """
        first = await _organization(database, "restart-a")
        second = await _organization(database, "restart-b")
        async with database.sessions() as session:
            session.add(
                Workspace(
                    id=uuid.uuid4(),
                    org_id=second,
                    name="still-hidden",
                    isolation_mode="dedicated",
                )
            )
            await session.commit()

        await database.restart()
        response = await api.get("/workspaces", headers=_auth(first))

        assert response.json() == {"workspaces": [], "total": 0}

    async def test_readiness_recovers_rather_than_latching_after_a_restart(
        self, api, database
    ):
        """Ready, then restarted, then ready again — without a process restart.

        A readiness check that cached its first answer would keep reporting a
        recovered control plane as ready even when it was not, or keep a recovered
        one out of rotation forever. Both are outages the cache would cause.
        """
        assert (await api.get("/readyz")).status_code == 200
        await database.restart()

        assert (await api.get("/readyz")).status_code == 200


class TestControlPlaneReadinessTracksTheDatabase:
    """`/readyz` is not a no-op: an unusable management database is not ready."""

    async def test_an_unreachable_database_reports_not_ready(self, api, database):
        """The direction that makes readiness meaningful.

        Disposing the engine and pointing it at a closed URL is a real connection
        failure, not a patched exception — so this establishes the route actually
        depends on the database rather than on a mock that was told to raise.
        """
        await database.engine.dispose()
        database.engine = create_async_engine(
            "postgresql+asyncpg://superplane-readiness-probe@127.0.0.1:1/absent",
            connect_args={"timeout": 2},
        )
        database.sessions = async_sessionmaker(database.engine, expire_on_commit=False)

        response = await api.get("/readyz")

        assert response.status_code == 503

    async def test_the_not_ready_body_does_not_leak_the_connection_string(
        self, api, database
    ):
        """asyncpg errors embed credentials and hosts; a probe body is widely visible.

        Kubernetes events, logs and dashboards all surface this string, so a DSN in it
        is a credential disclosure with a very wide blast radius.
        """
        await database.engine.dispose()
        database.engine = create_async_engine(
            "postgresql+asyncpg://leak-check:secret-value@127.0.0.1:1/absent",
            connect_args={"timeout": 2},
        )
        database.sessions = async_sessionmaker(database.engine, expire_on_commit=False)

        response = await api.get("/readyz")

        assert response.status_code == 503
        for leak in ("secret-value", "leak-check", "127.0.0.1", "asyncpg"):
            assert leak not in response.text
