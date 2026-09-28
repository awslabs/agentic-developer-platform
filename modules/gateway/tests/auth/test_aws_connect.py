"""Tests for AWS account connect flow (Issue #562).

Coverage:
  - connect_start creates pending row + SM secret, returns valid launch URL
  - connect_start URL is under 8000 chars (browser limit)
  - connect_start template is valid YAML
  - verify success when assume_role succeeds → status flips to verified
  - verify failure propagates user-friendly reason
  - verify is idempotent (second call on verified row is no-op)
  - tenant isolation: user A cannot verify user B's pending credential
  - Issue #4742: verify classifies the role as routing-capable (v2) or
    single-user (v1) and records a machine-readable reason
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from urllib.parse import unquote

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# The cfn_template module reads ADP_CFN_TEMPLATE_BUCKET from env to decide
# which bucket to pre-sign against.  Set it BEFORE the module is imported
# anywhere in the test process so the first call gets the right value.
os.environ.setdefault("ADP_CFN_TEMPLATE_BUCKET", "adp-test-cfn-templates")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

from src.auth.aws_connect_routes import router
from src.auth.middleware import get_current_user_context
from src.auth.vault_routes import get_secrets_manager
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.organization import Organization, Team
from src.shared.models.vault import UserCredential  # noqa: F401 — needed for metadata.create_all
from src.shared.schemas.auth import TokenContext

# ---------------------------------------------------------------------------
# Test database setup
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


def make_engine():
    return create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


def load_credential(app: FastAPI, credential_id: str) -> UserCredential:
    async def load() -> UserCredential:
        dependency = app.dependency_overrides[get_db]
        async for session in dependency():
            return await session.get(UserCredential, credential_id)
        raise AssertionError("database dependency did not yield a session")

    return asyncio.run(load())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(
    user_id: str = "user-alice",
    org_id: str = "org-acme",
    team_id: str = "team-eng",
    is_admin: bool = False,
) -> TokenContext:
    return TokenContext(
        user_id=user_id,
        org_id=org_id,
        team_id=team_id,
        department_id="dept-eng",
        account_type="human",
        is_admin=is_admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


# IMPORTANT: TokenContext.user_id is the Cognito sub (a UUID). The handler
# resolves it to the Postgres users.id via cognito_sub, which is why we seed
# a matching `users` row per test context below.
ALICE_COGNITO_SUB = "sub-alice-cognito"
ALICE_DB_ID = "db-id-alice"
BOB_COGNITO_SUB = "sub-bob-cognito"
BOB_DB_ID = "db-id-bob"

ALICE = _make_context(user_id=ALICE_COGNITO_SUB)
BOB = _make_context(user_id=BOB_COGNITO_SUB)
# Issue #600: User with empty org_id in token (GitHub-federated)
ALICE_EMPTY_ORG = _make_context(user_id=ALICE_COGNITO_SUB, org_id="")


class MockSecretsManager:
    """Mock SM that stores secrets in a dict."""

    def __init__(self):
        self._secrets: dict[str, str] = {}
        self._counter = 0

    def create_secret(self, service: str, label: str, payload: str | dict, **kwargs) -> str:
        self._counter += 1
        arn = f"arn:aws:secretsmanager:us-east-1:123456789012:secret:test-{self._counter}"
        if isinstance(payload, dict):
            payload = json.dumps(payload)
        self._secrets[arn] = payload
        return arn

    def get_secret(self, secret_arn: str) -> str:
        return self._secrets[secret_arn]

    def current_version_id(self, secret_arn: str) -> str:
        return hashlib.sha256(self._secrets[secret_arn].encode()).hexdigest()

    def get_secret_at_version(self, secret_arn: str, version_id: str) -> tuple[str, str]:
        assert self.current_version_id(secret_arn) == version_id
        return self._secrets[secret_arn], version_id

    def delete_secret(self, secret_arn: str, **kwargs) -> None:
        self._secrets.pop(secret_arn, None)

    def update_secret(self, secret_arn: str, payload: str | dict) -> None:
        if isinstance(payload, dict):
            payload = json.dumps(payload)
        self._secrets[secret_arn] = payload


def _assumed_role_result(**kwargs):
    parts = kwargs["role_arn"].split(":", 5)
    role_name = parts[5].rsplit("/", 1)[-1]
    return MagicMock(assumed_role_arn=f"arn:{parts[1]}:sts::{parts[4]}:assumed-role/{role_name}/verification")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _moto_s3():
    """Stub S3 so ``generate_presigned_url`` returns a URL for a real (mock) bucket."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=os.environ["ADP_CFN_TEMPLATE_BUCKET"])
        s3.put_object(
            Bucket=os.environ["ADP_CFN_TEMPLATE_BUCKET"],
            Key="cfn-templates/aws_role_v1.yaml",
            Body=b"placeholder",
        )
        yield


