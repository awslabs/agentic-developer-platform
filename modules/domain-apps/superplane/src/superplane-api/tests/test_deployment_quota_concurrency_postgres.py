"""Concurrent GPU quota reservation against real PostgreSQL row locking.

Issue #5671 (A15), finding ``f-96315bea-5e27-4d48-aacb-ca91301cf971``. The acceptance
criteria require quota to be enforced atomically "including concurrent requests", and
that guarantee rests entirely on ``reserve_deployment_gpus`` taking a ``FOR UPDATE`` lock
on the workspace row.

WHY THIS FILE EXISTS SEPARATELY
-------------------------------
The offline suite runs on in-memory SQLite, where ``with_for_update()`` compiles to
nothing. A concurrency test there passes whether or not the lock is present, so it cannot
distinguish the fix from the defect — the one thing it is supposed to check. This file
follows the module's existing convention for tests that need a real server
(``test_installation_postgres.py``, ``test_controller_management_postgres.py``): skipped
unless ``SUPERPLANE_TEST_POSTGRES_URL`` or ``pgserver`` is available.

WHERE IT RUNS, AND WHY THAT IS NOT OBVIOUS FROM THE LANE
-------------------------------------------------------
It DOES run in ``superplane-domain-ci.yml`` — verified on a real run of that lane, where
all three tests report PASSED rather than skipped. But nothing in the lane mentions
``pgserver``: it arrives transitively, because the job's earlier "Install gateway
dependencies" step installs ``bedrockgateway[dev]``, which depends on it, into the same
environment the API suite then runs in. That also fixes the interpreter — the lane pins
Python 3.12, and ``pgserver`` publishes no 3.13 wheel, so a future bump of the lane to
3.13 would silently turn these three tests into skips.

That is a fragile way to acquire a test dependency and it is worth stating plainly rather
than relying on: if this file ever starts skipping in CI, the cause is upstream of it, in
the gateway's dependency set or the lane's Python version, not in anything here. Run it
deliberately with either:

    pip install pgserver          # Python <= 3.12; no 3.13 wheel exists
    python -m pytest tests/test_deployment_quota_concurrency_postgres.py

    SUPERPLANE_TEST_POSTGRES_URL=postgresql+asyncpg://... python -m pytest ...

The source-level lock assertion in ``test_deployment_quota_and_isolation.py`` is the
backstop for the skip case: it fails if ``with_for_update()`` is removed even where no
database is available to demonstrate the consequence.

The service layer is called directly rather than through HTTP. The contended resource is
the workspace row, and going through the app would add its own session management between
the test and the lock without testing anything extra.
"""

import asyncio
import importlib.util
import os
import uuid

import pytest
from app.database import Base
from app.models.cluster import Cluster
from app.models.deployment import Deployment
from app.models.organization import Organization
from app.models.workspace import Workspace
from app.services.quota import RELEASED_DEPLOYMENT_STATUSES, reserve_deployment_gpus
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.skipif(
    not os.environ.get("SUPERPLANE_TEST_POSTGRES_URL")
    and importlib.util.find_spec("pgserver") is None,
    reason="requires pgserver (arrives transitively via bedrockgateway[dev] in the "
    "domain CI lane; Python <= 3.12 only) or SUPERPLANE_TEST_POSTGRES_URL",
)

ORG_ID = uuid.UUID("aaaaaaaa-0000-0000-0000-00000000000a")
CLUSTER_ID = uuid.UUID("cccccccc-0000-0000-0000-00000000000c")
WS_ID = uuid.UUID("aaaa1111-1111-1111-1111-11111111111a")
BUDGET_GPUS = 4


@pytest.fixture(scope="module")
def postgres_url(tmp_path_factory):
    external = os.environ.get("SUPERPLANE_TEST_POSTGRES_URL")
    if external:
        yield external
        return
    import pgserver

    server = pgserver.get_server(tmp_path_factory.mktemp("superplane-quota-pg"))
    try:
        yield server.get_uri().replace("postgresql://", "postgresql+asyncpg://", 1)
    finally:
        server.cleanup()


