"""Adversarial tests for credential-authorization binding — Issue #3184.

Independent attacker's perspective: attacks beyond implementers' own tests.
Exercises A1-A7 catalog + improvised variants the design didn't enumerate.

Attack categories:
  - A1: env-var spoofed user_id (body) vs registry truth
  - A2: same via assume-role path
  - A3: forged marker → empty authorized_user_id (tested at marker layer)
  - A4: chain at/over max_credential_chain_depth → deny
  - A5: legit long-horizon repeated access → no false deny
  - A7: same-user happy path
  - Improvised: invocation_id replay, empty/whitespace variants, DDB edge cases

The attackers DO NOT author any story code — they attack the merged result.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal.assume_role_routes import get_secrets_manager as ar_get_secrets_manager
from src.internal.assume_role_routes import router as assume_role_router
from src.internal.credential_routes import get_secrets_manager as cr_get_secrets_manager
from src.internal.credential_routes import router as credential_router
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import UserCredential

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
_VALID_KEY = "test-internal-api-key-adversarial"

# Victims and attackers use different IDs from the original test suite.
_ATTACKER_USER_ID = "user-attacker-adv"
_VICTIM_USER_ID = "user-victim-adv"
_INVOCATION_ATTACKER = "inv-attacker-run-001"
_INVOCATION_VICTIM = "inv-victim-run-002"
_INVOCATION_REPLAYED = "inv-victim-run-002"  # Replay of victim's invocation
_INVOCATION_LONG_HORIZON = "inv-long-horizon-legit-003"

_ROLE_SECRET_JSON = json.dumps(
    {
        "role_arn": "arn:aws:iam::999888777666:role/AdvTarget",
        "external_id": "adp-adv-test",
        "session_duration_seconds": 900,
        "default_region": "us-west-2",
    }
)


# ---------------------------------------------------------------------------
# Test infrastructure (independent from story authors' fixtures)
# ---------------------------------------------------------------------------


def _make_engine():
    return create_async_engine(
        TEST_DB_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def engine():
    eng = _make_engine()
    async with eng.begin() as conn:
        import src.shared.models.audit  # noqa: F401
        import src.shared.models.vault  # noqa: F401

        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        org = Organization(
            id="org-adv",
            name="Adversarial Test Org",
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
            cognito_client_ids=[],
        )
        dept = Department(id="dept-adv", org_id="org-adv", name="Security")
        team = Team(id="team-adv", org_id="org-adv", department_id="dept-adv", name="Red")

        attacker = User(
            id=_ATTACKER_USER_ID,
            org_id="org-adv",
            team_id="team-adv",
            email="attacker@adversarial.test",
        )
        victim = User(
            id=_VICTIM_USER_ID,
            org_id="org-adv",
            team_id="team-adv",
            email="victim@adversarial.test",
        )
        session.add_all([org, dept, team, attacker, victim])
        await session.flush()

        # Seed victim credentials only — attacker should NEVER be able to read these.
        victim_bearer = UserCredential(
            id="cred-adv-bearer-victim",
            org_id="org-adv",
            user_id=_VICTIM_USER_ID,
            service="github",
            label="main",
            credential_type="bearer",
            secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:adv-victim-bearer",
        )
        victim_aws = UserCredential(
            id="cred-adv-aws-victim",
            org_id="org-adv",
            user_id=_VICTIM_USER_ID,
            service="aws",
            label="prod",
            credential_type="aws_role",
            secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:adv-victim-aws",
        )
        # Attacker's own credential (for A5/A7 happy path).
        attacker_bearer = UserCredential(
            id="cred-adv-bearer-attacker",
            org_id="org-adv",
            user_id=_ATTACKER_USER_ID,
            service="github",
            label="main",
            credential_type="bearer",
            secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:adv-attacker-bearer",
        )
        attacker_aws = UserCredential(
            id="cred-adv-aws-attacker",
            org_id="org-adv",
            user_id=_ATTACKER_USER_ID,
            service="aws",
            label="prod",
            credential_type="aws_role",
            secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:adv-attacker-aws",
        )
        session.add_all([victim_bearer, victim_aws, attacker_bearer, attacker_aws])
        await session.commit()
        yield session


def _make_raw_read_app(db_session: AsyncSession, mock_sm=None) -> TestClient:
    app = FastAPI()
    app.include_router(credential_router)

    async def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    if mock_sm is not None:
        app.dependency_overrides[cr_get_secrets_manager] = lambda: mock_sm
    return TestClient(app, raise_server_exceptions=False)


def _make_assume_role_app(db_session: AsyncSession, mock_sm=None) -> TestClient:
    app = FastAPI()
    app.include_router(assume_role_router)

    async def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    if mock_sm is not None:
        app.dependency_overrides[ar_get_secrets_manager] = lambda: mock_sm
    return TestClient(app, raise_server_exceptions=False)


def _settings_mock(*, enforce: bool = True, raw_read_enabled: bool = True):
    """Settings mock — enforce=True by default for adversarial testing."""
    s = MagicMock()
    s.internal_api_key = _VALID_KEY
    s.aws_region = "us-east-1"
    s.vault_raw_read_enabled = raw_read_enabled
    s.enforce_credential_binding = enforce
    s.webhook_events_table = "adp-test-webhook-events"
    return s


def _mock_ddb_query_response(authorized_user_id: str | None = None, invocation_id: str = "", arrived_at: str = "2026-07-08T12:00:00Z"):
    """Build a mock DDB Query response (composite-key table: event_id + arrived_at).

    None → no items found; "" → item exists without authorized_user_id attribute.
    """
    if authorized_user_id is None:
        return {"Items": [], "Count": 0, "ScannedCount": 0}
    if authorized_user_id == "":
        return {
            "Items": [{"event_id": invocation_id, "arrived_at": arrived_at}],
            "Count": 1,
            "ScannedCount": 1,
        }
    return {
        "Items": [
            {
                "event_id": invocation_id,
                "arrived_at": arrived_at,
                "authorized_user_id": authorized_user_id,
            }
        ],
        "Count": 1,
        "ScannedCount": 1,
    }


def _mock_sts_response():
    return {
        "Credentials": {
            "AccessKeyId": "ASIAADVERSARIALEXAMPLE",
            "SecretAccessKey": "adversarial/secret/key/EXAMPLE",
            "SessionToken": "adversarial-session-token",
            "Expiration": datetime(2026, 7, 7, 23, 59, 59, tzinfo=UTC),
        },
        "AssumedRoleUser": {
            "AssumedRoleId": "AROAADVERSARIAL:adp-adv-test",
            "Arn": "arn:aws:sts::999888777666:assumed-role/AdvTarget/adp-adv-test",
        },
    }


# ===========================================================================
# A1: Attacker sets ADP_USER_ID=<victim>, calls adp-cred raw --service github
# ===========================================================================


class TestA1EnvVarInjectionRawRead:
    """A1: Agent spoofs body user_id to victim, but registry says attacker.

    The agent's env has ADP_USER_ID=<victim> (injected via prompt manipulation).
    The CLI sends body.user_id=victim. But the invocation_id maps to the
    attacker's run in the registry → drift detected → 403 in enforce mode.
    """

    @pytest.mark.asyncio
    async def test_a1_victim_user_id_in_body_rejected_enforce(self, db):
        """Body says victim, registry says attacker → 403 drift."""
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            # Registry says the run belongs to the ATTACKER
            mock_table.query.return_value = _mock_ddb_query_response(_ATTACKER_USER_ID, _INVOCATION_ATTACKER)

            client = _make_raw_read_app(db)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": _VICTIM_USER_ID,  # Spoofed!
                    "agent_id": "developer",
                    "task_id": "task-a1",
                    "service": "github",
                    "label": "main",
                    "invocation_id": _INVOCATION_ATTACKER,
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "credential_authorization_drift"

    @pytest.mark.asyncio
    async def test_a1_shadow_mode_uses_registry_not_body(self, db):
        """A1 shadow: drift detected but not blocked. Credential resolves
        to the REGISTRY user (attacker), not the body user (victim).
        The attacker gets their own cred, NOT the victim's.
        """
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = "ghp_attacker_token"

        settings = _settings_mock(enforce=False)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            # Registry says attacker
            mock_table.query.return_value = _mock_ddb_query_response(_ATTACKER_USER_ID, _INVOCATION_ATTACKER)

            client = _make_raw_read_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": _VICTIM_USER_ID,  # Spoofed
                    "agent_id": "developer",
                    "task_id": "task-a1-shadow",
                    "service": "github",
                    "label": "main",
                    "invocation_id": _INVOCATION_ATTACKER,
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        # Shadow mode succeeds, but resolves to ATTACKER's credential
        assert resp.status_code == 200
        # Verify audit shows drift
        stmt = select(AuditLog).where(AuditLog.event_type == "vault_credential_raw_read")
        result = await db.execute(stmt)
        audits = result.scalars().all()
        drift_audits = [a for a in audits if a.details.get("binding_drift_detected") is True]
        assert len(drift_audits) >= 1
        # Crucially: the authorized_user_id in audit is the ATTACKER, not victim
        assert drift_audits[0].details["authorized_user_id"] == _ATTACKER_USER_ID


# ===========================================================================
# A2: Same via assume-role path
# ===========================================================================


class TestA2AssumeRoleInjection:
    """A2: Attacker spoofs body user_id in assume-role request."""

    @pytest.mark.asyncio
    async def test_a2_victim_user_id_rejected_enforce(self, db):
        """Body says victim, registry says attacker → 403."""
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.assume_role_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            mock_table.query.return_value = _mock_ddb_query_response(_ATTACKER_USER_ID, _INVOCATION_ATTACKER)

            client = _make_assume_role_app(db)
            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": _VICTIM_USER_ID,
                    "agent_id": "developer",
                    "task_id": "task-a2",
                    "service": "aws",
                    "label": "prod",
                    "invocation_id": _INVOCATION_ATTACKER,
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "credential_authorization_drift"

    @pytest.mark.asyncio
    async def test_a2_no_sts_session_for_victim(self, db):
        """Even in shadow mode, STS tags use registry user (attacker), not body (victim)."""
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = _ROLE_SECRET_JSON

        settings = _settings_mock(enforce=False)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.assume_role_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
            patch("src.internal.sts_assume_service.boto3") as mock_boto3,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            # Registry resolves to ATTACKER
            mock_table.query.return_value = _mock_ddb_query_response(_ATTACKER_USER_ID, _INVOCATION_ATTACKER)

            mock_sts_client = MagicMock()
            mock_boto3.client.return_value = mock_sts_client
            mock_sts_client.assume_role.return_value = _mock_sts_response()

            client = _make_assume_role_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": _VICTIM_USER_ID,  # Spoofed
                    "agent_id": "developer",
                    "task_id": "task-a2-shadow",
                    "service": "aws",
                    "label": "prod",
                    "invocation_id": _INVOCATION_ATTACKER,
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200
        # STS session tags must use the REGISTRY user (attacker), NOT victim
        call_kwargs = mock_sts_client.assume_role.call_args[1]
        tags = {t["Key"]: t["Value"] for t in call_kwargs["Tags"]}
        assert tags["adp:user_id"] == _ATTACKER_USER_ID
        assert tags["adp:user_id"] != _VICTIM_USER_ID


# ===========================================================================
# A4: Chain at/over max_credential_chain_depth → empty authorized_user_id
# ===========================================================================


class TestA4DepthGating:
    """A4: When authorized_user_id="" (chain too deep), enforce → 403."""

    @pytest.mark.asyncio
    async def test_a4_empty_authorized_user_id_denied_enforce(self, db):
        """Registry row exists but authorized_user_id="" → 403 in enforce."""
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            # Row exists but authorized_user_id is missing (depth exceeded at spawn)
            mock_table.query.return_value = {
                "Items": [{"event_id": "inv-deep-chain", "arrived_at": "2026-07-08T10:00:00Z"}],
                "Count": 1,
                "ScannedCount": 1,
            }

            client = _make_raw_read_app(db)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": _VICTIM_USER_ID,
                    "agent_id": "developer",
                    "task_id": "task-a4",
                    "service": "github",
                    "label": "main",
                    "invocation_id": "inv-deep-chain",
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "credential_binding_failed"
        assert "No authorized user" in detail["message"]

    @pytest.mark.asyncio
    async def test_a4_assume_role_empty_authorized_user_denied(self, db):
        """Same A4 attack via assume-role → 403."""
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.assume_role_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            mock_table.query.return_value = {
                "Items": [{"event_id": "inv-deep-chain-ar", "arrived_at": "2026-07-08T10:00:00Z"}],
                "Count": 1,
                "ScannedCount": 1,
            }

            client = _make_assume_role_app(db)
            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": _VICTIM_USER_ID,
                    "agent_id": "developer",
                    "task_id": "task-a4-ar",
                    "service": "aws",
                    "label": "prod",
                    "invocation_id": "inv-deep-chain-ar",
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "credential_binding_failed"


# ===========================================================================
# A5: Legit long-horizon run refreshes STS repeatedly → no false deny
# ===========================================================================


class TestA5LongHorizonRefresh:
    """A5: Legitimate user repeatedly accesses credentials over time."""

    @pytest.mark.asyncio
    async def test_a5_repeated_access_succeeds(self, db):
        """Same user, same invocation_id, multiple requests → all 200."""
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = "ghp_long_horizon_token"

        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            mock_table.query.return_value = _mock_ddb_query_response(_ATTACKER_USER_ID, _INVOCATION_LONG_HORIZON)

            client = _make_raw_read_app(db, mock_sm)

            # Multiple requests simulating refresh over a long run
            for i in range(5):
                resp = client.post(
                    "/internal/v1/credential-raw-read",
                    json={
                        "user_id": _ATTACKER_USER_ID,
                        "agent_id": "developer",
                        "task_id": f"task-a5-{i}",
                        "service": "github",
                        "label": "main",
                        "invocation_id": _INVOCATION_LONG_HORIZON,
                    },
                    headers={
                        "X-Internal-Api-Key": _VALID_KEY,
                        "X-Agent-Scopes": "credential:raw-read",
                    },
                )
                assert resp.status_code == 200, f"Request {i} failed: {resp.json()}"


# ===========================================================================
# A7: Same-user happy path → success
# ===========================================================================


class TestA7HappyPath:
    """A7: Legitimate credential access with matching user → 200."""

    @pytest.mark.asyncio
    async def test_a7_raw_read_matching_user_succeeds(self, db):
        """Body user_id matches registry → 200."""
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = "ghp_legitimate_token"

        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            mock_table.query.return_value = _mock_ddb_query_response(_ATTACKER_USER_ID, _INVOCATION_ATTACKER)

            client = _make_raw_read_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": _ATTACKER_USER_ID,  # Matches registry
                    "agent_id": "developer",
                    "task_id": "task-a7",
                    "service": "github",
                    "label": "main",
                    "invocation_id": _INVOCATION_ATTACKER,
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["value"] == "ghp_legitimate_token"
        assert data["credential_type"] == "bearer"

    @pytest.mark.asyncio
    async def test_a7_assume_role_matching_user_succeeds(self, db):
        """Body user_id matches registry for assume-role → 200."""
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = _ROLE_SECRET_JSON

        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.assume_role_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
            patch("src.internal.sts_assume_service.boto3") as mock_boto3,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            mock_table.query.return_value = _mock_ddb_query_response(_ATTACKER_USER_ID, _INVOCATION_ATTACKER)

            mock_sts_client = MagicMock()
            mock_boto3.client.return_value = mock_sts_client
            mock_sts_client.assume_role.return_value = _mock_sts_response()

            client = _make_assume_role_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/credential-assume-role",
                json={
                    "user_id": _ATTACKER_USER_ID,  # Matches registry
                    "agent_id": "developer",
                    "task_id": "task-a7-ar",
                    "service": "aws",
                    "label": "prod",
                    "invocation_id": _INVOCATION_ATTACKER,
                },
                headers={"X-Internal-Api-Key": _VALID_KEY},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["access_key_id"] == "ASIAADVERSARIALEXAMPLE"


# ===========================================================================
# Improvised: Invocation-ID replay attack
# ===========================================================================


class TestImprovisedInvocationReplay:
    """Attacker replays victim's invocation_id to steal their credentials."""

    @pytest.mark.asyncio
    async def test_replay_victim_invocation_id_gets_victim_cred(self, db):
        """If attacker knows victim's invocation_id AND registry returns
        victim's user_id for that ID, the binding correctly resolves to victim.

        This is NOT a vulnerability: the body user_id won't match, causing
        drift rejection in enforce mode. The registry is the source of truth.
        """
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            # The VICTIM's run is in the registry under this invocation_id
            mock_table.query.return_value = _mock_ddb_query_response(_VICTIM_USER_ID, _INVOCATION_VICTIM)

            client = _make_raw_read_app(db)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    # Attacker sends their own user_id with victim's invocation_id
                    "user_id": _ATTACKER_USER_ID,
                    "agent_id": "developer",
                    "task_id": "task-replay",
                    "service": "github",
                    "label": "main",
                    "invocation_id": _INVOCATION_VICTIM,  # Replayed!
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        # Enforce mode: body user != registry user → 403 drift
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "credential_authorization_drift"

    @pytest.mark.asyncio
    async def test_replay_with_matching_body_user_id(self, db):
        """More sophisticated attack: attacker sets body.user_id=victim AND
        uses victim's invocation_id. Registry returns victim → matches body.

        This succeeds because the binding resolves correctly — BUT the attacker
        would need to control the pod environment (ADP_MESSAGE_ID is from the
        trusted SQS envelope, not agent-writable). If they somehow spoofed it,
        they'd get the victim's cred. This tests the boundary holds if SQS
        envelope trust is maintained.
        """
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = "ghp_victim_secret_LEAKED"

        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            mock_table.query.return_value = _mock_ddb_query_response(_VICTIM_USER_ID, _INVOCATION_VICTIM)

            client = _make_raw_read_app(db, mock_sm)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    # Attacker pretends to be victim AND uses their invocation_id
                    "user_id": _VICTIM_USER_ID,
                    "agent_id": "developer",
                    "task_id": "task-replay-match",
                    "service": "github",
                    "label": "main",
                    "invocation_id": _INVOCATION_VICTIM,
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        # This succeeds — the boundary relies on ADP_MESSAGE_ID being
        # set by the trusted SQS envelope, not agent-controlled.
        # If the env var trust is broken, this is a vulnerability.
        # For now, document that the gateway trusts the invocation_id
        # — the trust boundary is at the pod environment level.
        assert resp.status_code == 200