@pytest.fixture
def mock_sm():
    return MockSecretsManager()


@pytest.fixture
def app_and_client(mock_sm):
    """Create a test app with the AWS connect router + in-memory DB."""
    engine = make_engine()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            from src.shared.models.organization import User

            org = Organization(id="org-acme", name="Acme Corp")
            session.add(org)
            team = Team(id="team-eng", name="Eng Team", org_id="org-acme", department_id="dept-eng")
            session.add(team)
            # Seed Postgres users rows keyed by cognito_sub — the handler
            # looks these up via User.cognito_sub to resolve the FK for
            # user_credentials.user_id.
            session.add(
                User(
                    id=ALICE_DB_ID,
                    org_id="org-acme",
                    team_id="team-eng",
                    email="alice@example.com",
                    name="Alice",
                    cognito_sub=ALICE_COGNITO_SUB,
                )
            )
            session.add(
                User(
                    id=BOB_DB_ID,
                    org_id="org-acme",
                    team_id="team-eng",
                    email="bob@example.com",
                    name="Bob",
                    cognito_sub=BOB_COGNITO_SUB,
                )
            )
            await session.commit()

    asyncio.run(_setup())

    app = FastAPI()
    app.include_router(router)

    # Override dependencies
    async def get_test_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = get_test_db
    app.dependency_overrides[get_secrets_manager] = lambda: mock_sm

    client = TestClient(app)
    return app, client


@pytest.fixture
def alice_client(app_and_client):
    app, client = app_and_client
    app.dependency_overrides[get_current_user_context] = lambda: ALICE
    return client


@pytest.fixture
def bob_client(app_and_client):
    app, client = app_and_client
    app.dependency_overrides[get_current_user_context] = lambda: BOB
    return client


# ---------------------------------------------------------------------------
# Tests: connect/start
# ---------------------------------------------------------------------------


class TestConnectStart:
    def test_creates_pending_row_and_returns_launch_url(self, alice_client, mock_sm):
        resp = alice_client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "prod-readonly", "account_id": "123456789012"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "credential_id" in body
        assert "launch_url" in body
        assert "console.aws.amazon.com/cloudformation" in body["launch_url"]
        assert "quickcreate" in body["launch_url"]

        # Verify SM was called
        assert len(mock_sm._secrets) == 1
        secret_value = list(mock_sm._secrets.values())[0]
        parsed = json.loads(secret_value)
        assert parsed["account_id"] == "123456789012"
        assert parsed["role_arn"] == "arn:aws:iam::123456789012:role/ADP-Agent-prod-readonly"
        assert "external_id" in parsed
        assert str(uuid.UUID(parsed["external_id"])) == parsed["external_id"]

    def test_launch_url_includes_correct_params(self, alice_client):
        resp = alice_client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "my-role", "account_id": "111222333444"},
        )
        assert resp.status_code == 201
        url = resp.json()["launch_url"]

        # Check key parameters are in the URL
        assert "stackName=ADP-Agent-my-role" in url
        assert "param_Nickname=my-role" in url
        # UserSessionTag must be the Postgres users.id (what STS sees in the
        # session tag), not the Cognito sub — see handler comment.
        assert f"param_UserSessionTag={ALICE_DB_ID}" in url
        assert "param_GatewayRolePrincipal=" in url
        assert "param_GatewayAccountId=" in url

    def test_launch_url_under_browser_limit(self, alice_client):
        """URL must be under 8000 chars to fit in browser URL bars."""
        # Use worst-case parameter lengths
        long_nickname = "a" * 64
        resp = alice_client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": long_nickname, "account_id": "999888777666"},
        )
        assert resp.status_code == 201
        url = resp.json()["launch_url"]
        assert len(url) < 8000, f"Launch URL exceeds browser limit: {len(url)} chars"

    def test_launch_url_uses_s3_presigned_template_url(self, alice_client):
        """AWS Console requires templateURL, and CFN only accepts S3 hosts."""
        resp = alice_client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "test", "account_id": "123456789012"},
        )
        url = resp.json()["launch_url"]
        fragment_query = url.split("quickcreate?", 1)[1]

        assert "templateURL=" in fragment_query
        assert "templateBody=" not in fragment_query

        template_url_param = next(p for p in fragment_query.split("&") if p.startswith("templateURL="))
        decoded = unquote(template_url_param[len("templateURL=") :])
        # Virtual-hosted S3 host + pre-signed query params.  Match the bucket
        # from the test-env ADP_CFN_TEMPLATE_BUCKET setting (see conftest).
        assert ".s3." in decoded or ".s3.amazonaws.com" in decoded
        assert "X-Amz-Signature=" in decoded or "Signature=" in decoded
        assert "cfn-templates/aws_role_v1.yaml" in decoded

    def test_rejects_invalid_account_id(self, alice_client):
        resp = alice_client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "test", "account_id": "12345"},
        )
        assert resp.status_code == 422

    def test_rejects_empty_nickname(self, alice_client):
        resp = alice_client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "", "account_id": "123456789012"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests: verify
