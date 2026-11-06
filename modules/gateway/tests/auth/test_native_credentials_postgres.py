"""Private issuer contracts: real PostgreSQL, signed JWTs and real wire crypto.

Only network/provider boundaries are fictional. No live authority is created.
"""

import asyncio
import copy
import hashlib
import json
import runpy
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, inspect, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic.migration import MigrationContext
from alembic.operations import Operations
from src.auth import native_credential_authority as authority
from src.auth import native_credential_service as state
from src.auth import native_credentials as routes
from src.auth.aws_connection_authority import connection_binding
from src.auth.cognito_jwt import CognitoJWTValidator
from src.auth.native_credential_wire import VERSION, canonical, decode, digest, encode
from src.shared.models.base import Base
from src.shared.models.native_credentials import NativeCredentialRequest as Record
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, Team, TeamMembership, User
from src.shared.models.tool_entitlement import ToolEntitlement
from src.shared.models.vault import UserCredential, UserIdentity
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

BASE = "/auth/native-credentials"
SECRET = b"offline-private-credential-sentinel"
CONFIG = authority.NativeConfig(
    "https://gateway.example.test",
    "offline-signing-v1",
    "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001",
    "https://browser.example.test",
    "native-client",
    "browser-client",
)


class Providers:
    def __init__(self, loop):
        self.loop = loop
        self.signer = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.policy_value = dict(
            status="active",
            revision=Decimal(7),
            allowed_tools=["credentials.assume_role", "credentials.materialize"],
            consented_tools=["credentials.assume_role", "credentials.materialize"],
        )
        self.calls = []
        self.effects = []
        self.revoked = False
        self.version = "version-one"
        self.secret_value = SECRET
        self.hook = None
        self.expiry_delta = 0

    def current_session(self, actor):
        self.calls.append("session")
        authority.require(not self.revoked, "native_human_session_required", 401)
        actor.fresh()

    def policy(self, tenant_id, user_id):
        assert tenant_id == "tenant" and user_id in {"owner", "other"}
        self.calls.append("policy")
        return copy.deepcopy(self.policy_value)

    def effect(self, name):
        self.calls.append(name)
        self.effects.append(name)
        if self.hook:
            asyncio.run_coroutine_threadsafe(self.hook(name), self.loop).result(timeout=5)

    def public_key(self, config):
        self.effect("key")
        return self.signer.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def sign(self, config, message):
        signature = self.signer.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
        self.effect("sign")
        return signature

    def secret_version(self, arn):
        self.effect("version")
        return self.version

    def secret(self, arn, version):
        assert version == self.version
        self.effect("secret")
        return self.secret_value

    def assume(self, **kwargs):
        assert kwargs["user_id"] == "owner" and kwargs["receipt_id"]
        self.effect("sts")
        return dict(
            AccessKeyId="offline-access-key",
            SecretAccessKey=SECRET.decode(),
            SessionToken="offline-session",
            Expiration=datetime.fromtimestamp(int(time.time()) + 900 + self.expiry_delta, UTC),
        )