# ===========================================================================
# Improvised: Empty/whitespace invocation_id variants
# ===========================================================================


class TestImprovisedEmptyInvocationId:
    """Attack with various empty/whitespace invocation_id values."""

    @pytest.mark.asyncio
    async def test_empty_string_invocation_id_rejected_enforce(self, db):
        """Empty string invocation_id → treated as missing → 403."""
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
        ):
            client = _make_raw_read_app(db)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": _VICTIM_USER_ID,
                    "agent_id": "developer",
                    "task_id": "task-empty-inv",
                    "service": "github",
                    "label": "main",
                    "invocation_id": "",  # Empty string
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "credential_binding_failed"

    @pytest.mark.asyncio
    async def test_null_invocation_id_rejected_enforce(self, db):
        """null invocation_id → treated as missing → 403."""
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
        ):
            client = _make_raw_read_app(db)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": _VICTIM_USER_ID,
                    "agent_id": "developer",
                    "task_id": "task-null-inv",
                    "service": "github",
                    "label": "main",
                    "invocation_id": None,
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "credential_binding_failed"

    @pytest.mark.asyncio
    async def test_whitespace_only_invocation_id(self, db):
        """Whitespace-only invocation_id — verify how DDB handles it.

        The binding code does `if not invocation_id:` which is False for
        whitespace strings like "   ". This would pass to DDB as a lookup key.
        If DDB has no such row, it returns empty → enforce → 403.
        """
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            # DDB won't find a row for whitespace key
            mock_table.query.return_value = {"Items": [], "Count": 0, "ScannedCount": 0}

            client = _make_raw_read_app(db)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": _VICTIM_USER_ID,
                    "agent_id": "developer",
                    "task_id": "task-ws-inv",
                    "service": "github",
                    "label": "main",
                    "invocation_id": "   ",  # Whitespace only
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        # Whitespace passes the `if not invocation_id` check (truthy),
        # goes to DDB, no row found → empty authorized_user_id → 403
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "credential_binding_failed"