# ---------------------------------------------------------------------------


class TestConnectVerify:
    def _create_pending_credential(self, client) -> str:
        """Helper: create a pending credential and return its ID."""
        resp = client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "verify-test", "account_id": "123456789012"},
        )
        assert resp.status_code == 201
        return resp.json()["credential_id"]

    @patch("src.auth.aws_connect_routes.assume_role")
    def test_verify_success_records_server_owned_provenance(self, mock_assume, app_and_client):
        """When STS AssumeRole succeeds, status becomes verified."""
        app, client = app_and_client
        app.dependency_overrides[get_current_user_context] = lambda: ALICE
        mock_assume.return_value = MagicMock(
            access_key_id="AKIA...",
            secret_access_key="secret",
            session_token="token",
            expiration="2026-01-01T00:00:00Z",
            region="us-east-1",
            profile_name="adp-aws-verify-test",
            assumed_role_arn="arn:aws:sts::123456789012:assumed-role/ADP-Agent-verify-test/verification",
        )

        cred_id = self._create_pending_credential(client)
        resp = client.post(
            "/auth/credentials/aws/verify",
            json={"credential_id": cred_id},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "verified"
        assert load_credential(app, cred_id).aws_verified_at is not None

    @patch("src.auth.aws_connect_routes.assume_role")
    def test_verify_failure_returns_reason(self, mock_assume, alice_client):
        """When STS fails with NoSuchEntity, return user-friendly reason."""
        from src.internal.sts_assume_service import STSAssumeError

        mock_assume.side_effect = STSAssumeError("role not found", code="NoSuchEntity")

        cred_id = self._create_pending_credential(alice_client)
        resp = alice_client.post(
            "/auth/credentials/aws/verify",
            json={"credential_id": cred_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert "not been created yet" in body["reason"]

    @patch("src.auth.aws_connect_routes.assume_role")
    def test_verify_access_denied_reason(self, mock_assume, alice_client):
        """AccessDenied returns a trust-policy-related message."""
        from src.internal.sts_assume_service import STSAssumeError

        mock_assume.side_effect = STSAssumeError("access denied", code="AccessDenied")

        cred_id = self._create_pending_credential(alice_client)
        resp = alice_client.post(
            "/auth/credentials/aws/verify",
            json={"credential_id": cred_id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert "trust policy" in body["reason"]

    @patch("src.auth.aws_connect_routes.assume_role")
    def test_verify_is_idempotent(self, mock_assume, alice_client):
        """Second verify on an already-verified row is a no-op."""
        mock_assume.side_effect = _assumed_role_result

        cred_id = self._create_pending_credential(alice_client)

        # First verify
        resp1 = alice_client.post(
            "/auth/credentials/aws/verify",
            json={"credential_id": cred_id},
        )
        assert resp1.json()["status"] == "verified"

        # Second verify — no STS call needed
        mock_assume.reset_mock()
        resp2 = alice_client.post(
            "/auth/credentials/aws/verify",
            json={"credential_id": cred_id},
        )
        assert resp2.json()["status"] == "verified"
        # Should NOT have called assume_role again
        mock_assume.assert_not_called()

    def test_verify_returns_404_for_nonexistent(self, alice_client):
        resp = alice_client.post(
            "/auth/credentials/aws/verify",
            json={"credential_id": "nonexistent-id"},
        )
        assert resp.status_code == 404

    def test_tenant_isolation_bob_cannot_verify_alice(self, app_and_client, mock_sm):
        """User B cannot verify user A's pending credential."""
        app, client = app_and_client

        # Create as Alice
        app.dependency_overrides[get_current_user_context] = lambda: ALICE
        resp = client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "isolation-test", "account_id": "123456789012"},
        )
        assert resp.status_code == 201
        cred_id = resp.json()["credential_id"]

        # Verify as Bob — should 404
        app.dependency_overrides[get_current_user_context] = lambda: BOB
        resp = client.post(
            "/auth/credentials/aws/verify",
            json={"credential_id": cred_id},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Issue #600 — org_id fallback for GitHub-federated users
# ---------------------------------------------------------------------------


class TestOrgIdFallback:
    """Regression tests for Issue #600: empty org_id in token falls back to users.org_id."""

    def test_connect_start_uses_db_org_id_when_token_empty(self, app_and_client, mock_sm):
        """When token_context.org_id is empty, credential is written with users.org_id."""
        app, client = app_and_client
        app.dependency_overrides[get_current_user_context] = lambda: ALICE_EMPTY_ORG

        resp = client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "fallback-test", "account_id": "123456789012"},
        )
        assert resp.status_code == 201
        cred_id = resp.json()["credential_id"]

        # Verify endpoint should find the credential even with empty org_id
        # token (because both write and read fall back to DB org_id)
        with patch("src.auth.aws_connect_routes.assume_role") as mock_assume:
            mock_assume.side_effect = _assumed_role_result
            resp2 = client.post(
                "/auth/credentials/aws/verify",
                json={"credential_id": cred_id},
            )
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "verified"

    @patch("src.auth.aws_connect_routes.assume_role")
    def test_verify_uses_db_org_id_when_token_empty(self, mock_assume, app_and_client, mock_sm):
        """Verify endpoint resolves org_id from DB when token is empty."""
        app, client = app_and_client
        mock_assume.side_effect = _assumed_role_result

        # Create credential with normal token (has org_id)
        app.dependency_overrides[get_current_user_context] = lambda: ALICE
        resp = client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "verify-fallback", "account_id": "123456789012"},
        )
        assert resp.status_code == 201
        cred_id = resp.json()["credential_id"]

        # Now verify with empty org_id token — should still find it via fallback
        app.dependency_overrides[get_current_user_context] = lambda: ALICE_EMPTY_ORG
        resp2 = client.post(
            "/auth/credentials/aws/verify",
            json={"credential_id": cred_id},
        )
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "verified"