@pytest.fixture
async def native(pg_url, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ADP_GATEWAY_ACCOUNT_ID", "234567890123")
    monkeypatch.delenv("ADP_GATEWAY_ROLE_ARN", raising=False)
    engine = create_async_engine(to_async_url(pg_url))
    migration = runpy.run_path(str(Path(__file__).resolve().parents[2] / "alembic/versions/108_native_credentials.py"))

    def schema(connection):
        Base.metadata.create_all(
            connection,
            tables=[m.__table__ for m in (Organization, Team, User, TeamMembership, TenantMembership, UserIdentity, UserCredential, ToolEntitlement)],
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration["upgrade"]()

    async with engine.begin() as connection:
        await connection.run_sync(schema)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        db.add(Organization(id="tenant", name="Fictional native issuer tests"))
        db.add(Team(id="team", org_id="tenant", department_id="department", name="Test team"))
        for user in ("owner", "other"):
            db.add(
                User(
                    id=user,
                    cognito_sub="subject-" + user,
                    org_id="tenant",
                    team_id="team",
                    email=user + "@example.test",
                    user_kind="human",
                    is_shadow=False,
                )
            )
        await db.flush()
        for user in ("owner", "other"):
            db.add(TenantMembership(user_id=user, tenant_id="tenant", is_active=True))
            db.add(TeamMembership(user_id=user, org_id="tenant", team_id="team"))
        db.add(
            UserCredential(
                id="credential",
                org_id="tenant",
                user_id="owner",
                service="ssh",
                label="test",
                credential_type="ssh_key",
                secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:offline",
                agent_consent=True,
            )
        )
        for operation in ("assume_role", "materialize"):
            db.add(ToolEntitlement(org_id="tenant", team_id="", tool_name="credentials." + operation, enabled=True))
    providers = Providers(asyncio.get_running_loop())
    service = state.NativeCredentialService(factory, CONFIG, providers)
    monkeypatch.setattr(routes, "service", lambda: service)
    signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    validator = CognitoJWTValidator(
        user_pool_id="us-east-1_offline", client_id="browser-client", allowed_client_ids=["native-client"], region="us-east-1", agent_clients_table=""
    )
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing.public_key()))
    jwk["kid"] = "offline"
    validator.jwk_client.fetch_data = lambda: {"keys": [jwk]}
    monkeypatch.setattr(authority, "get_cognito_validator", lambda: validator)

    def token(client="native-client", **claims):
        now = int(time.time())
        payload = {
            "sub": "subject-owner",
            "iss": validator.issuer,
            "client_id": client,
            "token_use": "access",
            "auth_time": now,
            "exp": now + 3600,
            "iat": now,
            "jti": client + "-session",
            "username": "owner@example.test",
            "custom:org_id": "tenant",
            "custom:account_type": "human",
            **claims,
        }
        return jwt.encode(payload, signing, algorithm="RS256", headers={"kid": "offline"})

    app = FastAPI()
    app.include_router(routes.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=CONFIG.issuer) as client:

        async def call(method, path, value=None, *, browser=False, bearer=None, origin=True, **kwargs):
            headers = {"Authorization": "Bearer " + (bearer or token("browser-client" if browser else "native-client"))}
            if browser and origin:
                headers["Origin"] = CONFIG.approval_origin
            return await client.request(method, BASE + path, headers=headers, json=value, **kwargs)

        yield SimpleNamespace(
            engine=engine, factory=factory, providers=providers, service=service, token=token, call=call, client=client, migration=migration
        )
    await engine.dispose()


def create_body(**changes):
    return dict(
        request_id=str(uuid4()),
        credential_id="credential",
        operation="materialize",
        service="ssh",
        label="test",
        argv=["/usr/bin/true"],
        cwd="/tmp",
        use_seconds=300,
        **changes,
    )


async def created(native, value=None):
    result = await native.call("POST", "/requests", value or create_body())
    assert result.status_code == 200, result.text
    return result.json()


def recipient(selection):
    encryption, proof = x25519.X25519PrivateKey.generate(), ed25519.Ed25519PrivateKey.generate()
    keys = dict(
        encryption_public_key=encode(encryption.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)),
        proof_public_key=encode(proof.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)),
    )
    offer = dict(
        version=VERSION,
        selection=selection,
        command_digest="c" * 64,
        sink_digest="d" * 64,
        nonce=encode(secrets.token_bytes(32)),
        recipient_fingerprint=digest(keys),
        **keys,
    )
    return SimpleNamespace(offer=offer, encryption=encryption, signer=proof)


async def enrolled(native, value=None):
    result = await created(native, value)
    host = recipient(result["selection"])
    receipt = result["receipt_id"]
    response = await native.call("POST", f"/requests/{receipt}/offer", {"offer": host.offer})
    assert response.status_code == 200, response.text
    preview = await native.call("GET", f"/approvals/{receipt}", browser=True)
    assert preview.status_code == 200, preview.text
    return result, host, preview.json()


