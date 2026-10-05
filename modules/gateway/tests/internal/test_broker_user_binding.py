"""The broker refuses a human the run's own record does not name — #5663 (A09).

Finding ``f-7c46ead6-06bf-4726-94a7-b09ea88efbb5``, gateway half. The issue's
Validation section asks, in as many words, for "a test asserting the credential
broker refuses to release material for a human the run's own record does not
legitimately name". This is the endpoint of the escalation whose ingestion half is
closed in ``modules/agent-factory/webhook-ingress/lambda/github/`` — an agent worker
influenced by outsider text arranges to be recorded as authorised by someone else,
and then asks the broker for that person's vault material.

Why this needs its own module even though ``verify_broker_worker`` already contains
the comparison: nothing asserted it. A refusal that exists only as three lines of
code in a 100-line dependency is one refactor away from being dropped, and the
failure would be silent — the route would keep returning 200 with a credential, just
someone else's. The issue is explicit that where current code already meets a
criterion the deliverable is *evidence*, not a rewrite. So these tests drive the
REAL ``verify_broker_worker`` against a mounted route and assert the observable
outcome (HTTP status, and whether STS was called at all), not a mocked helper's
return value.

What is deliberately NOT stubbed: ``verify_broker_worker`` itself, the FastAPI
dependency wiring, and ``authorize_worker_credential``. What is stubbed is the
environment a unit test cannot have — the pod verifier, the DynamoDB authority/event
store, Secrets Manager and STS. The refusal under test happens strictly before any
of those effects, which is why every refusal case below also asserts that STS
``assume_role`` and Secrets Manager were never reached — a 404 that still read the
secret would be a leak with a tidy status code.

Negative verification (recorded because it is not reproducible from the file alone):
neutralising the comparison in ``broker_identity.verify_broker_worker`` turns the
three :class:`TestBrokerRefusesUnnamedHuman` identity cases red, so they are bound to
that control rather than to the fixtures.

The shape of the refusal (404, not 403) is the pre-existing #3985 convention:
distinguishing "refused" from "no such thing" on an internal plane is itself an
oracle. That is asserted rather than assumed, because a well-meaning change to 403
would leak exactly what the convention exists to hide.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.agentauth.broker_identity import verify_broker_worker
from src.agentauth.grants import AuthorityReference, DelegatedGrant
from src.internal.assume_role_routes import get_secrets_manager, router
from src.internal.auth_deps import verify_internal_or_irsa
from src.orchestration.models import OrchestrationAcceptedPlan  # noqa: F401 — register before create_all
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import UserCredential

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

ORG = "org-acme"
# The human the run's own webhook-events row names. Their credential is the one the
# broker may release to this run.
OWNER = "user-alice"
# A different real human in the same org. This is the interesting adversary: not a
# fabricated id (which would fail a foreign key and prove nothing about authority)
# but someone who genuinely exists and genuinely has stored credentials.
VICTIM = "user-bob"
INVOCATION = "inv-run-under-test"
ARRIVED_AT = "2026-09-20T10:00:00Z"

_ROLE_SECRET_JSON = json.dumps(
    {
        "role_arn": "arn:aws:iam::123456789012:role/ADPDeployAgent",
        "external_id": "adp-dev-hosted-agent",
        "session_duration_seconds": 1800,
        "default_region": "us-west-2",
    }
)


@pytest.fixture
async def db() -> AsyncSession:
    engine = create_async_engine(
        TEST_DB_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        import src.shared.models.audit  # noqa: F401
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                Organization(
                    id=ORG,
                    name="Acme",
                    aws_accounts=[],
                    role_mappings={},
                    settings={},
                    github_installation_ids=[],
                    cognito_client_ids=[],
                ),
                Department(id="dept-eng", org_id=ORG, name="Engineering"),
                Team(id="team-eng", org_id=ORG, department_id="dept-eng", name="Eng"),
                User(id=OWNER, org_id=ORG, team_id="team-eng", email="alice@acme.test"),
                User(id=VICTIM, org_id=ORG, team_id="team-eng", email="bob@acme.test"),
            ]
        )
        await session.commit()
        # Both humans have a real aws_role credential, so a refusal can only come
        # from the identity binding and never from "the credential is missing".
        for owner in (OWNER, VICTIM):
            session.add(
                UserCredential(
                    user_id=owner,
                    org_id=ORG,
                    service="aws",
                    label="prod",
                    credential_type="aws_role",
                    secret_arn=f"arn:aws:secretsmanager:us-east-1:111122223333:secret:{owner}-role",
                )
            )
        await session.commit()
        yield session
    await engine.dispose()


def _settings() -> MagicMock:
    s = MagicMock()
    s.internal_api_key = "unused-here"
    s.aws_region = "us-east-1"
    s.enforce_credential_binding = False
    s.webhook_events_table = "adp-test-webhook-events"
    return s


def _grant() -> DelegatedGrant:
    return DelegatedGrant(
        grant_id="grant:run-under-test:1",
        tenant_id=ORG,
        principal=f"{INVOCATION}#1",
        authority=AuthorityReference("github_event", "verified-github-event", OWNER, ORG),
        allowed_actions=frozenset(),
        flow_id="flow-under-test",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )


def _runtime(*, authorized_user_id: str | None, db: AsyncSession) -> SimpleNamespace:
    """A runtime whose event store returns the run's OWN server-written row.

    ``authorized_user_id`` is the human that row names — i.e. the only human this
    run's own record legitimately entitles it to. ``None`` models a row on which the
    attribute is absent entirely, which is the case that must not widen anything.
    """
    item: dict = {}
    if authorized_user_id is not None:
        item["authorized_user_id"] = {"S": authorized_user_id}
    event_client = MagicMock()
    event_client.get_item.return_value = {"Item": item} if item else {}
    caller = SimpleNamespace(tenant_id=ORG, invocation_id=INVOCATION, principal=f"{INVOCATION}#1")
    return SimpleNamespace(
        authenticate=lambda *_: (None, caller, None, _grant()),
        validate_flow=AsyncMock(),
        store=SimpleNamespace(_read=lambda *_: {"arrived_at": {"S": ARRIVED_AT}}, client=event_client),
        _event_client=event_client,
    )


def _client(db: AsyncSession, mock_sm: MagicMock) -> TestClient:
    """Mount the real route with the REAL broker dependency in the auth slot."""
    app = FastAPI()
    app.include_router(router)

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_secrets_manager] = lambda: mock_sm

    # The substitution the live app performs for BROKER_PATHS when a worker presents
    # agent-authority headers. Overriding this rather than stubbing the dependency is
    # what makes these tests exercise a real authorization decision.
    async def verified_transport(request: Request):
        request.state.token_context = SimpleNamespace(user_id="test-worker", credential_scopes=["credential:assume-role"])
        await verify_broker_worker(request)

    app.dependency_overrides[verify_internal_or_irsa] = verified_transport
    return TestClient(app, raise_server_exceptions=False)


class _Session:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_):
        return False


def _request(user_id: str) -> dict:
    return {
        "user_id": user_id,
        "agent_id": "developer",
        "task_id": "task-1",
        "service": "aws",
        "label": "prod",
        "invocation_id": INVOCATION,
    }


class TestBrokerRefusesUnnamedHuman:
    """The comparison is against the run's own record, not against the request."""

    @pytest.mark.asyncio
    async def test_request_for_another_human_is_refused(self, db, monkeypatch):
        """THE PROPERTY: the run's row names Alice; asking for Bob's is refused.

        Bob exists and has a real credential, so nothing but the identity binding can
        produce this refusal.
        """
        runtime = _runtime(authorized_user_id=OWNER, db=db)
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = _ROLE_SECRET_JSON
        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
        monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)
        monkeypatch.setattr("src.shared.database.get_session_factory", lambda: lambda: _Session(db))
        with (
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings()),
            patch("src.internal.sts_assume_service.boto3") as boto3,
        ):
            resp = _client(db, mock_sm).post("/internal/v1/credential-assume-role", json=_request(VICTIM))
        assert resp.status_code == 404, resp.text
        # No effect: the refusal precedes credential material entirely.
        boto3.client.return_value.assume_role.assert_not_called()
        mock_sm.get_secret.assert_not_called()

    @pytest.mark.asyncio
    async def test_absent_authorized_user_refuses_rather_than_widening(self, db, monkeypatch):
        """A row with no ``authorized_user_id`` entitles the run to nobody.

        This is the absence case the issue insists on: "must be refused identically
        when it asserts nothing at all — absent identity must fail closed rather than
        widening the scope". Here the run ASKS for the legitimate owner and is still
        refused, because its own record does not name them. An implementation that
        fell back to the body would pass every other test in this file.
        """
        runtime = _runtime(authorized_user_id=None, db=db)
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = _ROLE_SECRET_JSON
        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
        monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)
        monkeypatch.setattr("src.shared.database.get_session_factory", lambda: lambda: _Session(db))
        with (
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings()),
            patch("src.internal.sts_assume_service.boto3") as boto3,
        ):
            resp = _client(db, mock_sm).post("/internal/v1/credential-assume-role", json=_request(OWNER))
        assert resp.status_code == 404, resp.text
        boto3.client.return_value.assume_role.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_authorized_user_is_not_a_wildcard(self, db, monkeypatch):
        """``authorized_user_id=""`` is what ``spawn_persona`` writes for a
        service-rooted (EventBridge) run — explicitly "no human". It must not match
        an empty request or authorize anyone."""
        runtime = _runtime(authorized_user_id="", db=db)
        mock_sm = MagicMock()
        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
        monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)
        monkeypatch.setattr("src.shared.database.get_session_factory", lambda: lambda: _Session(db))
        with (
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings()),
            patch("src.internal.sts_assume_service.boto3") as boto3,
        ):
            resp = _client(db, mock_sm).post("/internal/v1/credential-assume-role", json=_request(OWNER))
        assert resp.status_code == 404, resp.text
        boto3.client.return_value.assume_role.assert_not_called()

    @pytest.mark.asyncio
    async def test_refusal_reads_the_runs_own_row_by_primary_key(self, db, monkeypatch):
        """The lookup must be a keyed read of THIS run's row, never a query.

        A query or scan whose partition the caller influences would let the run choose
        which record it is judged against — the same "caller selects the row" defect
        this issue closes on the ingestion side. Asserted on the wire: the key is the
        authenticated ``invocation_id``, and the read is strongly consistent so a row
        written moments ago cannot be missed and read as "no authority".
        """
        runtime = _runtime(authorized_user_id=OWNER, db=db)
        mock_sm = MagicMock()
        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
        monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)
        monkeypatch.setattr("src.shared.database.get_session_factory", lambda: lambda: _Session(db))
        with (
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings()),
            patch("src.internal.sts_assume_service.boto3"),
        ):
            _client(db, mock_sm).post("/internal/v1/credential-assume-role", json=_request(VICTIM))
        kwargs = runtime._event_client.get_item.call_args.kwargs
        assert kwargs["Key"]["event_id"] == {"S": INVOCATION}
        assert kwargs["ConsistentRead"] is True
        assert kwargs["ProjectionExpression"] == "authorized_user_id"

    @pytest.mark.asyncio
    async def test_refusal_does_not_disclose_the_named_human(self, db, monkeypatch):
        """The response must not tell the caller who the run IS bound to.

        Otherwise the refusal becomes an oracle for enumerating which human each run
        carries authority for — information the caller could not otherwise obtain, and
        the reason this path answers 404 rather than 403.
        """
        runtime = _runtime(authorized_user_id=OWNER, db=db)
        mock_sm = MagicMock()
        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
        monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)
        monkeypatch.setattr("src.shared.database.get_session_factory", lambda: lambda: _Session(db))
        with (
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings()),
            patch("src.internal.sts_assume_service.boto3"),
        ):
            resp = _client(db, mock_sm).post("/internal/v1/credential-assume-role", json=_request(VICTIM))
        assert OWNER not in resp.text
        assert "authorized_user_id" not in resp.text