# ---------------------------------------------------------------------------
# Tests: Issue #4742 — routing-capability probe (child G of #4692)
# ---------------------------------------------------------------------------


class TestRoutingCapabilityProbe:
    """`connect_verify` classifies a verified role as a usable Bedrock routing
    destination or not.

    The probe repeats the assume WITHOUT session tags. A v1-shaped role's trust
    policy conditions on `aws:RequestTag/adp:user_id`, so it denies an untagged
    assume; a v2 routing role has no such condition and allows it. So the two
    cases are distinguished by the `send_session_tags=False` call's outcome.

    **Two patch targets, one per call site.** #4745 moved the probe into
    `src.shared.services.routing_probe` so that the mapping-save path and this one
    share exactly one assume probe (design note §6.7 item 1). `assume_role` is
    therefore resolved in *that* module's namespace by the probe, and in this
    module's by the verify assume, so patching only one of them leaves the other
    reaching the real STS. Patching both separately is also the sharper shape: the
    tagged verify assume and the untagged probe are now distinct mocks, so a test
    can no longer confuse one for the other.
    """

    def _create_pending_credential(self, client) -> str:
        resp = client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "routing-probe", "account_id": "123456789012"},
        )
        assert resp.status_code == 201
        return resp.json()["credential_id"]

    @staticmethod
    def _probe_calls(mock_assume):
        """The assume calls made with tags suppressed — i.e. the probes."""
        return [c for c in mock_assume.call_args_list if c.kwargs.get("send_session_tags") is False]

    @patch("src.shared.services.routing_probe.assume_role")
    @patch("src.auth.aws_connect_routes.assume_role")
    def test_v2_role_classified_routing_capable(self, mock_assume, mock_probe_assume, alice_client):
        """Untagged assume succeeds → no single-user pin → routing-capable."""
        mock_assume.side_effect = _assumed_role_result
        mock_probe_assume.return_value = MagicMock()

        cred_id = self._create_pending_credential(alice_client)
        resp = alice_client.post("/auth/credentials/aws/verify", json={"credential_id": cred_id})

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "verified"
        assert body["routing_capable"] is True
        assert body["routing_reason"] is None

    @patch("src.shared.services.routing_probe.assume_role")
    @patch("src.auth.aws_connect_routes.assume_role")
    def test_v1_role_classified_not_routing_capable(self, mock_assume, mock_probe_assume, alice_client):
        """A v1-shaped role: the tagged assume works, the untagged one is denied
        by the RequestTag condition. Must be reported as NOT routing-capable with
        the re-run-v2 reason the admin UI renders."""
        from src.auth.aws_connect_routes import ROUTING_REASON_USER_PINNED
        from src.internal.sts_assume_service import STSAssumeError

        mock_assume.side_effect = _assumed_role_result
        mock_probe_assume.side_effect = STSAssumeError("denied", code="AccessDenied")

        cred_id = self._create_pending_credential(alice_client)
        resp = alice_client.post("/auth/credentials/aws/verify", json={"credential_id": cred_id})

        assert resp.status_code == 200
        body = resp.json()
        # The connection is still VERIFIED — v1 is valid for its read-only
        # purpose. Only routing is unavailable.
        assert body["status"] == "verified"
        assert body["routing_capable"] is False
        assert body["routing_reason"] == ROUTING_REASON_USER_PINNED

    @patch("src.shared.services.routing_probe.assume_role")
    @patch("src.auth.aws_connect_routes.assume_role")
    def test_probe_sends_no_session_tags(self, mock_assume, mock_probe_assume, alice_client):
        """Guards the probe's whole mechanism: if it sent tags, a v1 role would
        pass and every connection would be misreported as routing-capable."""
        mock_assume.side_effect = _assumed_role_result
        mock_probe_assume.return_value = MagicMock()

        cred_id = self._create_pending_credential(alice_client)
        alice_client.post("/auth/credentials/aws/verify", json={"credential_id": cred_id})

        probes = self._probe_calls(mock_probe_assume)
        assert len(probes) == 1, "expected exactly one untagged probe assume"
        # And the real verify assume must still be tagged.
        tagged = [c for c in mock_assume.call_args_list if c.kwargs.get("send_session_tags") is not False]
        assert len(tagged) == 1

    @patch("src.shared.services.routing_probe.assume_role")
    @patch("src.auth.aws_connect_routes.assume_role")
    def test_transient_probe_error_is_distinguishable(self, mock_assume, mock_probe_assume, alice_client):
        """Throttling is not evidence about the trust policy. Fail closed on
        routing, but with a reason that tells an operator to re-probe rather than
        to re-run CloudFormation."""
        from src.auth.aws_connect_routes import ROUTING_REASON_PROBE_INCONCLUSIVE
        from src.internal.sts_assume_service import STSAssumeError

        mock_assume.side_effect = _assumed_role_result
        mock_probe_assume.side_effect = STSAssumeError("slow down", code="Throttling")

        cred_id = self._create_pending_credential(alice_client)
        resp = alice_client.post("/auth/credentials/aws/verify", json={"credential_id": cred_id})

        body = resp.json()
        assert body["status"] == "verified"
        assert body["routing_capable"] is False
        assert body["routing_reason"] == ROUTING_REASON_PROBE_INCONCLUSIVE

    @patch("src.shared.services.routing_probe.assume_role")
    @patch("src.auth.aws_connect_routes.assume_role")
    def test_classification_persisted_and_replayed_on_reverify(self, mock_assume, mock_probe_assume, alice_client):
        """The registry reads `routing_capable` off the row, so it must persist.
        An idempotent re-verify replays it without a second probe."""
        from src.auth.aws_connect_routes import ROUTING_REASON_USER_PINNED
        from src.internal.sts_assume_service import STSAssumeError

        mock_assume.side_effect = _assumed_role_result
        mock_probe_assume.side_effect = STSAssumeError("denied", code="AccessDeniedException")

        cred_id = self._create_pending_credential(alice_client)
        alice_client.post("/auth/credentials/aws/verify", json={"credential_id": cred_id})

        mock_assume.reset_mock()
        mock_probe_assume.reset_mock()
        resp2 = alice_client.post("/auth/credentials/aws/verify", json={"credential_id": cred_id})

        body = resp2.json()
        assert body["status"] == "verified"
        assert body["routing_capable"] is False
        assert body["routing_reason"] == ROUTING_REASON_USER_PINNED
        mock_assume.assert_not_called()
        # Neither assume runs again: the replay must not re-probe either.
        mock_probe_assume.assert_not_called()

    @patch("src.shared.services.routing_probe.assume_role")
    @patch("src.auth.aws_connect_routes.assume_role")
    def test_failed_verify_does_not_probe(self, mock_assume, mock_probe_assume, alice_client):
        """No point probing a role that cannot be assumed at all — and the
        response must not claim a classification it never made."""
        from src.internal.sts_assume_service import STSAssumeError

        mock_assume.side_effect = STSAssumeError("role not found", code="NoSuchEntity")

        cred_id = self._create_pending_credential(alice_client)
        resp = alice_client.post("/auth/credentials/aws/verify", json={"credential_id": cred_id})

        body = resp.json()
        assert body["status"] == "failed"
        assert body["routing_capable"] is None
        # Asserted on the probe's OWN mock: with the probe in another module, a
        # count of this module's calls would be trivially zero and prove nothing.
        mock_probe_assume.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Issue #5182 — setup re-read, existing-role import, fresh verification