async def approved(native, value=None):
    result, host, preview = await enrolled(native, value)
    receipt = result["receipt_id"]
    decision = dict(action="approve", snapshot=preview["snapshot"], approval_nonce=preview["approval_nonce"])
    response = await native.call("POST", f"/approvals/{receipt}", decision, browser=True)
    assert response.status_code == 200, response.text
    challenge = await native.call("POST", f"/requests/{receipt}/challenge", {})
    assert challenge.status_code == 200, challenge.text
    proof = dict(version=VERSION, offer=host.offer, challenge=challenge.json()["challenge"])
    host.proof = dict(type="proof", proof=proof, signature=encode(host.signer.sign(canonical(proof))))
    return result, host


async def sql(native, statement):
    async with native.factory() as db, db.begin():
        await db.execute(statement)


async def aws(native):
    role = "arn:aws:iam::123456789012:role/OfflineCustomerRole"
    external = "00000000-0000-0000-0000-000000000009"
    async with native.factory() as db, db.begin():
        row = await db.get(UserCredential, "credential")
        row.service, row.credential_type = "aws", "aws_role"
        row.scopes = dict(status="verified", account_id="123456789012", role_arn=role)
        row.aws_external_id, row.aws_verified_version_id = external, native.providers.version
        row.aws_verified_at, row.aws_verification_attempt = datetime.now(UTC), "verification-attempt"
        row.aws_verified_binding = connection_binding(row)
    native.providers.secret_value = json.dumps(dict(account_id="123456789012", role_arn=role, external_id=external)).encode()
    value = create_body()
    value.update(operation="assume_role", service="aws", use_seconds=900)
    return value


def decrypt(native, host, envelope):
    body = envelope["body"]
    native.providers.signer.public_key().verify(
        decode(envelope["signature"]), canonical(body), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256()
    )
    aad = canonical({key: value for key, value in body.items() if key != "ciphertext"})
    shared = host.encryption.exchange(x25519.X25519PublicKey.from_public_bytes(decode(body["ephemeral_public_key"], 32)))
    key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=decode(host.offer["nonce"], 32), info=VERSION.encode() + hashlib.sha256(aad).digest()
    ).derive(shared)
    return json.loads(AESGCM(key).decrypt(decode(body["iv"], 12), decode(body["ciphertext"]), aad))


@pytest.mark.parametrize("operation", ["materialize", "assume_role"])
async def test_real_private_envelope_and_sql_never_recover_secret(native, operation):
    value = await aws(native) if operation == "assume_role" else create_body()
    result, host = await approved(native, value)
    receipt = result["receipt_id"]
    issued = await native.call("POST", f"/requests/{receipt}/issue", host.proof)
    assert issued.status_code == 200, issued.text
    envelope = issued.json()
    payload = decrypt(native, host, envelope)
    assert envelope["body"]["offer"] == host.offer
    assert envelope["body"]["challenge"] == host.proof["proof"]["challenge"]
    assert issued.headers["cache-control"] == "no-store"
    if operation == "materialize":
        assert decode(payload["file_base64"]) == SECRET
        assert native.providers.effects.count("sts") == 0
    else:
        assert payload == dict(access_key_id="offline-access-key", secret_access_key=SECRET.decode(), session_token="offline-session")
        assert envelope["body"]["credential_expires_at"] - envelope["body"]["issued_at"] == 900
        assert native.providers.effects.count("sts") == 1
    assert native.providers.effects.count("secret") == 1
    async with native.factory() as db:
        serialized = (await db.execute(text("SELECT row_to_json(r)::text FROM native_credential_requests r"))).scalar_one()
    for forbidden in (SECRET.decode(), encode(SECRET), envelope["body"]["ciphertext"], envelope["signature"], "s3://", "https://s3"):
        assert forbidden not in serialized
    status = await native.call("GET", f"/requests/{receipt}")
    assert status.json()["status"] == "delivered"
    assert set(status.json()) == {"execution_id", "request_id", "receipt_id", "status", "selection", "snapshot", "expires_at"}
    before = list(native.providers.effects)
    assert (await native.call("POST", f"/requests/{receipt}/issue", host.proof)).status_code == 409
    assert native.providers.effects == before