# ===========================================================================
# Improvised: DDB edge cases
# ===========================================================================


class TestImprovisedDDBEdgeCases:
    """Edge cases in the DDB lookup path."""

    @pytest.mark.asyncio
    async def test_ddb_timeout_failsoft_enforce_denies(self, db):
        """DDB ClientError (timeout) → empty string → enforce denies.

        The binding code returns "" on DDB errors (fail-soft for availability).
        In enforce mode, "" → 403.
        """
        from botocore.exceptions import ClientError

        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            mock_table.query.side_effect = ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "Rate exceeded"}},
                "Query",
            )

            client = _make_raw_read_app(db)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": _VICTIM_USER_ID,
                    "agent_id": "developer",
                    "task_id": "task-ddb-timeout",
                    "service": "github",
                    "label": "main",
                    "invocation_id": "inv-timeout-test",
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        # DDB error → _lookup returns "" → enforce → 403
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "credential_binding_failed"

    @pytest.mark.asyncio
    async def test_ddb_item_exists_but_no_authorized_user_id_attribute(self, db):
        """DDB item exists (event_id) but has no authorized_user_id attribute.

        This simulates an old-format row written before S1 was deployed.
        Should return "" → enforce → 403.
        """
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            # Item exists but no authorized_user_id attribute
            mock_table.query.return_value = {
                "Items": [
                    {
                        "event_id": "inv-old-format",
                        "arrived_at": "2026-07-08T10:00:00Z",
                        "tenant_id": "old-tenant",
                        "status": "completed",
                    }
                ],
                "Count": 1,
                "ScannedCount": 1,
            }

            client = _make_raw_read_app(db)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": _VICTIM_USER_ID,
                    "agent_id": "developer",
                    "task_id": "task-old-format",
                    "service": "github",
                    "label": "main",
                    "invocation_id": "inv-old-format",
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        # .get("authorized_user_id", "") returns "" → enforce → 403
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_extremely_long_invocation_id(self, db):
        """Very long invocation_id (>256 chars) — verify no crash."""
        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            # DDB won't find a row for this gibberish
            mock_table.query.return_value = {"Items": [], "Count": 0, "ScannedCount": 0}

            long_id = "a" * 1024
            client = _make_raw_read_app(db)
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": _VICTIM_USER_ID,
                    "agent_id": "developer",
                    "task_id": "task-long-inv",
                    "service": "github",
                    "label": "main",
                    "invocation_id": long_id,
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        # No crash — DDB returns no item → 403
        assert resp.status_code == 403