# ---------------------------------------------------------------------------


def _launch_params(launch_url: str) -> dict[str, str]:
    """The Quick-Create parameters carried in a launch URL's fragment."""
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(launch_url).fragment.split("?", 1)[1])
    return {key: values[0] for key, values in query.items()}


def _package_files(download_base64: str) -> dict[str, str]:
    """Decode the setup ZIP into ``{filename: text}``."""
    import base64
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(base64.b64decode(download_base64))) as bundle:
        return {name: bundle.read(name).decode("utf-8") for name in bundle.namelist()}


class TestConnectSetupReRead:
    """`GET /auth/credentials/aws/{id}/setup` — resume an interrupted setup, or
    hand provisioning to an AWS administrator.

    The property that matters is *sameness*: the package must describe the role
    ADP will actually assume. A fresh ExternalId or a different session tag would
    produce a role that verifies against nothing, which is the failure this
    endpoint exists to prevent.
    """

    def _connect(self, client, nickname="handoff", account_id="123456789012") -> tuple[str, str]:
        resp = client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": nickname, "account_id": account_id},
        )
        assert resp.status_code == 201
        return resp.json()["credential_id"], resp.json()["launch_url"]

    def test_preserves_external_id_and_session_tag(self, alice_client):
        """The re-read must reuse the stored ExternalId and the same users.id
        session tag — not mint new ones."""
        cred_id, connect_url = self._connect(alice_client)
        original = _launch_params(connect_url)

        resp = alice_client.get(f"/auth/credentials/aws/{cred_id}/setup")
        assert resp.status_code == 200
        body = resp.json()
        reread = _launch_params(body["launch_url"])

        assert reread["param_ExternalId"] == original["param_ExternalId"]
        assert reread["param_UserSessionTag"] == original["param_UserSessionTag"] == ALICE_DB_ID
        assert body["role_arn"] == "arn:aws:iam::123456789012:role/ADP-Agent-handoff"
        assert body["status"] == "pending"

    def test_repeated_reads_are_stable(self, alice_client):
        """An interrupted setup retried twice must not end up with two different
        ExternalIds — the second read would otherwise invalidate the first
        administrator's stack."""
        cred_id, _ = self._connect(alice_client)
        first = alice_client.get(f"/auth/credentials/aws/{cred_id}/setup").json()
        second = alice_client.get(f"/auth/credentials/aws/{cred_id}/setup").json()

        assert _launch_params(first["launch_url"])["param_ExternalId"] == _launch_params(second["launch_url"])["param_ExternalId"]

    def test_package_contains_template_parameters_and_instructions(self, alice_client):
        """The download is the administrator's whole input: template, the exact
        parameter values, and how to apply them."""
        from src.auth.aws_connect_setup import PACKAGE_FILES

        cred_id, _ = self._connect(alice_client)
        body = alice_client.get(f"/auth/credentials/aws/{cred_id}/setup").json()

        assert body["download_filename"] == "adp-aws-123456789012.zip"
        files = _package_files(body["download_base64"])
        assert set(files) == set(PACKAGE_FILES)
        # The template is the DEPLOYED object (moto seeds it as "placeholder"),
        # not a copy baked into the image.
        assert files["template.yaml"] == "placeholder"
        assert "123456789012" in files["README.md"]

    def test_package_parameters_match_the_launch_url(self, alice_client):
        """Console launch and CLI apply must create the *same* role. Divergence
        here is undetectable until verification fails."""
        cred_id, _ = self._connect(alice_client)
        body = alice_client.get(f"/auth/credentials/aws/{cred_id}/setup").json()

        parameters = {p["ParameterKey"]: p["ParameterValue"] for p in json.loads(_package_files(body["download_base64"])["parameters.json"])}
        url_params = _launch_params(body["launch_url"])
        assert parameters == {key.removeprefix("param_"): value for key, value in url_params.items() if key.startswith("param_")}
        assert parameters["UserSessionTag"] == ALICE_DB_ID
        assert parameters["ExternalId"] == url_params["param_ExternalId"]

    def test_response_is_not_cacheable(self, alice_client):
        """The body carries an ExternalId; a shared cache must not keep it."""
        cred_id, _ = self._connect(alice_client)
        resp = alice_client.get(f"/auth/credentials/aws/{cred_id}/setup")
        assert resp.headers["cache-control"] == "no-store"

    def test_other_user_setup_read_is_404(self, app_and_client):
        """Bob must not be able to read Alice's setup material — and must not be
        able to tell her connection exists. Ownership is in the query, so the id
        simply does not resolve."""
        app, client = app_and_client
        app.dependency_overrides[get_current_user_context] = lambda: ALICE
        cred_id, _ = self._connect(client)

        app.dependency_overrides[get_current_user_context] = lambda: BOB
        resp = client.get(f"/auth/credentials/aws/{cred_id}/setup")
        assert resp.status_code == 404

    def test_unknown_credential_is_404(self, alice_client):
        resp = alice_client.get("/auth/credentials/aws/does-not-exist/setup")
        assert resp.status_code == 404

    def test_imported_role_has_no_setup_package(self, alice_client):
        """ADP did not write that role's trust policy, so handing out the v1
        template as "its setup" would be a false instruction."""
        resp = alice_client.post(
            "/auth/credentials/aws/import",
            json={
                "nickname": "existing",
                "account_id": "123456789012",
                "role_arn": "arn:aws:iam::123456789012:role/SomeOtherRole",
            },
        )
        cred_id = resp.json()["credential_id"]

        setup = alice_client.get(f"/auth/credentials/aws/{cred_id}/setup")
        assert setup.status_code == 409
        assert setup.json()["detail"]["error"] == "not_provisionable"


