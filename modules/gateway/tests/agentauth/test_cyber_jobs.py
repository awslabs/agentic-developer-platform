"""Real GitHub signatures + SQL ownership + versioned Moto S3 + SQS delivery."""

import base64
import hashlib
import json
import time
from datetime import UTC, datetime
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from moto import mock_aws

from src.agentauth import cyber_jobs
from src.agentauth.bootstrap import envelope_digest
from src.shared.database import get_db
from src.shared.models.organization import Organization, Team, TeamMembership, User
from src.shared.models.vault import UserIdentity
from tests.agentauth.test_work_producer import ROLE, proof

KEY = "o/tenant/t/team/u/sub/s/session/task/in/sample.bin"
BASE = "/internal/v1/agent/arc/cyber/"


@pytest.fixture
async def context(db_session, monkeypatch):
    db_session.add_all(
        [
            Organization(id="tenant", name="Tenant"),
            User(id="human", cognito_sub="sub", org_id="tenant", team_id="team", email="h@example.test", user_kind="human"),
        ]
    )
    await db_session.commit()
    db_session.add(Team(id="team", org_id="tenant", department_id="dept", name="Team"))
    await db_session.commit()
    db_session.add(TeamMembership(user_id="human", team_id="team", org_id="tenant"))
    await db_session.commit()
    db_session.add(
        UserIdentity(
            user_id="human",
            org_id="tenant",
            team_id="team",
            provider="github",
            provider_user_id="42",
            verification_method="admin_manual",
            verified_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing.public_key())) | {"kid": "github-test"}
    now = int(time.time())
    claims = dict(
        iss="https://token.actions.githubusercontent.com",
        aud="adp-agent-model-policy",
        iat=now,
        nbf=now,
        exp=now + 300,
        sub="repo:org/repo:ref:refs/heads/main",
        repository="org/repo",
        repository_id="123",
        workflow_ref="org/repo/.github/workflows/malware-analysis-agent.yml@refs/heads/main",
        run_id="456",
        run_attempt="1",
        actor_id="42",
        event_name="issues",
    )
    monkeypatch.setenv(
        "ADP_ARC_MODEL_BINDINGS",
        json.dumps(
            [
                dict(
                    repository_id="123",
                    repository="org/repo",
                    runner_role=ROLE,
                    workflow_ref=claims["workflow_ref"],
                    tenant_id="tenant",
                    persona="malware-analysis-agent",
                )
            ]
        ),
    )
    monkeypatch.setenv("CYBER_SAMPLE_BUCKET", "test-samples")
    original = httpx.AsyncClient

    def remote(request):
        if request.url.host == "token.actions.githubusercontent.com":
            return httpx.Response(200, json={"keys": [jwk]})
        assert request.url.host == "sts.us-east-1.amazonaws.com"
        return httpx.Response(
            200,
            text="<GetCallerIdentityResponse><GetCallerIdentityResult><Arn>"
            "arn:aws:sts::123456789012:assumed-role/webhook/session</Arn></GetCallerIdentityResult></GetCallerIdentityResponse>",
        )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(remote), **kw))
    with mock_aws():
        clients = cyber_jobs.cyber_clients()
        s3 = clients["s3"]
        s3.create_bucket(Bucket="test-samples")
        s3.put_bucket_versioning(Bucket="test-samples", VersioningConfiguration={"Status": "Enabled"})
        version = s3.put_object(Bucket="test-samples", Key=KEY, Body=b"original sample")["VersionId"]
        queue = clients["sqs"].create_queue(QueueName="cyber.fifo", Attributes={"FifoQueue": "true"})["QueueUrl"]
        monkeypatch.setenv("CYBER_TRIAGE_QUEUE", queue)
        monkeypatch.setenv("CYBER_STATIC_QUEUE", queue)
        monkeypatch.setenv("CYBER_RESULTS_TABLE", "results")
        app = FastAPI()
        app.include_router(cyber_jobs.router)
        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[cyber_jobs.cyber_clients] = lambda: clients
        async with original(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as client:
            yield dict(client=client, claims=claims, signing=signing, clients=clients, queue=queue, version=version)


async def post(ctx, operation="jobs", changes=None, claims=None, proof_digest=None):
    body = dict(artifact_id="sample-one", sample_s3_uri="s3://test-samples/" + KEY, stage="triage", script_base64=None, focus=[], yara_rules=[])
    if operation == "result":
        body = {}
    body.update(changes or {})
    body["github_oidc_token"] = jwt.encode(ctx["claims"] | (claims or {}), ctx["signing"], algorithm="RS256", headers={"kid": "github-test"})
    return await ctx["client"].post(BASE + operation, json=body, headers={"X-Adp-Producer-Proof": proof(proof_digest or envelope_digest(body))})


def delivered(ctx):
    return ctx["clients"]["sqs"].receive_message(QueueUrl=ctx["queue"]).get("Messages", [])


async def test_registered_job_binds_owner_run_and_immutable_version(context):
    response = await post(context)
    assert response.status_code == 200, response.text
    manifest = json.loads(delivered(context)[0]["Body"])
    assert manifest["artifact_id"] == response.json()["job_id"]
    assert (manifest["org_id"], manifest["team_id"], manifest["user_id"]) == ("tenant", "team", "sub")
    assert manifest["sample_download"]["sha256"] == hashlib.sha256(b"original sample").hexdigest()
    assert parse_qs(urlsplit(manifest["sample_download"]["url"]).query)["versionId"] == [context["version"]]
    context["clients"]["s3"].put_object(Bucket="test-samples", Key=KEY, Body=b"replacement")
    assert (
        context["clients"]["s3"].get_object(Bucket="test-samples", Key=KEY, VersionId=manifest["sample_download"]["version"])["Body"].read()
        == b"original sample"
    )


@pytest.mark.parametrize(
    "uri",
    [
        "s3://other/" + KEY,
        "s3://test-samples/" + KEY.replace("tenant", "victim"),
        "s3://test-samples/" + KEY.replace("/team/", "/victim-team/"),
        "s3://test-samples/" + KEY.replace("/sub/", "/victim-user/"),
        "s3://test-samples/" + KEY.replace("/in/", "/out/"),
        "s3://test-samples/" + KEY + "?versionId=stolen",
        "s3://test-samples/flat/sample.bin",
    ],
)
async def test_ownership_refused_before_any_storage_read(context, uri, monkeypatch):
    head = Mock(side_effect=AssertionError("unauthorized S3 read"))
    monkeypatch.setattr(context["clients"]["s3"], "head_object", head)
    assert (await post(context, changes={"sample_s3_uri": uri})).status_code == 403
    assert not head.called and not delivered(context)


@pytest.mark.parametrize(
    "claim,value",
    [
        ("actor_id", "99"),
        ("repository_id", "999"),
        ("workflow_ref", "org/repo/.github/workflows/attacker.yml@refs/heads/main"),
        ("job_workflow_ref", "org/repo/.github/workflows/reusable.yml@refs/heads/main"),
        ("event_name", "pull_request"),
        ("aud", "sts.amazonaws.com"),
        ("exp", 1),
    ],
)
async def test_forged_or_unregistered_workflow_cannot_enqueue(context, claim, value):
    assert (await post(context, claims={claim: value})).status_code == 403
    assert not delivered(context)


async def test_body_bound_proof_and_identity_fields_cannot_be_substituted(context):
    assert (await post(context, proof_digest="0" * 64)).status_code == 403
    assert (await post(context, changes={"org_id": "victim"})).status_code == 422
    assert not delivered(context)


async def test_script_registration_uses_actual_bytes(context):
    script = b'import json; print(json.dumps({"ok": True}))'
    response = await post(context, changes={"stage": "static", "script_base64": base64.b64encode(script).decode()})
    assert response.status_code == 200
    manifest = json.loads(delivered(context)[0]["Body"])
    assert manifest["script_sha256"] == hashlib.sha256(script).hexdigest()
    assert base64.b64decode(manifest["script_base64"]) == script
    assert "script_s3_uri" not in manifest


@pytest.mark.parametrize("script", ["bad-base64", base64.b64encode(b"def broken(:").decode(), base64.b64encode(b"x" * 32769).decode()])
async def test_unregistered_script_not_queued(context, script):
    assert (await post(context, changes={"stage": "static", "script_base64": script})).status_code == 422
    assert not delivered(context)


async def test_new_attempt_cannot_read_previous_attempt_results(context, monkeypatch):
    response = await post(context)
    query = Mock(return_value={"Items": []})
    monkeypatch.setattr(context["clients"]["dynamodb"], "query", query)
    job = {"job_id": response.json()["job_id"]}
    assert (await post(context, "result", job, claims={"run_attempt": "2"})).status_code == 404
    assert not query.called
    assert (await post(context, "result", job)).json() == {"status": "pending"}
    assert query.called


@pytest.mark.parametrize(
    "head",
    [
        {"ContentLength": 1},
        {"ContentLength": 1, "VersionId": "null"},
        {"ContentLength": 0, "VersionId": "v"},
        {"ContentLength": cyber_jobs.MAX_SAMPLE + 1, "VersionId": "v"},
    ],
)
async def test_unversioned_or_unbounded_sample_never_enqueues(context, monkeypatch, head):
    monkeypatch.setattr(context["clients"]["s3"], "head_object", Mock(return_value=head))
    assert (await post(context)).status_code == 409
    assert not delivered(context)


async def test_revocation_during_storage_preparation_prevents_enqueue(context, db_session, monkeypatch):
    from sqlalchemy import delete

    original = cyber_jobs.run_in_threadpool

    async def prepare_then_revoke(fn, *args, **kwargs):
        result = await original(fn, *args, **kwargs)
        if fn is cyber_jobs.register:
            await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == "human"))
            await db_session.commit()
        return result

    monkeypatch.setattr(cyber_jobs, "run_in_threadpool", prepare_then_revoke)
    assert (await post(context)).status_code == 403
    assert not delivered(context)


async def test_registered_reusable_workflow_requires_actual_verified_human(context, monkeypatch):
    import os

    entries = json.loads(os.environ["ADP_ARC_MODEL_BINDINGS"])
    reusable = "org/repo/.github/workflows/malware-analysis-agent.yml@refs/heads/main"
    entries[0]["job_workflow_ref"] = reusable
    monkeypatch.setenv("ADP_ARC_MODEL_BINDINGS", json.dumps(entries))
    assert (await post(context, claims={"job_workflow_ref": reusable})).status_code == 200
    assert delivered(context)
    assert (await post(context, claims={"job_workflow_ref": reusable, "actor_id": "99"})).status_code == 403
    assert not delivered(context)