class TestLegitimateRunStillGetsItsOwnMaterial:
    """Without this, "refuses everything" would satisfy the class above."""

    @pytest.mark.asyncio
    async def test_the_named_human_is_released_normally(self, db, monkeypatch):
        runtime = _runtime(authorized_user_id=OWNER, db=db)
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = _ROLE_SECRET_JSON
        from tests.internal.aws_ownership_fixture import verified_role_material

        await verified_role_material(db, mock_sm, _ROLE_SECRET_JSON)

        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
        monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)
        monkeypatch.setattr("src.shared.database.get_session_factory", lambda: lambda: _Session(db))
        with (
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings()),
            patch(
                "src.internal.assume_role_routes.resolve_credential_binding",
                return_value=SimpleNamespace(resolved_user_id=OWNER, from_registry=True, drift_detected=False),
            ),
            patch("src.internal.sts_assume_service.boto3") as boto3,
        ):
            boto3.client.return_value.assume_role.return_value = {
                "Credentials": {
                    "AccessKeyId": "ASIAIOSFODNN7EXAMPLE",
                    "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                    "SessionToken": "FwoGZXIvYXdzEBY_EXAMPLE_TOKEN",
                    "Expiration": datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC),
                },
                "AssumedRoleUser": {
                    "AssumedRoleId": "AROAEXAMPLE:adp-developer-task-1",
                    "Arn": "arn:aws:sts::123456789012:assumed-role/ADPDeployAgent/task-1",
                },
            }
            resp = _client(db, mock_sm).post("/internal/v1/credential-assume-role", json=_request(OWNER))
        assert resp.status_code == 200, resp.text
        assert resp.json()["profile_name"] == "adp-aws-prod"
        runtime.validate_flow.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invocation_mismatch_is_still_refused(self, db, monkeypatch):
        """Pre-existing binding, re-asserted: the body's ``invocation_id`` must equal
        the authenticated caller's. Kept here so a future refactor of the user check
        cannot quietly take this one with it — they are the same defect class, and a
        run that can claim another invocation can reach another human by that route.
        """
        runtime = _runtime(authorized_user_id=OWNER, db=db)
        mock_sm = MagicMock()
        monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")
        monkeypatch.setattr("src.agentauth.routes.get_agent_runtime", lambda: runtime)
        monkeypatch.setattr("src.shared.database.get_session_factory", lambda: lambda: _Session(db))
        body = dict(_request(OWNER), invocation_id="inv-someone-else")
        with (
            patch("src.internal.assume_role_routes.get_settings", return_value=_settings()),
            patch("src.internal.sts_assume_service.boto3") as boto3,
        ):
            resp = _client(db, mock_sm).post("/internal/v1/credential-assume-role", json=body)
        assert resp.status_code == 404, resp.text
        boto3.client.return_value.assume_role.assert_not_called()


@pytest.fixture(autouse=True)
def platform_account(monkeypatch):
    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "111111111111")