class TestConnectImport:
    """`POST /auth/credentials/aws/import` — register a role that already exists.

    The connect flow derives the role ARN from the nickname, so it can only ever
    describe a role it named itself. A user who already has a suitable role (or
    whose administrator created one under a different name) needs this path.
    """

    ARN = "arn:aws:iam::123456789012:role/PreExisting"

    def _import(self, client, **overrides):
        body = {"nickname": "existing", "account_id": "123456789012", "role_arn": self.ARN}
        body.update(overrides)
        return client.post("/auth/credentials/aws/import", json=body)

    def test_creates_pending_canonical_credential(self, alice_client, mock_sm):
        resp = self._import(alice_client, external_id="ext-abc")
        assert resp.status_code == 201
        body = resp.json()
        assert body["reused"] is False
        assert body["role_arn"] == self.ARN

        stored = json.loads(list(mock_sm._secrets.values())[0])
        assert stored["role_arn"] == self.ARN
        assert stored["external_id"] == "ext-abc"
        assert stored["account_id"] == "123456789012"

        # Owned by Alice and resolvable as a canonical connection: the setup read
        # finds it and refuses on its *kind*, not with a 404.
        owned = alice_client.get(f"/auth/credentials/aws/{body['credential_id']}/setup")
        assert owned.status_code == 409

    def test_import_does_not_assert_the_role_works(self, alice_client):
        """Registering is not verifying. The row starts pending so nothing
        downstream treats an unproven role as usable."""
        with patch("src.auth.aws_connect_routes.assume_role") as mock_assume:
            cred_id = self._import(alice_client).json()["credential_id"]
            mock_assume.assert_not_called()

        # And it is verifiable afterwards through the existing endpoint.
        with (
            patch("src.auth.aws_connect_routes.assume_role") as mock_assume,
            patch("src.shared.services.routing_probe.assume_role") as mock_probe,
        ):
            mock_assume.side_effect = _assumed_role_result
            mock_probe.return_value = MagicMock()
            resp = alice_client.post("/auth/credentials/aws/verify", json={"credential_id": cred_id})
        assert resp.status_code == 200
        assert resp.json()["status"] == "verified"
        assert mock_assume.call_args.kwargs["role_arn"] == self.ARN

    def test_account_mismatch_is_rejected(self, alice_client, mock_sm):
        """The ARN's own account disagreeing with the stated account means the
        caller mixed up two accounts. Either interpretation stored is a
        connection that fails every assume, so store neither."""
        resp = self._import(alice_client, account_id="999888777666")
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "account_mismatch"
        assert mock_sm._secrets == {}

    @pytest.mark.parametrize(
        "role_arn",
        [
            "arn:aws:iam::123456789012:user/NotARole",
            "arn:aws:iam::123456789012:role/*",
            "PreExisting",
            "arn:aws:iam::12345:role/PreExisting",
        ],
    )
    def test_rejects_non_role_arns(self, alice_client, role_arn):
        assert self._import(alice_client, role_arn=role_arn).status_code == 422

    def test_rejects_bad_region(self, alice_client):
        assert self._import(alice_client, default_region="not a region").status_code == 422

    def test_repeat_import_reuses_the_same_connection(self, alice_client):
        """A repeated or retried command must converge, not accumulate
        duplicate connections to one role."""
        first = self._import(alice_client).json()
        second = self._import(alice_client, nickname="different-name").json()

        assert second["reused"] is True
        assert second["credential_id"] == first["credential_id"]

    def test_nickname_collision_on_a_different_role_is_409(self, alice_client):
        """The vault's uniqueness domain is (user, service, label); silently
        reusing the label would repoint an existing connection at another
        account."""
        self._import(alice_client)
        resp = self._import(alice_client, role_arn="arn:aws:iam::123456789012:role/Another")
        assert resp.status_code == 409
        assert resp.json()["detail"]["error"] == "duplicate_nickname"

    def test_reuse_is_scoped_to_the_owner(self, app_and_client):
        """Two users may each connect the same account. Bob importing the same
        ARN gets his OWN connection — he must not be handed Alice's."""
        app, client = app_and_client
        app.dependency_overrides[get_current_user_context] = lambda: ALICE
        alice_cred = self._import(client).json()

        app.dependency_overrides[get_current_user_context] = lambda: BOB
        bob_cred = self._import(client).json()

        assert bob_cred["reused"] is False
        assert bob_cred["credential_id"] != alice_cred["credential_id"]


