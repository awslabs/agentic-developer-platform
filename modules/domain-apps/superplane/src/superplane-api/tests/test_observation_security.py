"""Regression acceptance for #5396; database fixtures include dedicated cluster bindings."""

import asyncio
import json
import uuid

import pytest
from sqlalchemy import select

from app.config import settings
from app.main import app
from app.models.cluster import Cluster
from app.models.organization import Organization
from app.models.workspace import Workspace
from app.models.observation import ObservationLease
from tests.conftest import async_session_test
from tests.test_heartbeat_auth import _observation, _submit

CREDENTIAL = "security-test-credential"
KEY = b"security-test-signing-key"


def configure(monkeypatch, workspaces, *, lease_scopes=()):
    monkeypatch.setattr(
        settings,
        "observation_submitters",
        json.dumps(
            [
                {
                    "submitter_id": "security-test-monitor",
                    "credential": CREDENTIAL,
                    "signing_key": KEY.decode(),
                    "workspaces": [str(w) for w in workspaces],
                    "lease_scopes": list(lease_scopes),
                }
            ]
        ),
    )


async def seed(name="prod"):
    org, workspace, cluster = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with async_session_test() as db:
        db.add(Organization(id=org, name=str(org)))
        await db.flush()
        db.add(Cluster(id=cluster, org_id=org, name="cluster", status="Active"))
        await db.flush()
        db.add(
            Workspace(
                id=workspace,
                org_id=org,
                name=name,
                status="Active",
                isolation_mode="namespace",
                cluster_id=cluster,
            )
        )
        await db.commit()
    return org, workspace, cluster


async def test_same_workspace_name_in_two_orgs_cannot_expand_grant(client, monkeypatch):
    org_a, ws_a, cluster_a = await seed()
    org_b, ws_b, cluster_b = await seed()
    configure(monkeypatch, [ws_a])
    own = await _submit(
        client,
        _observation(cluster_a, workspace=str(ws_a)),
        credential=CREDENTIAL,
        key=KEY,
    )
    assert own.status_code == 202
    foreign = await _submit(
        client,
        _observation(cluster_b, workspace=str(ws_b)),
        credential=CREDENTIAL,
        key=KEY,
    )
    assert foreign.status_code in (403, 404)
    rows = (
        await client.get(
            "/internal/observations/clusters", headers={"authorization": CREDENTIAL}
        )
    ).json()
    assert [r["cluster_id"] for r in rows] == [str(cluster_a)]
    async with async_session_test() as db:
        orgs = (
            (
                await db.execute(
                    select(Cluster.org_id).where(
                        Cluster.id.in_([uuid.UUID(r["cluster_id"]) for r in rows])
                    )
                )
            )
            .scalars()
            .all()
        )
    assert set(orgs) == {org_a} and org_b not in orgs


async def test_display_name_grant_does_not_authorize_a_cluster(client, monkeypatch):
    _, _, cluster = await seed()
    configure(monkeypatch, ["prod"])
    result = await _submit(
        client, _observation(cluster, workspace="prod"), credential=CREDENTIAL, key=KEY
    )
    assert result.status_code in (403, 404)
    result = await client.get(
        "/internal/observations/clusters", headers={"authorization": CREDENTIAL}
    )
    assert result.json() == []


@pytest.mark.parametrize("release", [False, True])
@pytest.mark.parametrize("scope", ["foreign", "empty", "unknown", "arbitrary"])
async def test_lease_authority_is_required_before_storage(
    client, monkeypatch, release, scope
):
    _, own, _ = await seed("own")
    _, _, foreign = await seed("foreign")
    configure(monkeypatch, [] if scope == "empty" else [own])
    body = {
        "resource_type": "cluster_health",
        "resource_id": str(foreign),
        "fence_token": 1,
    }
    if scope == "unknown":
        body["resource_id"] = str(uuid.uuid4())
    if scope == "arbitrary":
        body.update(resource_type="anything", resource_id="global")
    response = await client.post(
        "/internal/observations/leases" + ("/release" if release else ""),
        json=body,
        headers={"authorization": CREDENTIAL},
    )
    assert response.status_code in (403, 404)
    async with async_session_test() as db:
        assert not (await db.execute(select(ObservationLease))).scalars().all()


