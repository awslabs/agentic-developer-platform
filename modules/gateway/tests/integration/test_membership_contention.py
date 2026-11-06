"""Membership authorization must not serialize readers or retain SQL over I/O."""

import asyncio
import threading
from contextlib import suppress
from types import SimpleNamespace

import psycopg2
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg2 import sql
from sqlalchemy import select, text

from src.admin import team_memberships
from src.admin.exceptions import ResourceNotFoundError, SecondPrimaryTeamError
from src.admin.membership_revocation import revoke_membership
from src.orchestration import reviewer_checks
from src.orchestration.models import OrchestrationAcceptedPlan
from src.orchestration.policy_admission import _member_facts, resolve_authorization_context
from src.orchestration.run_reports import OrchestrationRunReport
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, Team, TeamMembership, User
from tests.integration import test_performance_contention
from tests.orchestration import test_shared_cycle
from tests.orchestration.test_merge_controller import prepared_merge
from tests.orchestration.test_review_cycle import ORG, tick
from tests.orchestration.test_reviewer_checks import observation

pg_sessions = test_performance_contention.pg_sessions
pg_server = test_shared_cycle.pg_server
pg_url = test_shared_cycle.pg_url
cycle = test_shared_cycle.cycle
shared = test_shared_cycle.shared
store = test_shared_cycle.store


@pytest.fixture
async def members(pg_sessions, pg_url):
    factory, _, role, _ = pg_sessions
    with psycopg2.connect(pg_url) as bootstrap:
        with bootstrap.cursor() as cursor:
            cursor.execute(
                sql.SQL("GRANT INSERT, UPDATE, DELETE ON organizations, teams, users, tenant_memberships, team_memberships TO {}").format(
                    sql.Identifier(role)
                )
            )
            cursor.execute(sql.SQL("GRANT UPDATE ON orchestration_nodes TO {}").format(sql.Identifier(role)))
    async with factory() as session:
        assert not await session.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"))
        session.add_all([Organization(id=ORG, name=ORG), Organization(id="unrelated", name="unrelated")])
        await session.flush()
        session.add_all(
            [
                Team(id=team, org_id=org, department_id="department", name=team)
                for team, org in (("first", ORG), ("second", ORG), ("other", "unrelated"))
            ]
            + [User(id=user, org_id=org, team_id="", email=f"{user}@example.invalid") for user, org in (("human", ORG), ("outsider", "unrelated"))]
        )
        await session.flush()
        session.add_all(
            [
                TenantMembership(user_id=user, tenant_id=org, role="member", is_active=True)
                for user, org in (("human", ORG), ("outsider", "unrelated"))
            ]
        )
        await session.commit()
    yield factory
    assert factory.kw["bind"].pool.checkedout() == 0


async def test_independent_same_caller_policy_reads_do_not_wait_on_membership_reader(members):
    async with members() as first, members() as second, members() as unrelated:
        first_pid = await first.scalar(text("SELECT pg_backend_pid()"))
        second_pid = await second.scalar(text("SELECT pg_backend_pid()"))
        assert first_pid != second_pid
        assert await team_memberships.list_memberships(first, user_id="human", org_id=ORG) == []
        assert await asyncio.wait_for(_member_facts(unrelated, user_id="outsider", org_id="unrelated"), 2) == ("unrelated", frozenset())
        assert await asyncio.wait_for(_member_facts(second, user_id="human", org_id=ORG), 2) == (ORG, frozenset())
        assert await second.scalar(text("SELECT pg_blocking_pids(pg_backend_pid())")) == []


@pytest.mark.parametrize("case", ["missing", "cross_tenant", "revoked"])
async def test_membership_policy_denies_missing_cross_tenant_and_revoked_users(members, case):
    if case == "revoked":
        async with members() as session:
            await revoke_membership(session, org_id=ORG, user_id="human")
    user = {"missing": "absent", "cross_tenant": "outsider", "revoked": "human"}[case]
    async with members() as session:
        assert await _member_facts(session, user_id=user, org_id=ORG) == (None, frozenset())
        with pytest.raises(ResourceNotFoundError):
            await team_memberships.list_memberships(session, user_id=user, org_id=ORG)