async def test_concurrent_create_and_issue_have_one_original_record_effect(native):
    value = create_body()
    results = await asyncio.gather(*(native.call("POST", "/requests", value) for _ in range(5)))
    assert all(result.status_code == 200 for result in results), [result.text for result in results]
    assert len({r.json()["receipt_id"] for r in results}) == 1
    result, host = await approved(native, value)
    issued = await asyncio.gather(*(native.call("POST", f"/requests/{result['receipt_id']}/issue", host.proof) for _ in range(4)))
    assert sorted(r.status_code for r in issued) == [200, 409, 409, 409]
    assert native.providers.effects.count("secret") == 1
    conflict = await native.call("POST", "/requests", {**value, "argv": ["/usr/bin/false"]})
    assert conflict.status_code == 409


@pytest.mark.parametrize(
    "claims",
    [
        {"token_use": "id", "aud": "native-client"},
        {"client_id": "browser-client"},
        {"custom:account_type": "service"},
        {"auth_time": 0},
        {"username": ""},
        {"jti": ""},
        {"custom:org_id": ""},
        {"exp": 1},
        {"iss": "https://attacker.example.test"},
    ],
)
async def test_genuine_human_native_jwt_required(native, claims):
    client = claims.pop("client_id", "native-client")
    response = await native.call("POST", "/requests", create_body(), bearer=native.token(client, **claims))
    assert response.status_code == 401
    assert native.providers.effects == []


@pytest.mark.parametrize("kind", ["legacy", "wrapped", "signature", "other_user", "other_tenant", "revoked", "shadow", "bot", "raw_sub_alias"])
async def test_no_legacy_service_or_identity_alias_authority(native, kind):
    bearer = native.token()
    if kind == "legacy":
        bearer = "bg-local-token"
    elif kind == "wrapped":
        bearer = "adpctx1~" + bearer
    elif kind == "signature":
        bearer = bearer.rsplit(".", 1)[0] + "." + encode(b"wrong-signature")
    elif kind == "other_user":
        bearer = native.token(sub="subject-other")
    elif kind == "other_tenant":
        bearer = native.token(**{"custom:org_id": "another"})
    elif kind == "revoked":
        native.providers.revoked = True
    elif kind == "shadow":
        await sql(native, update(User).where(User.id == "owner").values(is_shadow=True))
    elif kind == "bot":
        await sql(native, update(User).where(User.id == "owner").values(user_kind="bot"))
    elif kind == "raw_sub_alias":
        bearer = native.token(sub="owner")
    response = await native.call("POST", "/requests", create_body(), bearer=bearer)
    assert response.status_code in {401, 403}
    assert native.providers.effects == []


@pytest.mark.parametrize("change", ["browser_client", "other_user", "origin", "nonce", "snapshot", "new_browser_session"])
async def test_browser_approval_is_exact_independent_human_interaction(native, change):
    result, host, preview = await enrolled(native)
    value = dict(action="approve", snapshot=preview["snapshot"], approval_nonce=preview["approval_nonce"])
    kwargs = dict(browser=True)
    if change == "browser_client":
        kwargs["bearer"] = native.token()
    elif change == "other_user":
        kwargs["bearer"] = native.token("browser-client", sub="subject-other")
    elif change == "origin":
        kwargs["origin"] = False
    elif change == "nonce":
        value["approval_nonce"] = "wrong"
    elif change == "snapshot":
        value["snapshot"] = "0" * 64
    elif change == "new_browser_session":
        kwargs["bearer"] = native.token("browser-client", jti="renewed")
    receipt = result["receipt_id"]
    response = await native.call("POST", f"/approvals/{receipt}", value, **kwargs)
    assert response.status_code in {401, 403, 404, 409}
    assert (await native.call("POST", f"/requests/{receipt}/challenge", {})).status_code == 409
    assert native.providers.effects == []