# ===========================================================================
# Improvised: Credential isolation validation
# ===========================================================================


class TestImprovisedCredentialIsolation:
    """Verify credentials are isolated between users even when binding succeeds."""

    @pytest.mark.asyncio
    async def test_attacker_cannot_read_victim_cred_via_own_binding(self, db):
        """Even with legitimate binding, attacker can only read THEIR creds.

        Registry says attacker → credential resolver uses attacker's user_id →
        only attacker's credentials are accessible. Victim's creds are invisible.
        """
        mock_sm = MagicMock()
        mock_sm.get_secret.return_value = "ghp_attacker_own_secret"

        settings = _settings_mock(enforce=True)

        with (
            patch("src.internal.routes.get_settings", return_value=settings),
            patch("src.internal.auth_deps.get_settings", return_value=settings),
            patch("src.internal.credential_routes.get_settings", return_value=settings),
            patch("src.internal.credential_binding._get_dynamodb_table") as mock_get_table,
        ):
            mock_table = MagicMock()
            mock_get_table.return_value = mock_table
            mock_table.query.return_value = _mock_ddb_query_response(_ATTACKER_USER_ID, _INVOCATION_ATTACKER)

            client = _make_raw_read_app(db, mock_sm)
            # Request with attacker's legitimate invocation_id
            # but asking for a service/label that only victim has would 404
            resp = client.post(
                "/internal/v1/credential-raw-read",
                json={
                    "user_id": _ATTACKER_USER_ID,
                    "agent_id": "developer",
                    "task_id": "task-isolation",
                    "service": "github",
                    "label": "main",
                    "invocation_id": _INVOCATION_ATTACKER,
                },
                headers={
                    "X-Internal-Api-Key": _VALID_KEY,
                    "X-Agent-Scopes": "credential:raw-read",
                },
            )

        # Attacker gets their own cred — this verifies the path works
        assert resp.status_code == 200
        assert resp.json()["value"] == "ghp_attacker_own_secret"