async def test_budget_global_lease_requires_explicit_configured_scope(
    client, monkeypatch
):
    _, ws, _ = await seed()
    configure(monkeypatch, [ws])
    body = {"resource_type": "budget_monitor", "resource_id": "global"}
    refused = await client.post(
        "/internal/observations/leases",
        json=body,
        headers={"authorization": CREDENTIAL},
    )
    assert refused.status_code in (403, 404)
    configure(monkeypatch, [ws], lease_scopes=["budget_monitor/global"])
    accepted = await client.post(
        "/internal/observations/leases",
        json=body,
        headers={"authorization": CREDENTIAL},
    )
    assert accepted.status_code == 200


async def raw_request(method, path, body, authorization):
    sent = False
    finished = asyncio.Event()
    messages = []

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await finished.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            finished.set()

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
            "headers": [
                (b"authorization", authorization),
                (b"content-type", b"application/json"),
            ],
        },
        receive,
        send,
    )
    return next(m["status"] for m in messages if m["type"] == "http.response.start")


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/internal/observations"),
        ("POST", "/internal/observations/leases"),
        ("POST", "/internal/observations/leases/release"),
        ("GET", "/internal/observations/clusters"),
        ("GET", "/internal/observations/00000000-0000-0000-0000-000000000001"),
        (
            "GET",
            "/internal/observations/00000000-0000-0000-0000-000000000001/cost-history",
        ),
        ("POST", "/internal/observations/00000000-0000-0000-0000-000000000001/events"),
    ],
)
async def test_non_ascii_authorization_is_refused_by_raw_asgi(
    monkeypatch, configured, method, path
):
    configure(monkeypatch, [])
    if not configured:
        monkeypatch.setattr(settings, "observation_submitters", "[]")
    assert 400 <= await raw_request(method, path, b"{}", b"bad-\xe9") < 500


async def test_nested_json_and_oversized_stream_are_refused(client):
    nested = b"[" * 20000 + b"0" + b"]" * 20000
    response = await client.post("/internal/observations", content=nested)
    assert 400 <= response.status_code < 500

    async def large_stream():
        for _ in range(17):
            yield b" " * 65536

    response = await client.post("/internal/observations", content=large_stream())
    assert response.status_code == 413


async def test_distinct_observation_callers_have_separate_quotas(client, monkeypatch):
    _, ws, _ = await seed()
    entries = [
        {
            "submitter_id": f"monitor-{i}",
            "credential": f"test-quota-{i}",
            "signing_key": KEY.decode(),
            "workspaces": [str(ws)],
        }
        for i in range(2)
    ]
    monkeypatch.setattr(settings, "observation_submitters", json.dumps(entries))
    for _ in range(60):
        response = await client.get(
            "/internal/observations/clusters", headers={"authorization": "test-quota-0"}
        )
        assert response.status_code == 200
    assert (
        await client.get(
            "/internal/observations/clusters", headers={"authorization": "test-quota-0"}
        )
    ).status_code == 429
    assert (
        await client.get(
            "/internal/observations/clusters", headers={"authorization": "test-quota-1"}
        )
    ).status_code == 200


@pytest.mark.parametrize("spelling", ["hex", "upper", "braced", "urn"])
async def test_equivalent_cluster_ids_share_one_lease(client, monkeypatch, spelling):
    _, workspace, cluster = await seed()
    configure(monkeypatch, [workspace])
    alternate = {
        "hex": cluster.hex,
        "upper": str(cluster).upper(),
        "braced": "{" + str(cluster) + "}",
        "urn": cluster.urn,
    }[spelling]
    headers = {"authorization": CREDENTIAL}
    body = {
        "resource_type": "cluster_health",
        "resource_id": str(cluster),
        "instance_id": "monitor-a",
    }
    acquired = await client.post(
        "/internal/observations/leases", json=body, headers=headers
    )
    assert acquired.status_code == 200, acquired.text
    contended = await client.post(
        "/internal/observations/leases",
        json={**body, "resource_id": alternate, "instance_id": "monitor-b"},
        headers=headers,
    )
    assert contended.status_code == 409, contended.text
    async with async_session_test() as db:
        rows = (await db.execute(select(ObservationLease))).scalars().all()
        assert len(rows) == 1
        assert rows[0].scope == acquired.json()["scope"]
    released = await client.post(
        "/internal/observations/leases/release",
        json={
            **body,
            "resource_id": alternate,
            "fence_token": acquired.json()["fence_token"],
        },
        headers=headers,
    )
    assert released.status_code == 200, released.text
    next_owner = await client.post(
        "/internal/observations/leases",
        json={**body, "resource_id": alternate, "instance_id": "monitor-b"},
        headers=headers,
    )
    assert next_owner.status_code == 200, next_owner.text
    assert next_owner.json()["scope"] == acquired.json()["scope"]