class TestFreshVerification:
    """`fresh=True` on verify — "does this connection work *now*".

    The cached path answers "did it work once", which is the right answer to a
    double-clicked UI button and the wrong answer to an operator asking whether a
    role an administrator may since have deleted is still usable.
    """

    def _verified_credential(self, client) -> str:
        with patch("src.auth.aws_connect_routes.assume_role") as mock_assume:
            mock_assume.side_effect = _assumed_role_result
            cred_id = client.post(
                "/auth/credentials/aws/connect",
                json={"nickname": "fresh-test", "account_id": "123456789012"},
            ).json()["credential_id"]
            resp = client.post("/auth/credentials/aws/verify", json={"credential_id": cred_id})
        assert resp.json()["status"] == "verified"
        return cred_id

    @patch("src.shared.services.routing_probe.assume_role")
    @patch("src.auth.aws_connect_routes.assume_role")
    def test_fresh_reprobes_where_cached_does_not(self, mock_assume, mock_probe, alice_client):
        mock_probe.return_value = MagicMock()
        cred_id = self._verified_credential(alice_client)

        mock_assume.reset_mock()
        mock_assume.side_effect = _assumed_role_result
        alice_client.post("/auth/credentials/aws/verify", json={"credential_id": cred_id})
        mock_assume.assert_not_called()  # the UI default: replayed verdict

        resp = alice_client.post("/auth/credentials/aws/verify", json={"credential_id": cred_id, "fresh": True})
        assert resp.status_code == 200
        assert resp.json()["status"] == "verified"
        assert mock_assume.call_count == 1

    @patch("src.shared.services.routing_probe.assume_role")
    @patch("src.auth.aws_connect_routes.assume_role")
    def test_fresh_failure_downgrades_a_verified_row(self, mock_assume, mock_probe, app_and_client):
        """A role deleted in AWS must not leave the connection reading verified —
        every consumer downstream trusts that label."""
        from src.internal.sts_assume_service import STSAssumeError

        app, client = app_and_client
        app.dependency_overrides[get_current_user_context] = lambda: ALICE
        mock_probe.return_value = MagicMock()
        cred_id = self._verified_credential(client)

        mock_assume.side_effect = STSAssumeError("role not found", code="NoSuchEntity")
        resp = client.post("/auth/credentials/aws/verify", json={"credential_id": cred_id, "fresh": True})
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"
        assert load_credential(app, cred_id).aws_verified_at is None

        # The downgrade is persisted: a later cached read must not resurrect the
        # stale pass.
        mock_assume.reset_mock()
        mock_assume.side_effect = STSAssumeError("role not found", code="NoSuchEntity")
        cached = client.post("/auth/credentials/aws/verify", json={"credential_id": cred_id})
        assert cached.json()["status"] == "failed"
        assert mock_assume.called, "a downgraded row must not answer from cache"
        # And the connection reports itself as needing setup again, not verified.
        assert client.get(f"/auth/credentials/aws/{cred_id}/setup").json()["status"] == "pending"
        # Routing selectability fails closed with it: the stored classification
        # came from a probe whose premise (the assume works) no longer holds.
        assert cached.json()["routing_capable"] is None

    @patch("src.shared.services.routing_probe.assume_role")
    @patch("src.auth.aws_connect_routes.assume_role")
    def test_fresh_failure_on_a_pending_row_changes_nothing(self, mock_assume, mock_probe, alice_client):
        """An interrupted setup that has never verified is already pending; a
        failed fresh check must not invent a different state for it."""
        from src.internal.sts_assume_service import STSAssumeError

        mock_assume.side_effect = STSAssumeError("not yet", code="NoSuchEntity")
        cred_id = alice_client.post(
            "/auth/credentials/aws/connect",
            json={"nickname": "interrupted", "account_id": "123456789012"},
        ).json()["credential_id"]

        resp = alice_client.post("/auth/credentials/aws/verify", json={"credential_id": cred_id, "fresh": True})
        assert resp.json()["status"] == "failed"
        # Still resumable by its owner, still the same setup material.
        assert alice_client.get(f"/auth/credentials/aws/{cred_id}/setup").json()["status"] == "pending"

    @patch("src.shared.services.routing_probe.assume_role")
    @patch("src.auth.aws_connect_routes.assume_role")
    def test_fresh_verify_is_owner_scoped(self, mock_assume, mock_probe, app_and_client):
        app, client = app_and_client
        mock_assume.side_effect = _assumed_role_result
        mock_probe.return_value = MagicMock()
        app.dependency_overrides[get_current_user_context] = lambda: ALICE
        cred_id = self._verified_credential(client)

        app.dependency_overrides[get_current_user_context] = lambda: BOB
        resp = client.post("/auth/credentials/aws/verify", json={"credential_id": cred_id, "fresh": True})
        assert resp.status_code == 404