async def wait_for_blocker(session, waiter_pid, owner_pid):
    async with asyncio.timeout(3):
        while owner_pid not in await session.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid}):
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("owner_action", ["add", "revoke", "cancel"])
async def test_membership_writers_serialize_and_recheck_revocation_after_wait(members, owner_action):
    started = asyncio.Event()
    waiter_pid = None

    async def competing_writer():
        nonlocal waiter_pid
        async with members() as session:
            waiter_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            started.set()
            await team_memberships.add_membership(session, user_id="human", org_id=ORG, team_id="second", is_primary=True)
            await session.commit()

    async with members() as owner, members() as observer:
        owner_pid = await owner.scalar(text("SELECT pg_backend_pid()"))
        await owner.execute(select(User).where(User.id == "human").with_for_update())
        waiting = asyncio.create_task(competing_writer())
        try:
            await asyncio.wait_for(started.wait(), 2)
            await wait_for_blocker(observer, waiter_pid, owner_pid)
            assert await asyncio.wait_for(_member_facts(observer, user_id="outsider", org_id="unrelated"), 2) == ("unrelated", frozenset())
            if owner_action == "add":
                await team_memberships.add_membership(owner, user_id="human", org_id=ORG, team_id="first", is_primary=True)
                await owner.commit()
                with pytest.raises(SecondPrimaryTeamError):
                    await asyncio.wait_for(waiting, 2)
            elif owner_action == "revoke":
                await revoke_membership(owner, user_id="human", org_id=ORG)
                with pytest.raises(ResourceNotFoundError):
                    await asyncio.wait_for(waiting, 2)
            else:
                waiting.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiting
                await owner.rollback()
                await asyncio.wait_for(competing_writer(), 2)
        finally:
            waiting.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await waiting
    async with members() as session:
        rows = list(await session.scalars(select(TeamMembership).where(TeamMembership.user_id == "human")))
        expected = {"add": ["first"], "revoke": [], "cancel": ["second"]}[owner_action]
        assert [row.team_id for row in rows] == expected
        assert all(row.is_primary for row in rows)


@pytest.mark.parametrize("change", ["none", "revoked", "team_removed", "cancel"])
@pytest.mark.parametrize("wait_at", ["token", "observe"])
async def test_reviewer_checks_release_sql_and_revalidate_after_provider_wait(shared, members, monkeypatch, change, wait_at):
    await tick(shared)
    if change == "team_removed":
        async with shared.factory() as session:
            plan = await session.get(OrchestrationAcceptedPlan, shared.plan.id)
            document = dict(plan.plan_document)
            document["execution_policy"] = {**document["execution_policy"], "team_ids": ["first"]}
            plan.plan_document = document
            await team_memberships.add_membership(session, org_id=ORG, user_id="human", team_id="first")
            await session.commit()
    envelope = shared.calls[-1]
    monkeypatch.setattr("src.orchestration.shared_policy.resolve_authorization_context", resolve_authorization_context)
    monkeypatch.setattr(reviewer_checks, "get_session_factory", lambda: members)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def barrier():
        if entered.is_set():
            return
        entered.set()
        await release.wait()

    async def token(*args, **kwargs):
        if wait_at == "token":
            await barrier()
        return "fictional-token"

    async def observe(*args, **kwargs):
        if wait_at == "observe":
            await barrier()
        return observation(shared.head)

    monkeypatch.setattr(reviewer_checks, "MergeProvider", lambda: SimpleNamespace(token=token))
    monkeypatch.setattr(reviewer_checks.GitHubMergeObserver, "observe", observe)
    app = FastAPI()
    app.post("/checks")(reviewer_checks.review_checks)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture") as client:
        task = asyncio.create_task(
            client.post("/checks", json={"head_sha": shared.head}, headers={"X-Adp-Report-Credential": envelope["run_report"]["credential"]})
        )
        try:
            async with asyncio.timeout(3):
                while not entered.is_set():
                    if task.done():
                        response = await task
                        pytest.fail(f"Provider was not reached: {response.status_code} {response.text}")
                    await asyncio.sleep(0.01)
            assert members.kw["bind"].pool.checkedout() == 0
            healthy = await asyncio.wait_for(
                client.post("/checks", json={"head_sha": shared.head}, headers={"X-Adp-Report-Credential": envelope["run_report"]["credential"]}),
                3,
            )
            assert healthy.status_code == 200
            assert healthy.json()["state"] == "passed"
            assert not task.done()
            async with members() as independent:
                expected_teams = frozenset({"first"}) if change == "team_removed" else frozenset()
                assert await asyncio.wait_for(_member_facts(independent, user_id="human", org_id=ORG), 2) == (ORG, expected_teams)
                assert await _member_facts(independent, user_id="outsider", org_id="unrelated") == ("unrelated", frozenset())
                if change == "revoked":
                    await revoke_membership(independent, org_id=ORG, user_id="human")
                elif change == "team_removed":
                    await team_memberships.remove_membership(independent, org_id=ORG, user_id="human", team_id="first")
                    await independent.commit()
            if change == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                release.set()
                response = await asyncio.wait_for(task, 3)
                assert response.status_code == (409 if change in {"revoked", "team_removed"} else 200)
                if change == "none":
                    assert response.json()["state"] == "passed"
        finally:
            release.set()
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
    assert members.kw["bind"].pool.checkedout() == 0