@pytest.fixture
async def session_factory(postgres_url):
    """A schema-isolated session factory, so a failed run cannot poison the next.

    Each test gets its own schema rather than sharing tables, because the whole point
    here is contention: leftover deployment rows from a previous test would change the
    headroom the next one starts with.
    """
    schema = "quota_" + uuid.uuid4().hex[:16]
    admin = create_async_engine(postgres_url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_async_engine(
        postgres_url, connect_args={"server_settings": {"search_path": schema}}
    )
    async with engine.begin() as conn:
        tables = [
            t
            for t in Base.metadata.sorted_tables
            if not t.info.get("postgresql_bootstrap_journal")
        ]
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def _seed(factory, *, budget_gpus: int = BUDGET_GPUS) -> None:
    async with factory() as session:
        session.add(Organization(id=ORG_ID, name="org-a", billing_plan="enterprise"))
        await session.flush()
        session.add(
            Cluster(
                id=CLUSTER_ID,
                org_id=ORG_ID,
                name="shared",
                cloud_provider="aws",
                cluster_type="eks",
                eks_cluster_arn="arn:aws:eks:eu-west-1:123456789012:cluster/shared",
                status="Active",
            )
        )
        await session.flush()
        session.add(
            Workspace(
                id=WS_ID,
                org_id=ORG_ID,
                name="alpha",
                isolation_mode="namespace",
                cluster_id=CLUSTER_ID,
                namespace_name="ws-alpha",
                budget_max_gpus=budget_gpus,
                status="Active",
            )
        )
        await session.commit()


def _kwargs(name: str, gpus: int) -> dict:
    return {
        "cluster_id": CLUSTER_ID,
        "name": name,
        "namespace": "ws-alpha",
        "model_name": "meta-llama/Llama-3.1-8B-Instruct",
        "desired_replicas": 1,
        "gpu_per_replica": gpus,
    }


async def _reserve(factory, name: str, gpus: int):
    """Reserve in its own session, so each attempt is a genuinely separate transaction.

    Returns the ``Deployment`` on success or the ``HTTPException`` on a refusal. Only
    ``HTTPException`` is caught: any other error means the reservation path itself broke,
    and swallowing it would let that surface as "refused", making a crash indistinguish-
    able from correct enforcement.
    """
    async with factory() as session:
        try:
            return await reserve_deployment_gpus(
                WS_ID,
                ORG_ID,
                gpus,
                session,
                deployment_kwargs=_kwargs(name, gpus),
            )
        except HTTPException as exc:
            return exc


async def _committed_gpus(factory) -> int:
    async with factory() as session:
        rows = (
            await session.execute(
                select(Deployment).where(Deployment.workspace_id == WS_ID)
            )
        ).scalars()
        return sum(
            (row.desired_replicas or 0) * (row.gpu_per_replica or 0)
            for row in rows
            if row.status not in RELEASED_DEPLOYMENT_STATUSES
        )


async def test_two_concurrent_reservations_cannot_exceed_the_budget(session_factory):
    """Two requests for the full budget, at once: exactly one is admitted.

    This is the case the row lock exists for. Without it both transactions read the same
    pre-request total of 0, both conclude 4 <= 4, and the workspace ends up holding 8 GPUs
    against a 4-GPU budget with neither request individually at fault.
    """
    await _seed(session_factory)

    results = await asyncio.gather(
        _reserve(session_factory, "dep-one", BUDGET_GPUS),
        _reserve(session_factory, "dep-two", BUDGET_GPUS),
    )

    admitted = [r for r in results if isinstance(r, Deployment)]
    refused = [r for r in results if not isinstance(r, Deployment)]

    assert len(admitted) == 1, f"expected exactly one admission, got {results}"
    assert len(refused) == 1
    assert getattr(refused[0], "status_code", None) == 429, refused[0]
    assert await _committed_gpus(session_factory) == BUDGET_GPUS


async def test_many_concurrent_reservations_admit_only_what_fits(session_factory):
    """Eight simultaneous 1-GPU requests against a 4-GPU budget: four are admitted.

    The stronger form of the same property — the lock must serialise every contender, not
    just protect against a single racing pair, and the total committed must land exactly
    on the budget rather than somewhere near it.
    """
    await _seed(session_factory)

    results = await asyncio.gather(
        *(_reserve(session_factory, f"dep-{i}", 1) for i in range(8))
    )

    admitted = [r for r in results if isinstance(r, Deployment)]
    assert len(admitted) == BUDGET_GPUS, (
        f"admitted {len(admitted)} of 8 against a {BUDGET_GPUS}-GPU budget"
    )
    assert await _committed_gpus(session_factory) == BUDGET_GPUS

    for refusal in (r for r in results if not isinstance(r, Deployment)):
        assert getattr(refusal, "status_code", None) == 429, refusal


async def test_a_released_reservation_frees_headroom_for_a_concurrent_retry(
    session_factory,
):
    """Capacity released by a failed provisioning attempt is reusable.

    Pairs with the rollback test in the offline suite: that one proves the release
    happens, this one proves what it releases is genuinely available again under real
    locking rather than left invisibly held by an uncommitted transaction.
    """
    from app.services.quota import release_deployment_reservation

    await _seed(session_factory)

    first = await _reserve(session_factory, "doomed", BUDGET_GPUS)
    assert isinstance(first, Deployment), first

    blocked = await _reserve(session_factory, "blocked", 1)
    assert not isinstance(blocked, Deployment), "budget was not actually held"

    async with session_factory() as session:
        held = await session.get(Deployment, first.id)
        await release_deployment_reservation(held, session)

    retry = await _reserve(session_factory, "retry", BUDGET_GPUS)
    assert isinstance(retry, Deployment), f"released capacity was not reusable: {retry}"
    assert await _committed_gpus(session_factory) == BUDGET_GPUS