@pytest.mark.parametrize(
    "field", ["encryption_public_key", "proof_public_key", "command_digest", "sink_digest", "selection", "challenge", "signature"]
)
async def test_proof_swaps_never_consume_original_or_issue(native, field):
    result, host = await approved(native)
    value = copy.deepcopy(host.proof)
    if field == "signature":
        value[field] = encode(b"x" * 64)
    elif field == "challenge":
        value["proof"][field] = encode(b"x" * 32)
    elif field == "selection":
        value["proof"]["offer"][field]["argv"] = ["/usr/bin/false"]
    else:
        value["proof"]["offer"][field] = "f" * 64
    response = await native.call("POST", f"/requests/{result['receipt_id']}/issue", value)
    assert response.status_code in {400, 503}
    assert native.providers.effects == []
    assert (await native.call("GET", f"/requests/{result['receipt_id']}")).json()["status"] == "challenged"


@pytest.mark.parametrize(
    "change", ["policy", "consent", "owner", "tenant_membership", "team_membership", "entitlement", "credential_expiry", "session", "version"]
)
async def test_current_authority_rechecked_after_secret_before_further_effect(native, change):
    result, host = await approved(native)

    async def revoke(name):
        if name != "secret":
            return
        if change == "policy":
            native.providers.policy_value["revision"] += 1
        elif change == "consent":
            await sql(native, update(UserCredential).values(agent_consent=False))
        elif change == "owner":
            await sql(native, update(UserCredential).values(user_id="other"))
        elif change == "tenant_membership":
            await sql(native, update(TenantMembership).values(revoked_at=datetime.now(UTC)))
        elif change == "team_membership":
            await sql(native, delete(TeamMembership))
        elif change == "entitlement":
            await sql(native, update(ToolEntitlement).values(enabled=False))
        elif change == "credential_expiry":
            await sql(native, update(UserCredential).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        elif change == "session":
            native.providers.revoked = True
        elif change == "version":
            native.providers.version = "changed"

    native.providers.hook = revoke
    receipt = result["receipt_id"]
    issued = await native.call("POST", f"/requests/{receipt}/issue", host.proof)
    assert issued.status_code == 503
    assert issued.json() == {"detail": {"error": "native_private_delivery_unknown"}}
    assert "sign" not in native.providers.effects and "sts" not in native.providers.effects
    async with native.factory() as db:
        assert (await db.get(Record, receipt)).status == "unknown"


@pytest.mark.parametrize("boundary", ["sts", "sign"])
async def test_revoked_authority_after_provider_effect_prevents_private_release(native, boundary):
    result, host = await approved(native, await aws(native))

    async def revoke(name):
        if name == boundary:
            native.providers.policy_value["revision"] += 1

    native.providers.hook = revoke
    response = await native.call("POST", f"/requests/{result['receipt_id']}/issue", host.proof)
    assert response.status_code == 503
    assert native.providers.effects.count("sts") == 1
    if boundary == "sts":
        assert "sign" not in native.providers.effects
    assert (await native.call("POST", f"/requests/{result['receipt_id']}/issue", host.proof)).status_code == 403


async def test_scheduled_effect_rechecks_after_queue_delay(native, monkeypatch):
    result, host = await approved(native)
    blocker = threading.Event()
    entered = asyncio.Event()
    pool = ThreadPoolExecutor(max_workers=1)
    occupied = pool.submit(blocker.wait)
    monkeypatch.setattr(state, "_EFFECTS", pool)
    original = native.service.effect

    async def effect(*args):
        entered.set()
        return await original(*args)

    monkeypatch.setattr(native.service, "effect", effect)
    task = asyncio.create_task(native.call("POST", f"/requests/{result['receipt_id']}/issue", host.proof))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await sql(native, update(UserCredential).values(agent_consent=False))
        blocker.set()
        assert (await asyncio.wait_for(task, 5)).status_code == 503
        assert native.providers.effects == []
    finally:
        blocker.set()
        occupied.result(timeout=5)
        pool.shutdown(wait=True)


@pytest.mark.parametrize("delta", [-30, 30, 60, 0.5])
async def test_actual_sts_expiration_cannot_be_relabelled(native, delta):
    result, host = await approved(native, await aws(native))
    native.providers.expiry_delta = delta
    response = await native.call("POST", f"/requests/{result['receipt_id']}/issue", host.proof)
    assert response.status_code == 503
    assert native.providers.effects.count("sts") == 1 and "sign" not in native.providers.effects


async def test_renewed_native_token_can_reconcile_metadata_but_not_issue(native):
    value = create_body()
    result, host = await approved(native, value)
    bearer = native.token(jti="new-native-session")
    receipt = result["receipt_id"]
    for path in (f"/requests/{receipt}", f"/requests/by-request/{value['request_id']}"):
        response = await native.call("GET", path, bearer=bearer)
        assert response.status_code == 200 and response.json()["receipt_id"] == receipt
    assert (await native.call("POST", f"/requests/{receipt}/issue", host.proof, bearer=bearer)).status_code == 403
    assert native.providers.effects == []


async def test_ack_only_static_original_outcome_no_unknown_retry(native):
    result, host = await approved(native)
    receipt = result["receipt_id"]
    assert (await native.call("POST", f"/requests/{receipt}/issue", host.proof)).status_code == 200
    outcome = {k: result[k] for k in ("request_id", "execution_id", "receipt_id")}
    outcome.update(type="result", delivery_state="completed", code="child_exited", cleanup="complete", exit_code=0)
    assert (await native.call("POST", f"/requests/{receipt}/ack", {**outcome, "stdout": SECRET.decode()})).status_code == 400
    assert (await native.call("POST", f"/requests/{receipt}/ack", {**outcome, "request_id": str(uuid4())})).status_code == 400
    response = await native.call("POST", f"/requests/{receipt}/ack", outcome)
    assert response.json() == dict(receipt_id=receipt, status="acknowledged")
    assert (await native.call("POST", f"/requests/{receipt}/ack", outcome)).json() == response.json()
    preview = await native.call("GET", f"/approvals/{receipt}", browser=True)
    assert preview.json()["status"] == "acknowledged" and preview.json()["approval_nonce"] == ""
    assert (await native.call("POST", f"/requests/{receipt}/issue", host.proof)).status_code == 409


@pytest.mark.parametrize("revision", [True, Decimal("NaN"), Decimal("1.5"), -1, "7"])
async def test_malformed_policy_revision_fails_closed(native, revision):
    native.providers.policy_value["revision"] = revision
    response = await native.call("POST", "/requests", create_body())
    assert response.status_code == 403
    assert native.providers.effects == []


async def test_error_sanitization_does_not_echo_provider_secret(native, caplog):
    result, host = await approved(native)

    async def fail(name):
        if name == "secret":
            raise RuntimeError(SECRET.decode())

    native.providers.hook = fail
    response = await native.call("POST", f"/requests/{result['receipt_id']}/issue", host.proof)
    assert response.status_code == 503
    assert SECRET.decode() not in response.text and SECRET.decode() not in caplog.text
    before = list(native.providers.effects)
    assert (await native.call("POST", f"/requests/{result['receipt_id']}/issue", host.proof)).status_code == 409
    assert native.providers.effects == before


async def test_migration_roundtrip_and_model_parity(native):
    async with native.engine.begin() as connection:

        def check(conn):
            columns = inspect(conn).get_columns("native_credential_requests")
            assert {c["name"] for c in columns} == set(Record.__table__.columns.keys())
            indexes = inspect(conn).get_indexes("native_credential_requests")
            assert any(i["unique"] and i["column_names"] == ["org_id", "user_id", "request_id"] for i in indexes)
            with Operations.context(MigrationContext.configure(conn)):
                native.migration["downgrade"]()
                assert "native_credential_requests" not in inspect(conn).get_table_names()
                native.migration["upgrade"]()

        await connection.run_sync(check)


async def test_independent_config_key_and_browser_client_separation(native):
    response = await native.call("GET", "/config")
    assert response.status_code == 200
    assert response.json() == dict(
        version=VERSION,
        issuer=CONFIG.issuer,
        audience="adp-native-private-host",
        key_id=CONFIG.key_id,
        approval_origin=CONFIG.approval_origin,
        issuer_key_pem=native.providers.signer.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode(),
    )
    assert response.headers["cache-control"] == "no-store"
    assert (await native.call("GET", "/config", browser=True)).status_code == 401
    assert native.providers.effects == ["key"]


@pytest.mark.parametrize("action", ["approve", "deny"])
async def test_concurrent_browser_decision_consumes_nonce_exactly_once(native, action):
    result, host, preview = await enrolled(native)
    receipt = result["receipt_id"]
    value = dict(action=action, snapshot=preview["snapshot"], approval_nonce=preview["approval_nonce"])
    responses = await asyncio.gather(*(native.call("POST", f"/approvals/{receipt}", value, browser=True) for _ in range(3)))
    assert sorted(r.status_code for r in responses) == [200, 409, 409]
    assert native.providers.effects == []
    if action == "deny":
        assert (await native.call("POST", f"/requests/{receipt}/challenge", {})).status_code == 409
        assert (await native.call("GET", f"/approvals/{receipt}", browser=True)).json()["approval_nonce"] == ""


async def test_challenge_replay_is_metadata_only_and_expiration_cannot_renew(native):
    result, host = await approved(native)
    receipt = result["receipt_id"]
    response = await native.call("POST", f"/requests/{receipt}/challenge", {})
    assert response.json()["challenge"] == host.proof["proof"]["challenge"]
    await sql(native, update(Record).values(challenge_expires_at=1))
    assert (await native.call("POST", f"/requests/{receipt}/challenge", {})).status_code == 409
    assert (await native.call("POST", f"/requests/{receipt}/issue", host.proof)).status_code == 409
    assert native.providers.effects == []


@pytest.mark.parametrize("swap", ["command", "recipient", "low_order", "selection"])
async def test_offer_is_immutable_and_low_order_recipient_refused_before_approval(native, swap):
    result, host, _ = await enrolled(native)
    changed = copy.deepcopy(host.offer)
    if swap == "command":
        changed["command_digest"] = "f" * 64
    elif swap == "recipient":
        changed = recipient(result["selection"]).offer
    elif swap == "selection":
        changed["selection"]["credential_id"] = "other"
    elif swap == "low_order":
        changed["encryption_public_key"] = encode(bytes(32))
        changed["recipient_fingerprint"] = digest({k: changed[k] for k in ("encryption_public_key", "proof_public_key")})
    response = await native.call("POST", f"/requests/{result['receipt_id']}/offer", {"offer": changed})
    assert response.status_code in {400, 409}
    assert native.providers.effects == []


@pytest.mark.parametrize(
    "raw",
    [
        b'{"request_id":"x","request_id":"y"}',
        b'{"secret":"offline-private-credential-sentinel"}',
        b'{"use_seconds":true}',
        b'{"use_seconds":NaN}',
        b'{"x":null}',
        b"x" * 196609,
    ],
)
async def test_bounded_strict_parser_does_not_echo_input(native, raw, caplog):
    response = await native.client.post(
        BASE + "/requests", content=raw, headers={"Authorization": "Bearer " + native.token(), "Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json() == {"detail": {"error": "native_request_invalid"}}
    assert SECRET.decode() not in response.text and SECRET.decode() not in caplog.text
    assert native.providers.effects == []


async def test_unknown_ack_records_observation_but_preserves_unknown(native):
    result, host = await approved(native)

    async def fail(name):
        if name == "secret":
            raise TimeoutError

    native.providers.hook = fail
    receipt = result["receipt_id"]
    assert (await native.call("POST", f"/requests/{receipt}/issue", host.proof)).status_code == 503
    outcome = {k: result[k] for k in ("request_id", "execution_id", "receipt_id")}
    outcome.update(type="result", delivery_state="unknown", code="delivery_acknowledgement_lost", cleanup="complete")
    response = await native.call("POST", f"/requests/{receipt}/ack", outcome)
    assert response.json() == dict(receipt_id=receipt, status="unknown")
    assert (await native.call("GET", f"/requests/{receipt}")).json()["status"] == "unknown"
    assert (await native.call("POST", f"/requests/{receipt}/issue", host.proof)).status_code == 409


async def test_expired_offer_is_terminal_metadata_only(native):
    result, host, _ = await enrolled(native)
    receipt = result["receipt_id"]
    await sql(native, update(Record).values(expires_at=1))
    preview = await native.call("GET", f"/approvals/{receipt}", browser=True)
    assert preview.json()["status"] == "expired" and preview.json()["approval_nonce"] == ""
    assert (await native.call("POST", f"/requests/{receipt}/offer", {"offer": host.offer})).status_code == 409
    assert native.providers.effects == []


@pytest.mark.parametrize("ack_state", ["acknowledged", "unknown"])
@pytest.mark.parametrize("cleanup", ["complete", "unknown"])
async def test_refreshed_status_returns_only_original_static_command_outcome(native, ack_state, cleanup):
    result, host = await approved(native)
    receipt = result["receipt_id"]
    if ack_state == "acknowledged":
        assert (await native.call("POST", f"/requests/{receipt}/issue", host.proof)).status_code == 200
    else:
        await sql(native, update(Record).values(status="unknown"))
    acknowledgment = {key: result[key] for key in ("execution_id", "request_id", "receipt_id")}
    acknowledgment.update(
        type="result",
        delivery_state="completed" if ack_state == "acknowledged" else "unknown",
        code="child_exited" if ack_state == "acknowledged" else "delivery_acknowledgement_lost",
        cleanup=cleanup,
    )
    if ack_state == "acknowledged":
        acknowledgment["exit_code"] = 7
    assert (await native.call("POST", f"/requests/{receipt}/ack", acknowledgment)).status_code == 200
    bearer = native.token(jti="renewed-native-session")
    for path in (f"/requests/{receipt}", f"/requests/by-request/{result['request_id']}"):
        response = await native.call("GET", path, bearer=bearer)
        assert response.status_code == 200
        assert response.json()["status"] == ack_state
        assert response.json()["acknowledgment"] == acknowledgment
        assert response.json()["acknowledgment_source"] == "client_reported"
        assert response.headers["cache-control"] == "no-store"
    preview = await native.call("GET", f"/approvals/{receipt}", browser=True)
    assert preview.json()["acknowledgment"] == acknowledgment and preview.json()["approval_nonce"] == ""
    assert preview.json()["acknowledgment_source"] == "client_reported"


@pytest.mark.parametrize("corruption", ["extra", "code", "ids", "exit_bool", "exit_range", "state", "cleanup"])
async def test_stored_acknowledgment_is_revalidated_before_status_projection(native, corruption):
    result = await created(native)
    acknowledgment = {key: result[key] for key in ("execution_id", "request_id", "receipt_id")}
    acknowledgment.update(type="result", delivery_state="completed", code="child_exited", cleanup="complete", exit_code=0)
    if corruption == "extra":
        acknowledgment["stdout"] = SECRET.decode()
    elif corruption == "code":
        acknowledgment["code"] = SECRET.decode()
    elif corruption == "ids":
        acknowledgment["receipt_id"] = "different"
    elif corruption == "exit_bool":
        acknowledgment["exit_code"] = True
    elif corruption == "exit_range":
        acknowledgment["exit_code"] = 999
    elif corruption == "state":
        acknowledgment["delivery_state"] = SECRET.decode()
    elif corruption == "cleanup":
        acknowledgment["cleanup"] = SECRET.decode()
    await sql(native, update(Record).values(status="acknowledged", acknowledgment=acknowledgment))
    response = await native.call("GET", f"/requests/{result['receipt_id']}")
    assert response.status_code == 503
    assert response.json() == {"detail": {"error": "native_acknowledgment_unavailable"}}
    assert SECRET.decode() not in response.text


@pytest.mark.parametrize("argv,accepted", [(["/usr/bin/true", ""], True), (["", "argument"], False), (["/usr/bin/true", "\x00"], False)])
async def test_empty_quoted_argument_is_preserved_but_executable_and_nul_still_refuse(native, argv, accepted):
    value = create_body()
    value["argv"] = argv
    response = await native.call("POST", "/requests", value)
    assert response.status_code == (200 if accepted else 400)
    if accepted:
        assert response.json()["selection"]["argv"] == argv
    assert native.providers.effects == []