@pytest.mark.parametrize("revoke_during_read", [0, 1, 2])
async def test_merge_review_storage_wait_releases_sql_and_rechecks_authority(shared, members, monkeypatch, revoke_during_read):
    from src.orchestration import merge_review
    from src.orchestration.merge_evidence import GitHubMergeObserver
    from src.orchestration.review_cycle import CycleBlockedError

    async with prepared_merge(shared, monkeypatch, reviewer_owned=True) as ctx:
        async with ctx.factory() as setup:
            report = await setup.get(OrchestrationRunReport, ctx.calls[-1]["message_id"])
            report.terminal_receipt = None
            report.worker_receipt = {"ownership_nonce": "a" * 32}
            await setup.commit()
        monkeypatch.setattr("src.orchestration.shared_policy.resolve_authorization_context", resolve_authorization_context)
        observed = await GitHubMergeObserver(ctx.merge_services.provider.client, "scoped-test-token", ctx.merge_services.provider.clock).observe(
            ctx.binding
        )

        async def observe(*args):
            assert members.kw["bind"].pool.checkedout() == 0
            return observed

        monkeypatch.setattr(GitHubMergeObserver, "observe", observe)
        read_document = merge_review.read_review_document
        reads = 0
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def read_without_transaction(*args, **kwargs):
            nonlocal reads
            reads += 1
            assert members.kw["bind"].pool.checkedout() == 0
            if reads == revoke_during_read:
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(5)
            return read_document(*args, **kwargs)

        monkeypatch.setattr(merge_review, "read_review_document", read_without_transaction)
        async with members() as session:
            task = asyncio.create_task(
                reviewer_checks.observe_reviewer_checks(
                    session,
                    credential=ctx.calls[-1]["run_report"]["credential"],
                    head_sha=ctx.head,
                    for_merge=True,
                    provider=ctx.merge_services.provider,
                    storage=ctx.merge_services.storage,
                )
            )
            try:
                if revoke_during_read:
                    async with asyncio.timeout(5):
                        while not entered.is_set():
                            if task.done():
                                await task
                                pytest.fail("Storage read was not reached")
                            await asyncio.sleep(0.01)
                    async with members() as revoking:
                        await revoke_membership(revoking, org_id=ORG, user_id="human")
                    release.set()
                    with pytest.raises(CycleBlockedError):
                        await asyncio.wait_for(task, 5)
                else:
                    assert (await asyncio.wait_for(task, 5))["state"] == "passed"
                    assert reads == 2
            finally:
                release.set()
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
