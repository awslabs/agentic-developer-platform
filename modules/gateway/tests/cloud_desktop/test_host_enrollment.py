"""Reject copied or out-of-scope EC2 enrollment before creating a run."""

import base64
import hashlib
import inspect
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws
from sqlalchemy import func, select

from src.agentauth.chat_crew_run import CrewRunStore
from src.shared.models.crew_checkpoint import CrewCheckpoint
from src.shared.models.crew_executor import CrewExecutor
from src.shared.models.dev_box import DevBox, DevBoxAccessSession, DevBoxDevice, DevBoxInstance, DevBoxResource
from src.shared.models.organization import Organization
from tests.cloud_desktop.test_routes import client, context

pytest_plugins = ("tests.cloud_desktop.test_routes",)

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box"))
from controller.identity import (  # noqa: E402
    crew_key_command,
    crew_profile_command,
    deliver_sealed_enrollment,
    rebind_host_run,
    reconcile_enrollment,
    reconcile_expired_enrollment,
    reconcile_expired_host_key,
    reconcile_host_fingerprint,
    reconcile_host_key,
    reconcile_host_readiness,
)
from image.write_enrollment import generate_key  # noqa: E402


@pytest.fixture
async def pending_host(approved, run_store):
    factory, approval, _ = approved
    async with factory.kw["bind"].begin() as connection:
        await connection.run_sync(CrewCheckpoint.__table__.create, checkfirst=True)
    now = int(time.time())
    run_id = run_store.mint(
        tenant_id="one",
        user_id="alice",
        session_id="host-box",
        lease_generation=1,
        lease_expires_at=now + 300,
        target_kind="ec2-instance",
        target_ref="i-0123456789abcdef0",
        deployment_id="test-deployment",
        provisioning=True,
        now=now,
    )
    async with factory.begin() as db:
        box = await db.get(DevBox, "host-box")
        box.auth_readiness = "pending"
        instance = await db.scalar(select(DevBoxInstance))
        instance.image_ref = "ami-approved"
        instance.private_address = "10.4.0.3"
        instance.certificate_fingerprint = "a" * 64
        device = await db.get(DevBoxDevice, approval["device_id"])
        device.enrollment_consumed_at = datetime.now(UTC)
        db.add(
            DevBoxAccessSession(
                box_id=box.id,
                user_id="alice",
                org_id="one",
                deployment_id="test-deployment",
                machine_generation=1,
                device_id=device.id,
                provider_session_ref=run_id,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        executor = await db.scalar(select(CrewExecutor).where(CrewExecutor.user_id == "alice"))
        executor.owner_key = "a" * 52
        executor.target_kind = "ec2-instance"
        executor.state = "provisioning"
        executor.dev_box_id = box.id
        executor.latest_verified_checkpoint_id = "verified-one"
        await db.flush()
        db.add(
            CrewCheckpoint(
                id="verified-one",
                executor_id=executor.id,
                tenant_id="one",
                user_id="alice",
                owner_key="a" * 52,
                target_kind="eks-pod",
                generation=1,
                sequence=7,
                idempotency_key="prior",
                status="verified",
                manifest_sha256="b" * 64,
                manifest_version_id="v7",
                crew_version="0.11.2",
                archive_bucket="fixture-bucket",
                archive_key=f"crew-checkpoints/{'a' * 52}/verified-one/archive",
                archive_version_id="v7",
                archive_sha256="c" * 64,
                archive_size=100,
                manifest_bucket="fixture-bucket",
                manifest_key=f"crew-checkpoints/{'a' * 52}/verified-one/manifest",
                manifest_size=50,
            )
        )
    return factory, run_store, run_id


@pytest.mark.asyncio
async def test_current_instance_discovers_only_its_provisioning_run_without_ssm(pending_host, monkeypatch):
    from fastapi import HTTPException
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from src.crew import checkpoint_routes as routes
    from src.crew.checkpoint_store import CrewCheckpointStore

    factory, run_store, run_id = pending_host
    monkeypatch.setattr(routes, "current_crew_store", lambda: run_store)
    monkeypatch.setattr(routes, "gateway_client_identity", lambda: SimpleNamespace(certificate="public-cert", fingerprint="f" * 64))

    async def public_keys():
        return JSONResponse({"keys": {"test": "public-key"}})

    monkeypatch.setattr(routes, "model_policy_keys", public_keys)
    caller = {"arn": "arn:aws:sts::000000000000:assumed-role/guest/i-0123456789abcdef0"}
    monkeypatch.setattr(routes, "verified_caller_identity", lambda _request: caller["arn"])
    request = Request({"type": "http", "method": "POST", "path": "/crew/host-restore/context", "headers": []})
    identity = worker_context()
    identity.requires_host_identity = True
    store = CrewCheckpointStore(factory)
    response = await routes.host_restore_context(request, identity, store)
    result = json.loads(response.body)
    assert response.headers["cache-control"] == "no-store"
    assert result.pop("lease_expires_at") > int(time.time())
    assert result == {
        "run_id": run_id,
        "tenant_id": "one",
        "user_id": "alice",
        "box_id": "host-box",
        "instance_ref": "i-0123456789abcdef0",
        "private_ip": "10.4.0.3",
        "lease_generation": 1,
        "checkpoint_id": "verified-one",
        "manifest_sha256": "b" * 64,
        "restore_manifest_version": 7,
        "deployment_id": "test-deployment",
        "client_certificate": "public-cert",
        "gateway_fingerprint": "f" * 64,
        "verification_keys": {"test": "public-key"},
    }
    assert "restore_token" not in result and "private_key" not in result
    caller["arn"] = "arn:aws:sts::000000000000:assumed-role/other/i-0123456789abcdef0"
    with pytest.raises(HTTPException) as wrong_role:
        await routes.host_restore_context(request, identity, store)
    assert wrong_role.value.status_code == 403
    caller["arn"] = "arn:aws:sts::000000000000:assumed-role/guest/i-aaaaaaaaaaaaaaaaa"
    with pytest.raises(HTTPException) as wrong_instance:
        await routes.host_restore_context(request, identity, store)
    assert wrong_instance.value.status_code == 403


@pytest.mark.asyncio
async def test_mounted_host_context_requires_verified_iam_caller(pending_host, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from starlette.responses import JSONResponse

    from src.crew import checkpoint_routes as routes
    from src.crew.checkpoint_store import CrewCheckpointStore

    factory, run_store, run_id = pending_host
    monkeypatch.setattr(routes, "current_crew_store", lambda: run_store)
    monkeypatch.setattr(routes, "gateway_client_identity", lambda: SimpleNamespace(certificate="public-cert", fingerprint="f" * 64))

    async def public_keys():
        return JSONResponse({"keys": {"test": "public-key"}})

    monkeypatch.setattr(routes, "model_policy_keys", public_keys)
    caller = {"arn": "arn:aws:sts::000000000000:assumed-role/guest/i-0123456789abcdef0"}
    monkeypatch.setattr(routes, "verified_caller_identity", lambda _request: caller["arn"])
    identity = worker_context()
    identity.requires_host_identity = True
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.enabled] = lambda: None
    app.dependency_overrides[routes.get_store] = lambda: CrewCheckpointStore(factory)
    app.dependency_overrides[routes.get_token_context] = lambda: identity
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admitted = await client.post("/crew/host-restore/context")
        assert admitted.status_code == 200
        assert admitted.json()["run_id"] == run_id
        caller["arn"] = None
        denied = await client.post("/crew/host-restore/context")
        assert denied.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("retire", ["generation", "access", "executor"])
async def test_retired_host_cannot_discover_run(pending_host, monkeypatch, retire):
    from fastapi import HTTPException
    from starlette.requests import Request

    from src.crew import checkpoint_routes as routes
    from src.crew.checkpoint_store import CrewCheckpointStore

    factory, run_store, _ = pending_host
    monkeypatch.setattr(routes, "current_crew_store", lambda: run_store)
    monkeypatch.setattr(routes, "verified_caller_identity", lambda _request: "arn:aws:sts::000000000000:assumed-role/guest/i-0123456789abcdef0")
    async with factory.begin() as db:
        if retire == "generation":
            box = await db.get(DevBox, "host-box")
            box.machine_generation = 2
        elif retire == "access":
            access = await db.scalar(select(DevBoxAccessSession))
            access.terminated_at = datetime.now(UTC)
        else:
            executor = await db.scalar(select(CrewExecutor).where(CrewExecutor.user_id == "alice"))
            executor.generation = 2
    request = Request({"type": "http", "method": "POST", "path": "/crew/host-restore/context", "headers": []})
    identity = worker_context()
    identity.requires_host_identity = True
    with pytest.raises(HTTPException) as denied:
        await routes.host_restore_context(request, identity, CrewCheckpointStore(factory))
    assert denied.value.status_code in (403, 409)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proof,stale",
    [
        ("crew-profile-ok\ncrew-ready-ok\n", None),
        ("crew-profile-ok\ncrew-bootstrap-ok\ncrew-ready-ok\n", None),
        ("", None),
        ("crew-profile-ok\ncrew-ready-ok\n", "box"),
        ("crew-profile-ok\ncrew-ready-ok\n", "executor"),
    ],
)
async def test_controller_promotes_only_verified_current_host_readback(pending_host, proof, stale):
    from controller.launch import LaunchTemplate

    factory, run_store, run_id = pending_host

    class Owned(Instance):
        def describe_instances(self, **kwargs):
            data = super().describe_instances(**kwargs)
            data["Reservations"][0]["Instances"][0]["PrivateIpAddress"] = "10.4.0.3"
            return data

    class Proof:
        def send_command(self, **kwargs):
            assert kwargs["CloudWatchOutputConfig"] == {"CloudWatchOutputEnabled": False}
            shell = kwargs["Parameters"]["commands"][0]
            assert subprocess.run(["bash", "-n", "-c", shell], capture_output=True).returncode == 0
            assert run_id not in shell and "--gateway-arn" in shell and "--model-id" in shell
            assert "crew-host-bootstrap.js" in shell and "test -e /run/adp/crew-ready ||" in shell
            assert "mkfs" not in shell and "/mnt/crew" not in shell, "no recorded workspace disk: no mount step"
            command = shlex.split(shell)
            scope = json.loads(command[-1])
            assert scope["run_id_sha256"] == hashlib.sha256(run_id.encode()).hexdigest()
            assert scope["restore_manifest_version"] == 7
            assert scope["manifest_sha256"] == "b" * 64 and scope["lease_generation"] == 1
            return {"Command": {"CommandId": "ready-readback"}}

        def get_command_invocation(self, **kwargs):
            assert kwargs == {"CommandId": "ready-readback", "InstanceId": "i-0123456789abcdef0"}
            if stale:
                import asyncio

                async def retire():
                    async with factory.begin() as db:
                        if stale == "box":
                            box = await db.get(DevBox, "host-box")
                            box.machine_generation = 2
                        else:
                            executor = await db.scalar(select(CrewExecutor).where(CrewExecutor.user_id == "alice"))
                            executor.generation = 2

                asyncio.run(retire())
            return {"Status": "Success", "StandardOutputContent": proof}

    template = LaunchTemplate(
        "lt-example",
        "4",
        "us-east-1",
        image_id="ami-approved",
        revision="r1",
        profile_revision="crew-r1",
        source_revision="a" * 40,
        gateway_execute_arn="arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev",
        crew_model_id="us.anthropic.example-v1:0",
    )
    is_ready = proof in ("crew-profile-ok\ncrew-ready-ok\n", "crew-profile-ok\ncrew-bootstrap-ok\ncrew-ready-ok\n") and not stale
    assert (
        await reconcile_host_readiness(
            factory,
            Owned(image_id="ami-approved"),
            Proof(),
            run_store,
            (await _host_instance_id(factory)),
            launch_template=template,
        )
        is is_ready
    )
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        assert (box.auth_readiness == "ready") is is_ready


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["owner_key", "user"])
async def test_replaced_generation_restores_the_inherited_checkpoint_despite_a_new_owner_key(pending_host, mismatch):
    """#8223 cause 47: a Replace creates a new executor with a fresh random owner_key and inherits the
    predecessor's verified checkpoint (inventory.bind_host). Readiness compared the checkpoint's
    owner_key with the executor's and silently refused, so a box whose owner had any checkpoint
    never became ready after a Replace (dev 2026-10-21, gen 28). The lineage is the executor's own
    latest_verified_checkpoint_id plus tenant and user; another user's checkpoint is still refused."""
    from controller.launch import LaunchTemplate

    factory, run_store, _run_id = pending_host
    async with factory.begin() as db:
        executor = await db.scalar(select(CrewExecutor).where(CrewExecutor.user_id == "alice"))
        executor.owner_key = "f" * 52  # the replaced generation's fresh key; the checkpoint keeps "a" * 52
        if mismatch == "user":
            checkpoint = await db.get(CrewCheckpoint, "verified-one")
            checkpoint.user_id = "mallory"

    class Owned(Instance):
        def describe_instances(self, **kwargs):
            data = super().describe_instances(**kwargs)
            data["Reservations"][0]["Instances"][0]["PrivateIpAddress"] = "10.4.0.3"
            return data

    class Proof:
        def send_command(self, **kwargs):
            scope = json.loads(shlex.split(kwargs["Parameters"]["commands"][0])[-1])
            assert scope["checkpoint_id"] == "verified-one" and scope["restore_manifest_version"] == 7
            return {"Command": {"CommandId": "ready-readback"}}

        def get_command_invocation(self, **kwargs):
            return {"Status": "Success", "StandardOutputContent": "crew-profile-ok\ncrew-ready-ok\n"}

    template = LaunchTemplate(
        "lt-example",
        "4",
        "us-east-1",
        image_id="ami-approved",
        revision="r1",
        profile_revision="crew-r1",
        source_revision="a" * 40,
        gateway_execute_arn="arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev",
        crew_model_id="us.anthropic.example-v1:0",
    )
    expected = mismatch == "owner_key"
    assert (
        await reconcile_host_readiness(
            factory, Owned(image_id="ami-approved"), Proof(), run_store, (await _host_instance_id(factory)), launch_template=template
        )
        is expected
    )
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        assert (box.auth_readiness == "ready") is expected
        assert ((await db.scalar(select(DevBoxInstance))).readiness_class == "crew_ready") is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("verified", [False, True])
async def test_authenticated_host_restore_readback_precedes_owner_lease(pending_host, monkeypatch, verified):
    from controller.launch import LaunchTemplate

    from src.crew.inventory import CrewExecutorInventory
    from src.crew.targets import CrewLeaseFencedError, CrewOwner, OwnerTargetResolver

    factory, run_store, run_id = pending_host
    table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="bootstrap-lease-fixture",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
    )
    monkeypatch.setattr("src.crew.host_lease.current_crew_store", lambda: run_store)
    monkeypatch.setattr("src.orchestration.intake_wiring._get_context_table", lambda: table)
    owner = CrewOwner("one", "alice")
    resolver = OwnerTargetResolver(CrewExecutorInventory(factory), table, deployment_id="test-deployment")
    with pytest.raises(CrewLeaseFencedError):
        await resolver.resolve(owner, now=int(time.time()))

    class Owned(Instance):
        def describe_instances(self, **arguments):
            data = super().describe_instances(**arguments)
            data["Reservations"][0]["Instances"][0]["PrivateIpAddress"] = "10.4.0.3"
            return data

    class Readback:
        def send_command(self, **arguments):
            command = arguments["Parameters"]["commands"][0]
            assert "crew-host-bootstrap.js" in command and "--gateway-arn" in command
            assert run_id not in command
            assert json.loads(shlex.split(command)[-1])["run_id_sha256"] == hashlib.sha256(run_id.encode()).hexdigest()
            assert subprocess.run(["bash", "-n", "-c", command], capture_output=True).returncode == 0
            return {"Command": {"CommandId": "restore-readback"}}

        def get_command_invocation(self, **_arguments):
            return {
                "Status": "Success" if verified else "Failed",
                "StandardOutputContent": "crew-profile-ok\ncrew-bootstrap-ok\ncrew-ready-ok\n" if verified else "",
            }

    template = LaunchTemplate(
        "lt-example",
        "4",
        "us-east-1",
        image_id="ami-approved",
        revision="r1",
        profile_revision="crew-r1",
        source_revision="a" * 40,
        gateway_execute_arn="arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev",
        crew_model_id="us.anthropic.example-v1:0",
    )
    result = await reconcile_host_readiness(
        factory, Owned(image_id="ami-approved"), Readback(), run_store, await _host_instance_id(factory), launch_template=template
    )
    assert result is verified
    if verified:
        target = await resolver.resolve(owner, now=int(time.time()))
        assert target.host_run_id == run_id and target.lease.generation == 1
        assert run_store.read_run(run_id)["status"] == "active"
    else:
        with pytest.raises(CrewLeaseFencedError):
            await resolver.resolve(owner, now=int(time.time()))
        assert run_store.read_run(run_id)["status"] == "provisioning"
    async with factory() as db:
        assert await db.scalar(select(func.count(DevBox.id))) == 1
        assert await db.scalar(select(func.count(CrewExecutor.id))) == 1


async def _host_instance_id(factory):
    async with factory() as db:
        return await db.scalar(select(DevBoxInstance.id))


@pytest.mark.asyncio
async def test_provisioning_host_receives_only_its_pinned_restore_grant(pending_host, monkeypatch):
    from fastapi import FastAPI, HTTPException
    from httpx import ASGITransport, AsyncClient
    from starlette.requests import Request

    from src.crew import checkpoint_routes as routes
    from src.crew.checkpoint_objects import VersionedCheckpointObjects
    from src.crew.checkpoint_store import CrewCheckpointStore
    from tests.crew.test_checkpoint_objects import S3Fixture

    factory, run_store, run_id = pending_host
    monkeypatch.setattr(routes, "current_crew_store", lambda: run_store)
    caller = {"arn": "arn:aws:sts::000000000000:assumed-role/guest/i-0123456789abcdef0"}
    monkeypatch.setattr(routes, "verified_caller_identity", lambda _request: caller["arn"])
    request = Request({"type": "http", "method": "POST", "path": "/crew/host-restore/grant", "headers": [(b"x-agent-runid", run_id.encode())]})
    context = worker_context()
    context.requires_host_identity = True
    store = CrewCheckpointStore(factory)
    storage = VersionedCheckpointObjects(S3Fixture(), "fixture-bucket")
    body = routes.HostRestoreGrant(checkpoint_id="verified-one")

    caller["arn"] = "arn:aws:sts::000000000000:assumed-role/guest/i-aaaaaaaaaaaaaaaaa"
    with pytest.raises(HTTPException) as foreign:
        await routes.host_restore_grant(request, body, context, store, storage)
    assert foreign.value.status_code == 403
    caller["arn"] = "arn:aws:sts::000000000000:assumed-role/other/i-0123456789abcdef0"
    with pytest.raises(HTTPException) as wrong_role:
        await routes.host_restore_grant(request, body, context, store, storage)
    assert wrong_role.value.status_code == 403
    async with factory() as db:
        assert (await db.get(CrewCheckpoint, "verified-one")).restore_pins == 0
    caller["arn"] = "arn:aws:sts::000000000000:assumed-role/guest/i-0123456789abcdef0"
    granted = await routes.host_restore_grant(request, body, context, store, storage)
    grant = json.loads(granted.body)
    assert granted.headers["cache-control"] == "no-store"
    assert grant["manifest_version"] == 7
    assert grant["objects"]["manifest"]["version_id"] == "v7"
    async with factory() as db:
        assert (await db.get(CrewCheckpoint, "verified-one")).restore_pins == 1
    with pytest.raises(HTTPException) as replay:
        await routes.host_restore_grant(request, body, context, store, storage)
    assert replay.value.status_code == 409
    with pytest.raises(HTTPException) as wrong_token:
        await routes.host_restore_release(request, routes.HostRestoreRelease(checkpoint_id="verified-one", restore_token="x" * 40), context, store)
    assert wrong_token.value.status_code == 404
    released = await routes.host_restore_release(
        request, routes.HostRestoreRelease(checkpoint_id="verified-one", restore_token=grant["restore_token"]), context, store
    )
    assert json.loads(released.body)["status"] == "released"
    async with factory() as db:
        assert (await db.get(CrewCheckpoint, "verified-one")).restore_pins == 0
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.enabled] = lambda: None
    app.dependency_overrides[routes.get_token_context] = lambda: context
    app.dependency_overrides[routes.get_store] = lambda: store
    app.dependency_overrides[routes.get_objects] = lambda: storage
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://gateway.fixture") as client:
        mounted = await client.post("/crew/host-restore/grant", headers={"X-Agent-RunId": run_id}, json={"checkpoint_id": "verified-one"})
        assert mounted.status_code == 200
        mounted_release = await client.post(
            "/crew/host-restore/release",
            headers={"X-Agent-RunId": run_id},
            json={
                "checkpoint_id": "verified-one",
                "restore_token": mounted.json()["restore_token"],
            },
        )
        assert mounted_release.status_code == 200
    async with factory.begin() as db:
        box = await db.get(DevBox, "host-box")
        box.machine_generation = 2
    with pytest.raises(HTTPException) as retired:
        await routes.host_restore_grant(request, body, context, store, storage)
    assert retired.value.status_code == 409
    async with factory() as db:
        assert (await db.get(CrewCheckpoint, "verified-one")).restore_pins == 0


class Instance:
    def __init__(self, *, instance_id="i-0123456789abcdef0", owner="alice", org="one", box_id="host-box", generation=1, image_id=None):
        self.instance_id = instance_id
        self.owner = owner
        self.org = org
        self.box_id = box_id
        self.generation = generation
        self.image_id = image_id

    def describe_instances(self, **arguments):
        assert arguments["InstanceIds"] == ["i-0123456789abcdef0"]
        tags = {
            "adp:component": "dev-box",
            "adp:deployment-id": "test-deployment",
            "adp:owner-id": self.owner,
            "adp:box-id": self.box_id,
            "adp:generation": str(self.generation),
        }
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": self.instance_id,
                            "ImageId": self.image_id,
                            "State": {"Name": "running"},
                            "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
                        }
                    ]
                }
            ]
        }


class InstanceProof:
    def __init__(self, *, challenge_digest, public_key_digest="a" * 64, profile_proof=None, status="Success"):
        self.challenge_digest = challenge_digest
        self.public_key_digest = public_key_digest
        self.profile_proof = profile_proof
        self.status = status
        self.requests = 0
        self.deliveries = 0

    def send_command(self, **arguments):
        assert arguments["InstanceIds"] == ["i-0123456789abcdef0"]
        commands = arguments["Parameters"]["commands"]
        if "write_enrollment.py --sealed" in commands[0]:
            self.deliveries += 1
            assert len(commands) == 1
            assert commands[0].startswith("set -e; test ")
            assert "&& sha256sum /run/adp/dev-box-enrollment /run/adp/dev-box-public-key" in commands[0]
            return {"Command": {"CommandId": "sealed-command"}}
        self.requests += 1
        assert commands[0] == "sha256sum /run/adp/dev-box-enrollment /run/adp/dev-box-public-key"
        if self.profile_proof is not None:
            assert len(commands) == 3
            assert "profile.json" in commands[1] and "adp-crew-host.service" in commands[1]
            assert "crew-r1" in commands[1] and "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in commands[1]
            assert "dev-box-private-key" in commands[2] and "dev-box-public-key" in commands[2]
            assert self.challenge_digest not in str(commands)
        else:
            assert len(commands) == 1
        return {"Command": {"CommandId": "command"}}

    def get_command_invocation(self, **arguments):
        assert arguments["InstanceId"] == "i-0123456789abcdef0"
        assert arguments["CommandId"] in {"command", "sealed-command"}
        return {
            "Status": "Success" if arguments["CommandId"] == "sealed-command" else self.status,
            "StandardOutputContent": (
                f"{self.challenge_digest}  /run/adp/dev-box-enrollment\n{self.public_key_digest}  /run/adp/dev-box-public-key\n"
                + (self.profile_proof or "" if arguments["CommandId"] == "command" else "")
            ),
        }


class DelayedInstanceProof(InstanceProof):
    def __init__(self, *, statuses, **kwargs):
        super().__init__(**kwargs)
        self.statuses = iter(statuses)
        self.reads = 0

    def get_command_invocation(self, **arguments):
        from botocore.exceptions import ClientError

        self.reads += 1
        status = next(self.statuses, "Success")
        if status == "InvocationDoesNotExist":
            raise ClientError({"Error": {"Code": status}}, "GetCommandInvocation")
        result = super().get_command_invocation(**arguments)
        result["Status"] = status
        return result


class SealedDeliveryProof:
    def __init__(self, challenge_digest, public_key_digest, ciphertext, *, status="Success"):
        self.challenge_digest = challenge_digest
        self.public_key_digest = public_key_digest
        self.ciphertext = ciphertext
        self.status = status
        self.requests = 0

    def send_command(self, **arguments):
        self.requests += 1
        assert arguments["InstanceIds"] == ["i-0123456789abcdef0"]
        assert arguments["CloudWatchOutputConfig"] == {"CloudWatchOutputEnabled": False}
        commands = arguments["Parameters"]["commands"]
        assert len(commands) == 1
        assert commands[0].startswith("set -e; test ")
        assert base64.b64encode(self.ciphertext).decode() in commands[0]
        assert self.public_key_digest in commands[0]
        assert "write_enrollment.py --sealed" in commands[0]
        assert self.challenge_digest not in str(commands)
        assert "&& sha256sum /run/adp/dev-box-enrollment /run/adp/dev-box-public-key" in commands[0]
        return {"Command": {"CommandId": "sealed-command"}}

    def get_command_invocation(self, **arguments):
        assert arguments == {"CommandId": "sealed-command", "InstanceId": "i-0123456789abcdef0"}
        return {
            "Status": self.status,
            "StandardOutputContent": (
                f"{self.challenge_digest}  /run/adp/dev-box-enrollment\n{self.public_key_digest}  /run/adp/dev-box-public-key\n"
            ),
        }


class HostKeyProof:
    def __init__(self, public_key, *, status="Success"):
        self.public_key = public_key
        self.status = status
        self.requests = 0

    def send_command(self, **arguments):
        self.requests += 1
        assert arguments["InstanceIds"] == ["i-0123456789abcdef0"]
        assert arguments["CloudWatchOutputConfig"] == {"CloudWatchOutputEnabled": False}
        commands = arguments["Parameters"]["commands"]
        assert len(commands) == 3
        assert "profile.json" in commands[0]
        assert commands[1] == "python3 /opt/adp/crew/write_enrollment.py --generate-key"
        assert "dev-box-private-key" in commands[2]
        assert self.public_key not in str(commands).encode()
        return {"Command": {"CommandId": "host-key-command"}}

    def get_command_invocation(self, **arguments):
        assert arguments == {"CommandId": "host-key-command", "InstanceId": "i-0123456789abcdef0"}
        return {"Status": self.status, "StandardOutputContent": f"crew-profile-ok\n{self.public_key.decode()}crew-key-ok\n"}


@pytest.fixture
async def approved(database):
    async with database() as db:
        db.add(
            DevBox(
                id="host-box",
                user_id="alice",
                org_id="one",
                deployment_id="test-deployment",
                template_id="t1",
                template_revision="r1",
                profile="crew",
                machine_generation=1,
                status="running",
                desired_state="running",
                observed_state="running",
            )
        )
        await db.flush()
        db.add(
            DevBoxInstance(
                box_id="host-box",
                user_id="alice",
                org_id="one",
                deployment_id="test-deployment",
                machine_generation=1,
                provider_instance_id="i-0123456789abcdef0",
                template_revision="r1",
                observed_state="running",
            )
        )
        await db.commit()
    url = "/cloud-desktops/host-box/host-enrollment"
    response = client(database, context("alice-sub")).post(url, json={"public_key_digest": "a" * 64})
    assert response.status_code == 201
    async with database() as db:
        device = await db.get(DevBoxDevice, response.json()["device_id"])
        device.sealed_enrollment = b"synthetic ciphertext".ljust(384, b"x")
        await db.commit()
    return database, response.json(), url


@pytest.mark.asyncio
async def test_approval_denies_changed_observed_key_before_issuing_challenge(approved):
    factory, prior, url = approved
    observed_key = b"newly observed host public key"
    observed_digest = hashlib.sha256(observed_key).hexdigest()
    async with factory() as db:
        instance = await db.scalar(select(DevBoxInstance))
        instance.enrollment_public_key = observed_key
        old_device = await db.get(DevBoxDevice, prior["device_id"])
        old_device.revoked_at = datetime.now(UTC)
        await db.commit()
    owner_client = client(factory, context("alice-sub"))
    assert owner_client.post(url, json={"public_key_digest": "a" * 64}).status_code == 409
    assert client(factory, context("other-sub", "two")).post(url, json={"public_key_digest": observed_digest}).status_code == 404
    accepted = owner_client.post(url, json={"public_key_digest": observed_digest})
    assert accepted.status_code == 201
    async with factory() as db:
        new_device = await db.get(DevBoxDevice, accepted.json()["device_id"])
        assert new_device.public_key_digest == observed_digest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [
        {"crew": {"allowed_targets": ["eks-pod"]}},
        {"crew": {"allowed_targets": ["unsupported"]}},
        {},
    ],
)
async def test_revoked_ec2_policy_denies_owner_and_controller_enrollment(approved, run_store, settings):
    factory, approval, url = approved
    async with factory() as db:
        organization = await db.get(Organization, "one")
        organization.settings = settings
        instance = await db.scalar(select(DevBoxInstance))
        instance.enrollment_public_key = b"observed key"
        await db.commit()
    http = client(factory, context("alice-sub"))
    assert http.get("/cloud-desktops/host-box/host-enrollment-key").status_code == 403
    assert http.post(url, json={"public_key_digest": "a" * 64}).status_code == 403
    sealed = f"{url}/{approval['device_id']}/sealed"
    assert http.post(sealed, json={"ciphertext_b64": base64.b64encode(b"x" * 384).decode()}).status_code == 403
    proof = InstanceProof(challenge_digest=hashlib.sha256(approval["challenge"].encode()).hexdigest())
    assert await reconcile_enrollment(factory, Instance(), proof, run_store, approval["device_id"]) is None
    assert proof.requests == 0
    async with factory() as db:
        device = await db.get(DevBoxDevice, approval["device_id"])
        assert device.enrollment_consumed_at is None


@pytest.mark.asyncio
async def test_owned_controller_key_readback_unlocks_private_owner_lookup(approved, tmp_path):
    from controller.launch import LaunchTemplate

    factory, _, _ = approved
    public_key = generate_key(root=tmp_path / "adp", trusted_uid=os.getuid())
    template = LaunchTemplate(
        "lt-example", "4", "us-east-1", image_id="ami-approved", revision="r1", profile_revision="crew-r1", source_revision="a" * 40
    )
    proof = HostKeyProof(public_key)
    url = "/cloud-desktops/host-box/host-enrollment-key"
    assert client(factory, context("alice-sub")).get(url).status_code == 409
    async with factory() as db:
        instance = await db.scalar(select(DevBoxInstance))
        instance.image_ref = "ami-approved"
        await db.commit()
        assert await reconcile_host_key(factory, Instance(image_id="ami-approved"), proof, instance.id, launch_template=template)
        assert proof.requests == 1
        assert not await reconcile_host_key(factory, Instance(image_id="ami-approved"), proof, instance.id, launch_template=template)
        assert proof.requests == 1
    response = client(factory, context("alice-sub")).get(url)
    assert response.status_code == 200 and response.json()["public_key_digest"] == hashlib.sha256(public_key).hexdigest()
    assert client(factory, context("other-sub", "two")).get(url).status_code == 404


@pytest.mark.asyncio
async def test_controller_tick_observes_pending_crew_host_key(approved, run_store, monkeypatch):
    from controller.launch import LaunchTemplate
    from controller.worker import scan_once

    factory, _, _ = approved
    template = LaunchTemplate(
        "lt-example", "4", "us-east-1", image_id="ami-approved", revision="r1", profile_revision="crew-r1", source_revision="a" * 40
    )
    observed = []

    async def record_key(_factory, _ec2, _ssm, instance_id, *, launch_template):
        observed.append((instance_id, launch_template.profile_revision))

    async def ignore_pending(*_args, **_kwargs):
        return None

    monkeypatch.setattr("controller.worker.reconcile_host_key", record_key)
    monkeypatch.setattr("controller.worker.reconcile_enrollment", ignore_pending)
    assert (
        await scan_once(factory, Instance(), InstanceProof(challenge_digest="unused"), "test-deployment", template, "worker", run_store=run_store)
        == 1
    )
    async with factory() as db:
        instance = await db.scalar(select(DevBoxInstance))
    assert observed == [(instance.id, "crew-r1")]


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["foreign_provider", "image_mismatch", "failed_command"])
async def test_host_key_readback_denies_unverified_or_failed_proof(approved, tmp_path, denial):
    from controller.launch import LaunchTemplate

    factory, _, _ = approved
    public_key = generate_key(root=tmp_path / "adp", trusted_uid=os.getuid())
    template = LaunchTemplate(
        "lt-example", "4", "us-east-1", image_id="ami-approved", revision="r1", profile_revision="crew-r1", source_revision="a" * 40
    )
    proof = HostKeyProof(public_key, status="Failed" if denial == "failed_command" else "Success")
    async with factory() as db:
        instance = await db.scalar(select(DevBoxInstance))
        instance.image_ref = "ami-other" if denial == "image_mismatch" else "ami-approved"
        await db.commit()
        assert not await reconcile_host_key(
            factory,
            Instance(owner="other" if denial == "foreign_provider" else "alice", image_id="ami-approved"),
            proof,
            instance.id,
            launch_template=template,
        )
        assert proof.requests == (1 if denial == "failed_command" else 0)
    assert client(factory, context("alice-sub")).get("/cloud-desktops/host-box/host-enrollment-key").status_code == 409


@pytest.mark.asyncio
async def test_current_owner_can_fetch_host_key_and_submit_only_sealed_input(approved):
    factory, approval, url = approved
    public_key = b"synthetic guest public key\n"
    digest = hashlib.sha256(public_key).hexdigest()
    async with factory() as db:
        instance = await db.scalar(select(DevBoxInstance))
        instance.enrollment_public_key = public_key
        device = await db.get(DevBoxDevice, approval["device_id"])
        device.public_key_digest = digest
        device.sealed_enrollment = None
        await db.commit()
    key_url = "/cloud-desktops/host-box/host-enrollment-key"
    owner_client = client(factory, context("alice-sub"))
    response = owner_client.get(key_url)
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"public_key_b64": base64.b64encode(public_key).decode(), "public_key_digest": digest, "generation": 1}
    assert client(factory, context("other-sub", "two")).get(key_url).status_code == 404
    sealed_url = f"{url}/{approval['device_id']}/sealed"
    payload = {"ciphertext_b64": base64.b64encode(b"x" * 384).decode()}
    assert client(factory, context("other-sub", "two")).post(sealed_url, json=payload).status_code == 404
    assert owner_client.post(sealed_url, json={"ciphertext_b64": "!" * 512}).status_code == 422
    assert owner_client.post(sealed_url, json={**payload, "challenge": approval["challenge"]}).status_code == 422
    delivered = owner_client.post(sealed_url, json=payload)
    assert delivered.status_code == 202 and delivered.json() == {"status": "pending"}
    assert delivered.headers["Cache-Control"] == "no-store"
    assert owner_client.post(sealed_url, json=payload).status_code == 202
    assert owner_client.post(sealed_url, json={"ciphertext_b64": base64.b64encode(b"y" * 384).decode()}).status_code == 409
    async with factory() as db:
        device = await db.get(DevBoxDevice, approval["device_id"])
        assert device.sealed_enrollment == b"x" * 384
        assert device.enrollment_digest != approval["challenge"]
        device.public_key_digest = "f" * 64
        await db.commit()
    assert owner_client.post(sealed_url, json=payload).status_code == 409


@pytest.mark.asyncio
async def test_authenticated_sealed_submission_prepares_only_the_current_host_run(approved, run_store):
    from controller.launch import LaunchTemplate

    factory, approval, url = approved
    public_key = b"synthetic host public key\n"
    digest = hashlib.sha256(public_key).hexdigest()
    async with factory() as db:
        instance = await db.scalar(select(DevBoxInstance))
        instance.image_ref = "ami-approved"
        instance.enrollment_public_key = public_key
        device = await db.get(DevBoxDevice, approval["device_id"])
        device.public_key_digest = digest
        device.sealed_enrollment = None
        await db.commit()
    template = LaunchTemplate(
        "lt-example", "4", "us-east-1", image_id="ami-approved", revision="r1", profile_revision="crew-r1", source_revision="a" * 40
    )
    proof = InstanceProof(
        challenge_digest=hashlib.sha256(approval["challenge"].encode()).hexdigest(),
        public_key_digest=digest,
        profile_proof="crew-profile-ok\ncrew-key-ok\n",
    )
    provider = Instance(image_id="ami-approved")
    assert await reconcile_enrollment(factory, provider, proof, run_store, approval["device_id"], launch_template=template) is None
    assert proof.deliveries == proof.requests == 0 and run_store.table.scan()["Count"] == 0
    sealed_url = f"{url}/{approval['device_id']}/sealed"
    sealed_input = {"ciphertext_b64": base64.b64encode(b"synthetic ciphertext".ljust(384, b"x")).decode()}
    assert client(factory, context("alice-sub")).post(sealed_url, json=sealed_input).status_code == 202
    run_id = await reconcile_enrollment(factory, provider, proof, run_store, approval["device_id"], launch_template=template)
    assert run_id and run_store.read_run(run_id)["status"] == "provisioning"
    assert proof.deliveries == proof.requests == 1
    async with factory() as db:
        device = await db.get(DevBoxDevice, approval["device_id"])
        box = await db.get(DevBox, "host-box")
        assert device.sealed_enrollment is None and box.auth_readiness == "pending"
    assert client(factory, context("alice-sub")).post(sealed_url, json=sealed_input).status_code == 409


@pytest.mark.asyncio
async def test_expired_host_rotates_only_after_both_registry_and_database_fence(approved, run_store, tmp_path):
    from controller.launch import LaunchTemplate

    factory, approval, _ = approved
    old_public = generate_key(root=tmp_path / "old", trusted_uid=os.getuid())
    replacement = generate_key(root=tmp_path / "replacement", trusted_uid=os.getuid())
    old_digest = hashlib.sha256(old_public).hexdigest()
    async with factory() as db:
        instance = await db.scalar(select(DevBoxInstance))
        instance.image_ref = "ami-approved"
        instance.enrollment_public_key = old_public
        device = await db.get(DevBoxDevice, approval["device_id"])
        device.public_key_digest = old_digest
        await db.commit()
        instance_id = instance.id
    template = LaunchTemplate(
        "lt-example", "4", "us-east-1", image_id="ami-approved", revision="r1", profile_revision="crew-r1", source_revision="a" * 40
    )
    first = InstanceProof(
        challenge_digest=hashlib.sha256(approval["challenge"].encode()).hexdigest(),
        public_key_digest=old_digest,
        profile_proof="crew-profile-ok\ncrew-key-ok\n",
    )
    run_id = await reconcile_enrollment(factory, Instance(image_id="ami-approved"), first, run_store, approval["device_id"], launch_template=template)
    assert run_id
    async with factory() as db:
        session = await db.scalar(select(DevBoxAccessSession))
        session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

    class RotationProof:
        def __init__(self):
            self.requests = 0
            self.status = "Failed"

        def send_command(self, **arguments):
            self.requests += 1
            command = arguments["Parameters"]["commands"]
            assert len(command) == 1 and "rm -f /run/adp/crew-ready" in command[0]
            assert (
                command[0].index("rm -f /run/adp/crew-ready")
                < command[0].index("systemctl stop adp-crew-host.service")
                < command[0].index("--rotate-key")
            )
            assert "systemctl stop adp-crew.service" not in command[0]
            assert f"--rotate-key {old_digest}" in command[0]
            assert run_id not in command[0] and approval["challenge"] not in command[0]
            assert arguments["CloudWatchOutputConfig"] == {"CloudWatchOutputEnabled": False}
            return {"Command": {"CommandId": "rotate-command"}}

        def get_command_invocation(self, **arguments):
            assert arguments["CommandId"] == "rotate-command"
            return {
                "Status": self.status,
                "StandardOutputContent": f"crew-profile-ok\n{replacement.decode()}crew-key-ok\n" if self.status == "Success" else "",
            }

    ssm = RotationProof()
    provider = Instance(image_id="ami-approved")
    assert not await reconcile_expired_host_key(factory, provider, ssm, run_store, instance_id, launch_template=template)
    assert ssm.requests == 0 and run_store.read_run(run_id) is not None
    row = run_store.read_run(run_id)
    run_store.table.update_item(
        Key={"event_id": run_id, "arrived_at": row["arrived_at"]},
        UpdateExpression="SET lease_expires_at = :expiry",
        ExpressionAttributeValues={":expiry": int(datetime.now(UTC).timestamp()) - 1},
    )
    assert not await reconcile_expired_host_key(factory, provider, ssm, run_store, instance_id, launch_template=template)
    assert ssm.requests == 1 and run_store.read_run(run_id) is None
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        instance = await db.get(DevBoxInstance, instance_id)
        session = await db.scalar(select(DevBoxAccessSession))
        assert box.status == "running" and box.auth_readiness == "fenced"
        assert instance.enrollment_public_key == old_public and session.terminated_at is not None
    ssm.status = "Success"
    assert await reconcile_expired_host_key(factory, provider, ssm, run_store, instance_id, launch_template=template)
    assert ssm.requests == 2 and run_store.read_run(run_id) is None
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        instance = await db.get(DevBoxInstance, instance_id)
        session = await db.scalar(select(DevBoxAccessSession))
        device = await db.get(DevBoxDevice, approval["device_id"])
        assert box.status == "running" and box.auth_readiness == "pending"
        assert instance.enrollment_public_key == replacement
        assert session.terminated_at is not None and device.revoked_at is not None
    assert not await reconcile_expired_host_key(factory, provider, ssm, run_store, instance_id, launch_template=template)
    assert ssm.requests == 2


@pytest.mark.asyncio
async def test_local_ssm_sealed_writer_refuses_tampering_even_with_old_matching_digest(approved, tmp_path):
    from controller.launch import LaunchTemplate

    factory, approval, _ = approved
    root = tmp_path / "adp"
    public_key = generate_key(root=root, trusted_uid=os.getuid())
    digest = hashlib.sha256(public_key).hexdigest()
    async with factory() as db:
        instance = await db.scalar(select(DevBoxInstance))
        instance.image_ref = "ami-approved"
        device = await db.get(DevBoxDevice, approval["device_id"])
        device.public_key_digest = digest
        await db.commit()
    encrypted = subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-encrypt",
            "-pubin",
            "-inkey",
            str(root / "dev-box-public-key"),
            "-pkeyopt",
            "rsa_padding_mode:oaep",
            "-pkeyopt",
            "rsa_oaep_md:sha256",
        ],
        input=approval["challenge"].encode(),
        capture_output=True,
        check=True,
    ).stdout
    writer = tmp_path / "local-sealed-writer.py"
    writer.write_text(
        "import base64,os,sys\n"
        "from pathlib import Path\n"
        "from image.write_enrollment import receive_sealed\n"
        "receive_sealed(base64.b64decode(sys.stdin.buffer.read(),validate=True),root=Path(sys.argv[1]),trusted_uid=os.getuid())\n"
    )

    class LocalSsm:
        def send_command(self, **arguments):
            assert arguments["CloudWatchOutputConfig"] == {"CloudWatchOutputEnabled": False}
            commands = arguments["Parameters"]["commands"]
            assert len(commands) == 1 and approval["challenge"] not in commands[0]
            command = (
                commands[0]
                .replace("/run/adp", str(root))
                .replace(
                    "python3 /opt/adp/crew/write_enrollment.py --sealed",
                    f"python3 {shlex.quote(str(writer))} {shlex.quote(str(root))}",
                )
            )
            executed = subprocess.run(
                ["bash", "-c", command],
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "user-services" / "dev-box")},
            )
            self.result = {
                "Status": "Success" if executed.returncode == 0 else "Failed",
                "StandardOutputContent": executed.stdout.replace(str(root), "/run/adp"),
            }
            return {"Command": {"CommandId": "local-sealed-command"}}

        def get_command_invocation(self, **arguments):
            assert arguments["CommandId"] == "local-sealed-command"
            return self.result

    template = LaunchTemplate(
        "lt-example", "4", "us-east-1", image_id="ami-approved", revision="r1", profile_revision="crew-r1", source_revision="a" * 40
    )
    ssm = LocalSsm()
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        instance = await db.scalar(select(DevBoxInstance))
        device = await db.get(DevBoxDevice, approval["device_id"])
        assert await deliver_sealed_enrollment(db, Instance(image_id="ami-approved"), ssm, box, instance, device, encrypted, launch_template=template)
        assert (root / "dev-box-enrollment").read_bytes() == approval["challenge"].encode()
        assert not await deliver_sealed_enrollment(
            db, Instance(image_id="ami-approved"), ssm, box, instance, device, encrypted[:-1] + bytes([encrypted[-1] ^ 1]), launch_template=template
        )
        assert (root / "dev-box-enrollment").read_bytes() == approval["challenge"].encode()


@pytest.mark.asyncio
async def test_retired_host_or_expired_approval_cannot_submit_sealed_input(approved):
    factory, approval, url = approved
    sealed_url = f"{url}/{approval['device_id']}/sealed"
    payload = {"ciphertext_b64": base64.b64encode(b"x" * 384).decode()}
    async with factory() as db:
        instance = await db.scalar(select(DevBoxInstance))
        instance.enrollment_public_key = b"synthetic key"
        device = await db.get(DevBoxDevice, approval["device_id"])
        device.public_key_digest = hashlib.sha256(instance.enrollment_public_key).hexdigest()
        device.enrollment_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    assert client(factory, context("alice-sub")).post(sealed_url, json=payload).status_code == 409
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        box.machine_generation += 1
        await db.commit()
    assert client(factory, context("alice-sub")).get("/cloud-desktops/host-box/host-enrollment-key").status_code == 409
    assert client(factory, context("alice-sub")).post(sealed_url, json=payload).status_code == 409


@pytest.mark.asyncio
async def test_expired_sealed_submission_is_cleared_without_run_registry(approved):
    from controller.worker import scan_once

    factory, approval, _ = approved
    async with factory() as db:
        device = await db.get(DevBoxDevice, approval["device_id"])
        device.enrollment_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert device.sealed_enrollment is not None
        await db.commit()
    assert await scan_once(factory, Instance(), InstanceProof(challenge_digest="unused"), "test-deployment", None, "worker") == 0
    async with factory() as db:
        device = await db.get(DevBoxDevice, approval["device_id"])
        assert device.sealed_enrollment is None and device.enrollment_consumed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", [None, "foreign_provider", "image_mismatch", "expired", "retired", "wrong_digest", "failed_command"])
async def test_sealed_controller_delivery_is_scoped_and_digest_verified(approved, denial):
    from controller.launch import LaunchTemplate

    factory, approval, _ = approved
    ciphertext = b"sealed synthetic ciphertext".ljust(384, b"x")
    expected_challenge = hashlib.sha256(approval["challenge"].encode()).hexdigest()
    proof = SealedDeliveryProof(
        expected_challenge if denial != "wrong_digest" else "0" * 64,
        "a" * 64,
        ciphertext,
        status="Failed" if denial == "failed_command" else "Success",
    )
    template = LaunchTemplate(
        "lt-example", "4", "us-east-1", image_id="ami-approved", revision="r1", profile_revision="crew-r1", source_revision="a" * 40
    )
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        instance = await db.scalar(select(DevBoxInstance))
        instance.image_ref = "ami-other" if denial == "image_mismatch" else "ami-approved"
        device = await db.get(DevBoxDevice, approval["device_id"])
        if denial == "expired":
            device.enrollment_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        if denial == "retired":
            device.revoked_at = datetime.now(UTC)
        await db.commit()
        delivered = await deliver_sealed_enrollment(
            db,
            Instance(owner="other" if denial == "foreign_provider" else "alice", image_id="ami-approved"),
            proof,
            box,
            instance,
            device,
            ciphertext,
            launch_template=template,
        )
        assert delivered is (denial is None)
        assert proof.requests == (0 if denial in {"foreign_provider", "image_mismatch", "expired", "retired"} else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [b"short", "ciphertext", b"x" * 385])
async def test_sealed_delivery_rejects_unbounded_or_plaintext_input_before_ssm(approved, invalid):
    factory, approval, _ = approved
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        instance = await db.scalar(select(DevBoxInstance))
        device = await db.get(DevBoxDevice, approval["device_id"])
        proof = SealedDeliveryProof(device.enrollment_digest, device.public_key_digest, b"x" * 384)
        with pytest.raises(ValueError, match="invalid sealed host enrollment"):
            await deliver_sealed_enrollment(db, Instance(), proof, box, instance, device, invalid, launch_template=None)
        assert proof.requests == 0


@pytest.fixture
def run_store():
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="host-enrollment-negative",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}, {"AttributeName": "arrived_at", "AttributeType": "S"}],
        )
        yield CrewRunStore(table, host_role_arn="arn:aws:iam::000000000000:role/guest")


@pytest.mark.parametrize("failure", ["foreign_instance", "foreign_owner", "retired_generation", "expired_challenge", "wrong_challenge", "wrong_key"])
@pytest.mark.asyncio
async def test_invalid_host_proof_never_creates_a_run(approved, run_store, failure):
    factory, approval, url = approved
    foreign = client(factory, context("other-sub", "two")).post(url, json={"public_key_digest": "b" * 64})
    assert foreign.status_code == 404
    assert "i-0123456789abcdef0" not in foreign.text and "alice" not in foreign.text
    forged = client(factory, context("alice-sub")).post(
        url, json={"public_key_digest": "b" * 64, "user_id": "other", "instance_id": "i-11111111111111111", "machine_generation": 2}
    )
    assert forged.status_code == 422
    async with factory() as db:
        if failure == "retired_generation":
            box = await db.get(DevBox, "host-box")
            box.machine_generation = 2
        elif failure == "expired_challenge":
            device = await db.get(DevBoxDevice, approval["device_id"])
            device.enrollment_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    digest = hashlib.sha256(approval["challenge"].encode()).hexdigest()
    witness = InstanceProof(
        challenge_digest="0" * 64 if failure == "wrong_challenge" else digest,
        public_key_digest="b" * 64 if failure == "wrong_key" else "a" * 64,
    )
    provider = Instance(
        instance_id="i-11111111111111111" if failure == "foreign_instance" else "i-0123456789abcdef0",
        owner="other" if failure == "foreign_owner" else "alice",
    )
    assert await reconcile_enrollment(factory, provider, witness, run_store, approval["device_id"]) is None
    assert run_store.table.scan()["Count"] == 0
    async with factory() as db:
        assert await db.scalar(select(func.count(DevBoxAccessSession.id))) == 0
        assert (await db.get(DevBoxDevice, approval["device_id"])).enrollment_consumed_at is None
    if failure in {"foreign_owner", "retired_generation", "expired_challenge", "foreign_instance"}:
        assert witness.requests == 0


@pytest.mark.asyncio
async def test_consumed_enrollment_cannot_issue_a_second_run(approved, run_store):
    factory, approval, url = approved
    digest = hashlib.sha256(approval["challenge"].encode()).hexdigest()
    witness = InstanceProof(challenge_digest=digest)
    run_id = await reconcile_enrollment(factory, Instance(), witness, run_store, approval["device_id"])
    assert run_id
    assert await reconcile_enrollment(factory, Instance(), witness, run_store, approval["device_id"]) is None
    assert client(factory, context("alice-sub")).post(url, json={"public_key_digest": "b" * 64}).status_code == 409
    assert run_store.table.scan()["Count"] == 2
    async with factory() as db:
        assert await db.scalar(select(func.count(DevBoxAccessSession.id))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("run_expired", [False, True])
@pytest.mark.parametrize("profile_pinned", [False, True])
async def test_same_host_enrollment_retry_fences_only_expired_run(approved, run_store, run_expired, profile_pinned):
    from controller.launch import LaunchTemplate

    factory, approval, url = approved
    template = (
        LaunchTemplate("lt-example", "4", "us-east-1", image_id="ami-approved", revision="r1", profile_revision="crew-r1", source_revision="a" * 40)
        if profile_pinned
        else None
    )
    provider = Instance(image_id="ami-approved") if profile_pinned else Instance()
    if profile_pinned:
        async with factory() as db:
            instance = await db.scalar(select(DevBoxInstance))
            instance.image_ref = "ami-approved"
            await db.commit()
    first_proof = InstanceProof(
        challenge_digest=hashlib.sha256(approval["challenge"].encode()).hexdigest(),
        profile_proof="crew-profile-ok\ncrew-key-ok\n" if profile_pinned else None,
    )
    old_run = await reconcile_enrollment(factory, provider, first_proof, run_store, approval["device_id"], launch_template=template)
    assert old_run
    async with factory() as db:
        old_session = await db.scalar(select(DevBoxAccessSession))
        old_session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        old_session_id = old_session.id
        await db.commit()
    if run_expired:
        old_row = run_store.read_run(old_run)
        run_store.table.update_item(
            Key={"event_id": old_run, "arrived_at": old_row["arrived_at"]},
            UpdateExpression="SET lease_expires_at = :expiry",
            ExpressionAttributeValues={":expiry": int(datetime.now(UTC).timestamp()) - 1},
        )
    duplicate = client(factory, context("alice-sub")).post(url, json={"public_key_digest": "a" * 64})
    assert duplicate.status_code == 409
    response = client(factory, context("alice-sub")).post(url, json={"public_key_digest": "b" * 64})
    assert response.status_code == 201
    new_approval = response.json()
    if profile_pinned:
        async with factory() as db:
            pending = await db.get(DevBoxDevice, new_approval["device_id"])
            pending.sealed_enrollment = b"new synthetic ciphertext".ljust(384, b"x")
            await db.commit()
    new_proof = InstanceProof(
        challenge_digest=hashlib.sha256(new_approval["challenge"].encode()).hexdigest(),
        public_key_digest="b" * 64,
        profile_proof="crew-profile-ok\ncrew-key-ok\n" if profile_pinned else None,
    )
    new_run = await reconcile_enrollment(factory, provider, new_proof, run_store, new_approval["device_id"], launch_template=template)
    assert bool(new_run) is run_expired
    assert (run_store.read_run(old_run) is None) is run_expired
    async with factory() as db:
        old_session = await db.get(DevBoxAccessSession, old_session_id)
        new_device = await db.get(DevBoxDevice, new_approval["device_id"])
        assert (old_session.terminated_at is not None) is run_expired
        assert (new_device.enrollment_consumed_at is not None) is run_expired
    if run_expired:
        assert new_run != old_run
        marker = run_store.table.get_item(Key={"event_id": "crew-lease#one#host-box#1", "arrived_at": "crew"})["Item"]
        assert marker["run_id"] == new_run
    else:
        assert run_store.read_run(old_run)["status"] == ("provisioning" if profile_pinned else "active")


@pytest.fixture
async def active_host(approved, run_store, monkeypatch):
    from types import SimpleNamespace

    factory, approval, _ = approved
    challenge_digest = hashlib.sha256(approval["challenge"].encode()).hexdigest()
    run_id = await reconcile_enrollment(factory, Instance(), InstanceProof(challenge_digest=challenge_digest), run_store, approval["device_id"])
    assert run_id
    monkeypatch.setattr(
        "src.proxy.bedrock_principal.get_settings", lambda: SimpleNamespace(webhook_events_table=run_store.table.name, aws_region="us-east-1")
    )
    monkeypatch.setattr("src.proxy.bedrock_principal._get_context_table", lambda: None)
    return factory, run_store, run_id


async def prepare_host_model_token(monkeypatch, store, run_id):
    from src.crew.model_token import mint_model_token
    from src.crew.targets import CrewLease, CrewOwner, ResolvedCrewTarget

    row = store.read_run(run_id)
    owner = CrewOwner(row["tenant_id"], row["user_id"])
    endpoint = "https://10.4.0.3:8443"
    target = ResolvedCrewTarget(
        endpoint,
        CrewLease(
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            target_kind="ec2-instance",
            target_ref=row["session_id"],
            generation=int(row["lease_generation"]),
            expires_at=int(row["lease_expires_at"]),
            endpoint=endpoint,
        ),
        "ready",
        host_run_id=run_id,
        host_instance_ref=row["target_ref"],
        host_fingerprint="a" * 64,
    )

    async def resolve(selected_owner, *, now):
        assert selected_owner == owner
        return target

    monkeypatch.setattr("src.crew.proxy.get_target_resolver", lambda: SimpleNamespace(resolve=resolve))
    monkeypatch.setattr("src.crew.handoff.current_crew_store", lambda: store)
    monkeypatch.setattr("src.agentauth.chat_crew_run.current_crew_store", lambda: store)
    monkeypatch.setattr("src.crew.model_token._secret", lambda: b"fictional-test-signing-key")
    return await mint_model_token(owner, target, now=int(time.time())), owner, target


def worker_context():
    from src.shared.schemas.auth import TokenContext

    identity = TokenContext(
        user_id="gateway-service",
        org_id="one",
        team_id="",
        department_id="",
        account_type="service",
        auth_source="iam",
        scope="internal",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    identity._dev_box_instance_id = "i-0123456789abcdef0"
    identity._dev_box_role_arn = "arn:aws:iam::000000000000:role/guest"
    return identity


@pytest.mark.asyncio
async def test_failed_stop_fence_never_admits_successor_and_confirmed_stop_revokes_run(active_host, monkeypatch):
    from src.proxy.bedrock_principal import BedrockRoutingIdentityError, resolve_routing_principal

    factory, store, run_id = active_host
    http = client(factory, context("alice-sub"))
    stop = "/cloud-desktops/host-box/stop"

    class Unavailable:
        def read_run(self, _run_id):
            raise RuntimeError("registry unavailable")

    monkeypatch.setattr("src.cloud_desktop.host_fencing.current_crew_store", Unavailable)
    refused = http.post(stop, headers={"Idempotency-Key": "stop"})
    assert refused.status_code == 503
    async with factory() as db:
        assert (await db.get(DevBox, "host-box")).desired_state == "running"
        assert await db.scalar(select(func.count(DevBoxAccessSession.id))) == 1
        assert (await resolve_routing_principal(db, worker_context(), agent_run_id=run_id))[1] == "alice"

    monkeypatch.setattr("src.cloud_desktop.host_fencing.current_crew_store", lambda: store)
    admitted = http.post(stop, headers={"Idempotency-Key": "stop"})
    assert admitted.status_code == 202, admitted.text
    assert http.post(stop, headers={"Idempotency-Key": "stop"}).json() == admitted.json()
    assert store.read_run(run_id) is None
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        assert box.auth_readiness == "fenced" and box.desired_state == "stopped"
        session = await db.scalar(select(DevBoxAccessSession))
        assert session.terminated_at is not None
        # A stop keeps the approved device: the controller re-binds on start (#8223 simplification).
        assert (await db.get(DevBoxDevice, session.device_id)).revoked_at is None
        with pytest.raises(BedrockRoutingIdentityError):
            await resolve_routing_principal(db, worker_context(), agent_run_id=run_id)


@pytest.mark.parametrize("revocation", ["membership", "policy", "crew_policy"])
@pytest.mark.asyncio
async def test_offboarding_fences_unattended_run_before_next_request(active_host, monkeypatch, revocation):
    from controller.worker import scan_once

    from src.proxy.bedrock_principal import BedrockRoutingIdentityError, resolve_routing_principal
    from src.shared.models.dev_box import DevBoxPolicy
    from src.shared.models.onboarding import TenantMembership

    factory, store, run_id = active_host
    async with factory() as db:
        if revocation == "policy":
            policy = await db.scalar(select(DevBoxPolicy).where(DevBoxPolicy.org_id == "one"))
            policy.enabled = False
        elif revocation == "membership":
            member = await db.scalar(select(TenantMembership).where(TenantMembership.user_id == "alice"))
            member.revoked_at = datetime.now(UTC)
        else:
            organization = await db.get(Organization, "one")
            organization.settings = {"crew": {"allowed_targets": ["eks-pod"]}}
        await db.commit()
    async with factory() as db:
        with pytest.raises(BedrockRoutingIdentityError):
            await resolve_routing_principal(db, worker_context(), agent_run_id=run_id)
    assert await scan_once(factory, Instance(), InstanceProof(challenge_digest="unused"), "test-deployment", None, "worker", run_store=store) == 1
    assert store.read_run(run_id) is None
    async with factory() as db:
        assert (await db.get(DevBox, "host-box")).auth_readiness == "fenced"
    assert client(factory, context("alice-sub")).post("/cloud-desktops/host-box/start", headers={"Idempotency-Key": "retry"}).status_code in (
        403,
        409,
    )


@pytest.mark.parametrize("revocation", ["policy", "membership", "crew_policy"])
@pytest.mark.asyncio
async def test_offboarding_scrubs_only_the_revoked_host_ingress(active_host, revocation, tmp_path):
    from controller.identity import reconcile_revoked_host_ingress
    from controller.launch import LaunchTemplate
    from controller.worker import scan_once

    from src.shared.models.dev_box import DevBoxPolicy
    from src.shared.models.onboarding import TenantMembership

    factory, store, run_id = active_host
    async with factory() as db:
        instance = await db.scalar(select(DevBoxInstance))
        instance.image_ref = "ami-approved"
        instance.enrollment_public_key = b"host-local-key"
        instance_id = instance.id
        if revocation == "policy":
            policy = await db.scalar(select(DevBoxPolicy).where(DevBoxPolicy.org_id == "one"))
            policy.enabled = False
        elif revocation == "membership":
            member = await db.scalar(select(TenantMembership).where(TenantMembership.user_id == "alice"))
            member.revoked_at = datetime.now(UTC)
        else:
            organization = await db.get(Organization, "one")
            organization.settings = {"crew": {"allowed_targets": ["eks-pod"]}}
        await db.commit()
    template = LaunchTemplate(
        "lt-example", "4", "us-east-1", image_id="ami-approved", revision="r1", profile_revision="crew-r1", source_revision="a" * 40
    )

    class IngressStop:
        status = "Failed"
        requests = 0
        command = ""

        def send_command(self, **arguments):
            self.requests += 1
            assert arguments["InstanceIds"] == ["i-0123456789abcdef0"]
            assert arguments["CloudWatchOutputConfig"] == {"CloudWatchOutputEnabled": False}
            (command,) = arguments["Parameters"]["commands"]
            self.command = command
            assert "crew-profile-ok" in command and "rm -f /run/adp/crew-ready" in command
            assert "systemctl stop adp-crew-host.service" in command
            assert "systemctl stop adp-crew.service" not in command
            assert hashlib.sha256(run_id.encode()).hexdigest() in command
            assert run_id not in command
            assert "server.key" not in command and "client-ca.pem" not in command
            assert subprocess.run(["bash", "-n", "-c", command], capture_output=True).returncode == 0
            return {"Command": {"CommandId": "scrub-command"}}

        def get_command_invocation(self, **arguments):
            assert arguments == {"CommandId": "scrub-command", "InstanceId": "i-0123456789abcdef0"}
            return {"Status": self.status, "StandardOutputContent": "crew-profile-ok\n"}

    ssm = IngressStop()
    provider = Instance(image_id="ami-approved")
    await scan_once(factory, provider, ssm, "test-deployment", template, "worker", run_store=store)
    assert store.read_run(run_id) is None and ssm.requests == 1
    guest_root = tmp_path / "adp"
    guest_root.mkdir()
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    stop = binary_dir / "systemctl"
    stop.write_text('#!/bin/sh\nif [ "$1" = is-active ]; then exit 3; fi\nprintf "%s\\n" "$*" > "$SCRUB_LOG"\n')
    stop.chmod(0o755)
    stop_log = tmp_path / "stopped"
    guest_authority = guest_root / "crew-host.json"
    receipt = guest_root / "crew-ready"
    guest_command = "if " + ssm.command.partition(" && if ")[2]
    guest_command = guest_command.replace("/run/adp/", f"{guest_root}/")
    guest_command = guest_command.replace("observed.st_uid==0", f"observed.st_uid=={os.getuid()}")
    environment = {**os.environ, "PATH": f"{binary_dir}:{os.environ['PATH']}", "SCRUB_LOG": str(stop_log)}

    def execute_guest(authority_run, generation):
        guest_authority.write_text(json.dumps({"run_id": authority_run, "lease_generation": generation}))
        receipt.write_text("ready")
        stop_log.unlink(missing_ok=True)
        return subprocess.run(["bash", "-c", guest_command], env=environment, capture_output=True).returncode

    assert execute_guest("another-run", 1) != 0
    assert receipt.exists() and not stop_log.exists()
    assert execute_guest(run_id, 2) != 0
    assert receipt.exists() and not stop_log.exists()
    guest_authority.unlink()
    guest_authority.symlink_to(tmp_path / "other-authority.json")
    (tmp_path / "other-authority.json").write_text(json.dumps({"run_id": run_id, "lease_generation": 1}))
    assert subprocess.run(["bash", "-c", guest_command], env=environment, capture_output=True).returncode != 0
    assert receipt.exists() and not stop_log.exists()
    guest_authority.unlink()
    assert subprocess.run(["bash", "-c", guest_command], env=environment, capture_output=True).returncode != 0
    assert receipt.exists() and not stop_log.exists()
    receipt.unlink()
    assert subprocess.run(["bash", "-c", guest_command], env=environment, capture_output=True).returncode == 0
    assert execute_guest(run_id, 1) == 0
    assert not receipt.exists() and stop_log.read_text().strip() == "stop adp-crew-host.service"
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        instance = await db.get(DevBoxInstance, instance_id)
        assert box.status == "running" and box.auth_readiness == "fenced"
        assert instance.readiness_class != "host_fenced"

    class UnavailableRegistry:
        def read_run(self, _run_id):
            raise RuntimeError("registry unavailable")

    assert not await reconcile_revoked_host_ingress(
        factory,
        provider,
        ssm,
        UnavailableRegistry(),
        instance_id,
        launch_template=template,
    )
    assert ssm.requests == 1
    await scan_once(factory, Instance(owner="other", image_id="ami-approved"), ssm, "test-deployment", template, "worker", run_store=store)
    assert ssm.requests == 1
    ssm.status = "Success"
    await scan_once(factory, provider, ssm, "test-deployment", template, "worker", run_store=store)
    assert ssm.requests == 2
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        instance = await db.get(DevBoxInstance, instance_id)
        assert box.status == "running" and box.auth_readiness == "fenced"
        assert instance.readiness_class == "host_fenced"
    await scan_once(factory, provider, ssm, "test-deployment", template, "worker", run_store=store)
    assert ssm.requests == 2


@pytest.mark.asyncio
async def test_removed_ec2_policy_denies_existing_host_model_run(active_host):
    from src.proxy.bedrock_principal import BedrockRoutingIdentityError, resolve_routing_principal

    factory, store, run_id = active_host
    async with factory() as db:
        assert (await resolve_routing_principal(db, worker_context(), agent_run_id=run_id))[1] == "alice"
        organization = await db.get(Organization, "one")
        organization.settings = {"crew": {"allowed_targets": ["eks-pod"]}}
        await db.commit()
    async with factory() as db:
        with pytest.raises(BedrockRoutingIdentityError):
            await resolve_routing_principal(db, worker_context(), agent_run_id=run_id)
    assert store.read_run(run_id)["status"] == "active"


@pytest.mark.asyncio
async def test_host_binding_lasts_the_box_lease_and_stop_keeps_the_approved_device(active_host, monkeypatch):
    """One long binding per generation (#8223 simplification): no five-minute lease, no renewal tick.

    The run and the access session minted at enrollment expire with the box's own lease, a stop
    revokes the run and terminates the session but keeps the consumed device, and the owner's
    desktop read names the binding state the browser decides on.
    """
    factory, store, run_id = active_host
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        box.lease_expires_at = datetime.now(UTC) + timedelta(hours=6)
        await db.commit()
    # Re-mint through a fresh approval so the horizon rule (not the fixture's default) is observed.
    async with factory() as db:
        session = await db.scalar(select(DevBoxAccessSession))
        assert session.expires_at.replace(tzinfo=UTC) > datetime.now(UTC) + timedelta(hours=7, minutes=50)
    assert store.read_run(run_id)["lease_expires_at"] > (datetime.now(UTC) + timedelta(hours=7, minutes=50)).timestamp()
    monkeypatch.setattr("src.cloud_desktop.host_fencing.current_crew_store", lambda: store)
    assert client(factory, context("alice-sub")).get("/cloud-desktops/host-box").json()["host_binding"] == "active"
    assert client(factory, context("alice-sub")).post("/cloud-desktops/host-box/stop", headers={"Idempotency-Key": "fence"}).status_code == 202
    assert store.read_run(run_id) is None
    async with factory() as db:
        session = await db.scalar(select(DevBoxAccessSession))
        device = await db.get(DevBoxDevice, session.device_id)
        box = await db.get(DevBox, "host-box")
        assert session.terminated_at is not None
        assert device.revoked_at is None and device.enrollment_consumed_at is not None
        assert box.auth_readiness == "fenced"
    assert client(factory, context("alice-sub")).get("/cloud-desktops/host-box").json()["host_binding"] == "enrolled"


@pytest.mark.asyncio
async def test_controller_rebinds_a_started_box_without_the_owner_browser(active_host, monkeypatch):
    """After stop → start the controller mints the run itself; the browser never enrolls twice."""
    from controller.worker import scan_once

    factory, store, old_run = active_host
    monkeypatch.setattr("src.cloud_desktop.host_fencing.current_crew_store", lambda: store)
    assert client(factory, context("alice-sub")).post("/cloud-desktops/host-box/stop", headers={"Idempotency-Key": "fence"}).status_code == 202
    assert store.read_run(old_run) is None
    # The machine came back on the same generation: what reconcile_state records after a start.
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        box.desired_state = "running"
        box.status = "running"
        box.observed_state = "running"
        box.lease_expires_at = datetime.now(UTC) + timedelta(hours=4)
        instance = await db.scalar(select(DevBoxInstance))
        instance.observed_state = "running"
        instance.readiness_class = "machine_running"
        instance.certificate_fingerprint = "c" * 64
        await db.commit()
    assert client(factory, context("alice-sub")).get("/cloud-desktops/host-box").json()["host_binding"] == "enrolled"
    # A second browser enrollment is refused by the API itself: the device is still approved.
    witness = InstanceProof(challenge_digest="unused")
    assert await scan_once(factory, Instance(), witness, "test-deployment", None, "worker", run_store=store) >= 1
    assert witness.requests == 0, "a re-bind attests nothing over SSM; readiness verifies the host"
    async with factory() as db:
        sessions = (await db.scalars(select(DevBoxAccessSession).order_by(DevBoxAccessSession.expires_at))).all()
        live = [session for session in sessions if session.terminated_at is None]
        box = await db.get(DevBox, "host-box")
        assert len(live) == 1 and live[0].provider_session_ref not in (None, old_run)
        assert live[0].expires_at.replace(tzinfo=UTC) > datetime.now(UTC) + timedelta(hours=3, minutes=50)
        assert box.auth_readiness == "ready"  # no profile pin in this fixture: the run is active at once
        new_run = live[0].provider_session_ref
    row = store.read_run(new_run)
    assert row["status"] == "active" and row["lease_generation"] == 1 and row["target_ref"] == "i-0123456789abcdef0"
    assert client(factory, context("alice-sub")).get("/cloud-desktops/host-box").json()["host_binding"] == "active"
    # Idempotent: a second tick binds nothing more.
    assert await rebind_host_run(factory, Instance(), witness, store, "host-box") is None


def _lease_mirror_table(name):
    """A fresh context table (the owner lease mirror) in the test's moto session, behaving as
    DynamoDB does under the gateway role (#8223 cause 43: unused expression values are refused,
    UpdateItem on crew#* keys is denied)."""
    from tests.crew.lease_mirror_table import GatewayRoleTable

    return GatewayRoleTable(
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=name,
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
        )
    )


def _published_host_lease(table, *, generation=1, expires_at=None, target_ref="host-box", target_kind="ec2-instance", state="ready"):
    """What a verified host leaves in the mirror: the state the pre-fix fence never touched."""
    item = {
        "PK": "crew#one#alice",
        "SK": "executor",
        "tenantId": "one",
        "ownerUserId": "alice",
        "crewLeaseState": state,
        "crewLease": {
            "tenant_id": "one",
            "user_id": "alice",
            "target_kind": target_kind,
            "target_ref": target_ref,
            "generation": generation,
            "expires_at": expires_at or int(time.time()) + 600,
            "endpoint": "https://10.4.0.3:8443",
        },
    }
    table.put_item(Item=item)
    return item


async def _machine_back_on_the_same_generation(factory):
    """What reconcile_state records after a start: running again, Crew verified, same generation."""
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        box.desired_state = "running"
        box.status = "running"
        box.observed_state = "running"
        box.lease_expires_at = datetime.now(UTC) + timedelta(hours=4)
        instance = await db.scalar(select(DevBoxInstance))
        instance.observed_state = "running"
        instance.readiness_class = "crew_ready"
        instance.certificate_fingerprint = "c" * 64
        instance.private_address = "10.4.0.3"
        await db.commit()


@pytest.mark.parametrize("action", ["stop", "replace", "release"])
@pytest.mark.asyncio
async def test_lifecycle_fence_clears_the_owner_lease_mirror(active_host, monkeypatch, action):
    """#8223 cause 39: stop/replace/release revoke the run but left the DynamoDB lease mirror
    holding the pre-fence lease with its original expiry, so publish_host_lease refused every
    same-generation restart as a conflicting live lease. The fence now clears the mirror."""
    factory, store, _ = active_host
    table = _lease_mirror_table(f"lease-mirror-{action}")
    _published_host_lease(table)
    monkeypatch.setattr("src.cloud_desktop.host_fencing.current_crew_store", lambda: store)
    monkeypatch.setattr("src.orchestration.intake_wiring._get_context_table", lambda: table)
    if action == "replace":
        # Replace is admitted only for a stopped, fenced box with a retained workspace disk. Stop
        # first (clearing the mirror), then re-seed the stale lease: an older controller's stop
        # left exactly that behind, and the replace fence must clear it too.
        assert (
            client(factory, context("alice-sub")).post("/cloud-desktops/host-box/stop", headers={"Idempotency-Key": "stop-first"}).status_code == 202
        )
        from src.shared.models.dev_box import DevBoxOperation

        async with factory.begin() as db:
            for operation in await db.scalars(select(DevBoxOperation)):
                operation.status = "succeeded"  # the controller finished the stop
            box = await db.get(DevBox, "host-box")
            box.status = "stopped"
            box.observed_state = "stopped"
            box.desired_state = "stopped"
            db.add(
                DevBoxResource(
                    id="disk-ws",
                    box_id=box.id,
                    user_id=box.user_id,
                    org_id=box.org_id,
                    deployment_id=box.deployment_id,
                    machine_generation=box.machine_generation,
                    resource_kind="workspace_disk",
                    provider_resource_id="vol-09b67859b1a446c98",
                    size_gb=100,
                    encrypted=True,
                    retention_state="retained",
                    deletion_state="present",
                )
            )
        _published_host_lease(table)
    body = {"json": {"workspace_retention": "delete"}} if action == "release" else {}
    response = client(factory, context("alice-sub")).post(f"/cloud-desktops/host-box/{action}", headers={"Idempotency-Key": action}, **body)
    assert response.status_code == 202, response.text
    mirror = table.get_item(Key={"PK": "crew#one#alice", "SK": "executor"})["Item"]
    assert mirror["crewLeaseState"] == "fenced", "fenced, never revoked: revoked is terminal for the owner"
    assert "crewLease" not in mirror
    assert (mirror["tenantId"], mirror["ownerUserId"]) == ("one", "alice")
    async with factory() as db:
        assert (await db.get(DevBox, "host-box")).auth_readiness == "fenced"


@pytest.mark.asyncio
async def test_fence_completes_when_the_lease_mirror_is_unreachable(active_host, monkeypatch):
    """The control DB is the authority and publication tolerates a stale mirror, so a mirror
    failure is logged rather than leaving the stop half-done."""
    factory, store, run_id = active_host
    monkeypatch.setattr("src.cloud_desktop.host_fencing.current_crew_store", lambda: store)

    def unreachable():
        raise RuntimeError("dynamodb unreachable")

    monkeypatch.setattr("src.orchestration.intake_wiring._get_context_table", unreachable)
    assert client(factory, context("alice-sub")).post("/cloud-desktops/host-box/stop", headers={"Idempotency-Key": "fence"}).status_code == 202
    assert store.read_run(run_id) is None
    async with factory() as db:
        assert (await db.get(DevBox, "host-box")).auth_readiness == "fenced"


@pytest.mark.parametrize("mirror_cleared_by_fence", [True, False])
@pytest.mark.asyncio
async def test_stopped_box_restarts_and_publishes_its_lease_again(active_host, monkeypatch, mirror_cleared_by_fence):
    """The live sequence of 2026-10-21 (gen 25): publish → API stop → API start → controller
    re-binds → readiness passes → publication. It must publish whether the fence cleared the
    mirror (new controller) or an older controller left the pre-stop lease in place with an
    unexpired expiry: the run that lease stood for is revoked, so it fences nothing."""
    from controller.worker import scan_once
    from fastapi import HTTPException

    from src.crew.host_lease import publish_host_lease
    from src.crew.model_token import verify_model_token
    from src.crew.targets import CrewOwner

    factory, store, old_run = active_host
    old_token, owner, old_target = await prepare_host_model_token(monkeypatch, store, old_run)
    assert (await verify_model_token(old_token, old_target, now=int(time.time())))["run"] == old_run
    table = _lease_mirror_table("lease-mirror-restart")
    monkeypatch.setattr("src.cloud_desktop.host_fencing.current_crew_store", lambda: store)
    monkeypatch.setattr("src.crew.host_lease.current_crew_store", lambda: store)
    monkeypatch.setattr("src.orchestration.intake_wiring._get_context_table", lambda: table if mirror_cleared_by_fence else None)
    stale = _published_host_lease(table)
    assert client(factory, context("alice-sub")).post("/cloud-desktops/host-box/stop", headers={"Idempotency-Key": "fence"}).status_code == 202
    assert store.read_run(old_run) is None
    with pytest.raises(HTTPException) as fenced:
        await verify_model_token(old_token, old_target, now=int(time.time()))
    assert fenced.value.status_code == 401
    mirror = table.get_item(Key={"PK": "crew#one#alice", "SK": "executor"})["Item"]
    assert ("crewLease" in mirror) is not mirror_cleared_by_fence

    await _machine_back_on_the_same_generation(factory)
    witness = InstanceProof(challenge_digest="unused")
    assert await scan_once(factory, Instance(), witness, "test-deployment", None, "worker", run_store=store) >= 1
    async with factory.begin() as db:
        live = [s for s in (await db.scalars(select(DevBoxAccessSession))).all() if s.terminated_at is None]
        assert len(live) == 1 and live[0].provider_session_ref != old_run
        new_run = live[0].provider_session_ref
        # The tick re-verified the host; readiness then records Crew answering on it.
        (await db.scalar(select(DevBoxInstance))).readiness_class = "crew_ready"

    now = int(time.time())
    await publish_host_lease(factory, table, CrewOwner("one", "alice"), "test-deployment", now)
    mirror = table.get_item(Key={"PK": "crew#one#alice", "SK": "executor"})["Item"]
    assert mirror["crewLeaseState"] == "ready"
    assert mirror["crewLease"]["generation"] == 1 and mirror["crewLease"]["target_ref"] == "host-box"
    assert mirror["crewLease"]["endpoint"] == "https://10.4.0.3:8443"
    assert mirror["crewLease"] != stale["crewLease"], "the new run's lease replaced the stale one"
    assert store.read_run(new_run)["status"] == "active"
    new_token, new_owner, new_target = await prepare_host_model_token(monkeypatch, store, new_run)
    assert new_owner == owner and new_run != old_run
    with pytest.raises(HTTPException) as stale:
        await verify_model_token(old_token, new_target, now=int(time.time()))
    assert stale.value.status_code == 401
    assert (await verify_model_token(new_token, new_target, now=int(time.time())))["run"] == new_run
    async with factory() as db:
        executor = await db.scalar(select(CrewExecutor))
        assert (executor.state, executor.lease_generation) == ("ready", 1)


@pytest.mark.asyncio
async def test_live_rival_run_on_the_same_generation_still_fences_publication(active_host, monkeypatch):
    """Tolerating a stale mirror must not admit a second host while the lease's run is live."""
    from src.crew.host_lease import publish_host_lease
    from src.crew.targets import CrewLeaseFencedError, CrewOwner

    factory, store, run_id = active_host
    table = _lease_mirror_table("lease-mirror-rival")
    monkeypatch.setattr("src.crew.host_lease.current_crew_store", lambda: store)
    await _machine_back_on_the_same_generation(factory)
    stale = _published_host_lease(table)
    # The generation marker names another, still active run: the mirrored lease is live.
    rival = store.read_run(run_id) | {"event_id": "rival-run", "status": "active"}
    store.table.put_item(Item=rival)
    store.table.update_item(
        Key={"event_id": "crew-lease#one#host-box#1", "arrived_at": "crew"},
        UpdateExpression="SET run_id = :rival",
        ExpressionAttributeValues={":rival": "rival-run"},
    )
    with pytest.raises(CrewLeaseFencedError):
        await publish_host_lease(factory, table, CrewOwner("one", "alice"), "test-deployment", int(time.time()))
    assert table.get_item(Key={"PK": "crew#one#alice", "SK": "executor"})["Item"] == stale
    async with factory() as db:
        assert (await db.scalar(select(CrewExecutor))).lease_generation is None


@pytest.mark.asyncio
async def test_release_and_replace_revoke_the_approved_device(active_host, monkeypatch):
    factory, store, run_id = active_host
    monkeypatch.setattr("src.cloud_desktop.host_fencing.current_crew_store", lambda: store)
    response = client(factory, context("alice-sub")).post(
        "/cloud-desktops/host-box/release", json={"workspace_retention": "delete"}, headers={"Idempotency-Key": "release"}
    )
    assert response.status_code == 202
    async with factory() as db:
        session = await db.scalar(select(DevBoxAccessSession))
        device = await db.get(DevBoxDevice, session.device_id)
        assert session.terminated_at is not None and device.revoked_at is not None
    assert store.read_run(run_id) is None


async def _stored_challenge_digest(factory):
    async with factory() as db:
        device = await db.scalar(select(DevBoxDevice))
        return device.enrollment_digest


@pytest.mark.asyncio
async def test_enrollment_polls_the_same_delayed_command(approved, run_store, monkeypatch):
    monkeypatch.setattr("controller.identity.ATTESTATION_POLL_SECONDS", 0, raising=False)
    factory, approval, _ = approved
    proof = DelayedInstanceProof(
        challenge_digest=await _stored_challenge_digest(factory),
        statuses=["InvocationDoesNotExist", "Pending", "InProgress", "Success"],
    )
    run_id = await reconcile_enrollment(factory, Instance(), proof, run_store, approval["device_id"])
    assert run_id and run_store.read_run(run_id)
    assert proof.requests == 1 and proof.reads == 4


@pytest.mark.parametrize("status", ["Failed", "TimedOut", "Cancelled", "Cancelling", "InProgress"])
@pytest.mark.asyncio
async def test_attestation_failure_or_timeout_does_not_mint(approved, run_store, monkeypatch, status):
    from itertools import repeat

    monkeypatch.setattr("controller.identity.ATTESTATION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("controller.identity.ATTESTATION_POLL_SECONDS", 0.005)
    factory, approval, _ = approved
    proof = DelayedInstanceProof(challenge_digest=await _stored_challenge_digest(factory), statuses=repeat(status))
    assert await reconcile_enrollment(factory, Instance(), proof, run_store, approval["device_id"]) is None
    assert proof.requests == 1
    assert run_store.table.scan()["Items"] == []
    async with factory() as db:
        assert (await db.get(DevBoxDevice, approval["device_id"])).enrollment_consumed_at is None


@pytest.mark.parametrize("revocation", ["expiry", "membership", "policy", "device"])
@pytest.mark.asyncio
async def test_attestation_rechecks_authority_before_mint(approved, run_store, monkeypatch, revocation):
    operation = "enroll"
    from controller import identity

    from src.shared.models.dev_box import DevBoxPolicy
    from src.shared.models.onboarding import TenantMembership

    factory, approval, _ = approved
    digest = await _stored_challenge_digest(factory)
    original_attest = identity.attest_host

    async def revoke_after_provider_delay(*arguments):
        result = await original_attest(*arguments)
        async with factory() as db:
            if revocation == "expiry":
                device = await db.get(DevBoxDevice, approval["device_id"])
                device.enrollment_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            elif revocation == "membership":
                member = await db.scalar(select(TenantMembership).where(TenantMembership.user_id == "alice"))
                member.revoked_at = datetime.now(UTC)
            elif revocation == "policy":
                (await db.get(DevBoxPolicy, "p1")).enabled = False
            else:
                (await db.get(DevBoxDevice, approval["device_id"])).revoked_at = datetime.now(UTC)
            await db.commit()
        return result

    monkeypatch.setattr(identity, "attest_host", revoke_after_provider_delay)
    monkeypatch.setattr(identity, "ATTESTATION_POLL_SECONDS", 0)
    proof = DelayedInstanceProof(challenge_digest=digest, statuses=["InProgress", "Success"])
    assert operation == "enroll"
    assert await reconcile_enrollment(factory, Instance(), proof, run_store, approval["device_id"]) is None
    assert run_store.table.scan()["Items"] == []
    assert proof.requests == 1 and proof.reads == 2


@pytest.mark.parametrize("guest_state", ["unenrolled", "active", "retired"])
@pytest.mark.parametrize("run_kind", ["webhook", "eks-pod"])
@pytest.mark.parametrize("registry_role", ["canonical", "explicit"])
@pytest.mark.asyncio
async def test_registered_guest_cannot_select_a_foreign_non_host_run(active_host, monkeypatch, guest_state, run_kind, registry_role):
    from types import SimpleNamespace

    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from src.auth.middleware import TokenContextMiddleware
    from src.proxy.bedrock_principal import BedrockRoutingIdentityError, resolve_routing_principal
    from src.proxy.routes import get_proxy_service, router
    from src.shared.config import get_settings

    factory, store, host_run_id = active_host
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        if guest_state == "retired":
            box.machine_generation += 1
        elif guest_state == "unenrolled":
            box.auth_readiness = "pending"
            access = await db.scalar(select(DevBoxAccessSession))
            await db.delete(access)
        await db.commit()
    store.table.put_item(
        Item={
            "event_id": "bob-webhook-run",
            "arrived_at": "now",
            "kind": "webhook",
            "status": "running",
            "tenant_id": "one",
            "user_id": "bob",
            "root_human_id": "bob",
            "is_human_rooted": True,
        }
    )
    foreign_run_id = "bob-webhook-run"
    if run_kind == "eks-pod":
        now = int(datetime.now(UTC).timestamp())
        foreign_run_id = store.mint(
            tenant_id="one",
            user_id="bob",
            session_id="bob-session",
            lease_generation=1,
            lease_expires_at=now + 300,
            target_kind="eks-pod",
            target_ref="bob-pod",
            now=now,
        )
        header = {
            "status": "active",
            "sessionMode": "persistent",
            "sessionState": "running",
            "tenantId": "one",
            "ownerUserId": "bob",
            "ttl": now + 300,
            "chatLease": {"generation": 1, "sandbox_uid": "bob-pod", "run_id": "session-run", "expires_at": now + 300},
        }
        monkeypatch.setattr("src.proxy.bedrock_principal._get_context_table", lambda: SimpleNamespace(get_item=lambda **_: {"Item": header}))
    settings = get_settings().model_copy(update={"trust_apigw_headers": True, "apigw_provenance_secret": "offline-edge-proof"})
    monkeypatch.setattr("src.auth.middleware.get_settings", lambda: settings)
    monkeypatch.setattr("src.auth.caller_provenance.get_settings", lambda: settings)
    entry = {
        "agent_id": "guest",
        "agent_name": "guest",
        "org_id": "one",
        "team_id": "",
        "scope": "internal",
        "role_arn": "arn:aws:iam::000000000000:role/adp-dev-box-guest-example",
    }
    if registry_role == "explicit":
        entry.update(role_arn="arn:aws:iam::000000000000:role/guest", requires_host_identity=True)
    monkeypatch.setattr("src.auth.agent_registry.get_agent_registry_service", lambda: SimpleNamespace(get_agent_by_role_arn=lambda _arn: entry))
    routed = []

    class Service:
        async def invoke_model(self, model_id, body, identity, *, agent_run_id, **_kwargs):
            async with factory() as db:
                try:
                    _, owner = await resolve_routing_principal(db, identity, agent_run_id=agent_run_id)
                except BedrockRoutingIdentityError as exc:
                    raise HTTPException(403, "Run not authorized") from exc
            routed.append(owner)
            return {"content": [{"text": "reply"}], "usage": {"input_tokens": 1, "output_tokens": 1}}

    app = FastAPI()
    app.add_middleware(TokenContextMiddleware)
    app.include_router(router)
    app.dependency_overrides[get_proxy_service] = Service
    headers = {
        "X-Caller-Identity": f"arn:aws:sts::000000000000:assumed-role/{entry['role_arn'].rsplit('/', 1)[-1]}/i-0123456789abcdef0",
        "X-Adp-Edge-Provenance": "offline-edge-proof",
        "X-Agent-RunId": foreign_run_id,
    }
    http = TestClient(app)
    refused = http.post("/model/approved-model/invoke", headers=headers, json={})
    assert refused.status_code == 403, refused.text
    assert not routed
    assert "bob" not in refused.text
    from src.auth.agent_registry import agent_entry_to_token_context

    async with factory() as db:
        with pytest.raises(BedrockRoutingIdentityError) as denied:
            await resolve_routing_principal(db, agent_entry_to_token_context(entry), agent_run_id=foreign_run_id)
        assert denied.value.details["reason"] == "host_run_required"
    assert http.post("/model/approved-model/invoke", headers={**headers, "X-Agent-RunId": ""}, json={}).status_code == 403
    host_row = store.read_run(host_run_id)
    store.table.update_item(
        Key={"event_id": host_run_id, "arrived_at": host_row["arrived_at"]},
        UpdateExpression="SET host_role_arn = :role",
        ExpressionAttributeValues={":role": entry["role_arn"]},
    )
    token, _, _ = await prepare_host_model_token(monkeypatch, store, host_run_id)
    own = http.post("/model/approved-model/invoke", headers={**headers, "X-Agent-RunId": host_run_id, "X-Adp-Crew-Model-Token": token}, json={})
    assert own.status_code == (200 if guest_state == "active" else 403), own.text
    assert routed == (["alice"] if guest_state == "active" else [])
    routed.clear()
    entry.update(agent_id="worker", role_arn="arn:aws:iam::000000000000:role/worker", requires_host_identity=False)
    headers["X-Caller-Identity"] = "arn:aws:sts::000000000000:assumed-role/worker/job"
    permitted = http.post("/model/approved-model/invoke", headers=headers, json={})
    assert permitted.status_code == 200, permitted.text
    assert routed == ["bob"]


@pytest.mark.asyncio
async def test_stop_does_not_admit_successor_while_revocation_is_in_flight(active_host, monkeypatch):
    import asyncio
    from threading import Event

    from src.proxy.bedrock_principal import read_routing_run

    factory, store, run_id = active_host
    entered = Event()
    proceed = Event()

    class PausedStore:
        def read_run(self, identifier):
            return store.read_run(identifier)

        def revoke_for_lease(self, **arguments):
            entered.set()
            if not proceed.wait(timeout=5):
                raise TimeoutError("fence did not complete")
            return store.revoke_for_lease(**arguments)

    monkeypatch.setattr("src.cloud_desktop.host_fencing.current_crew_store", PausedStore)
    http = client(factory, context("alice-sub"))
    pending = asyncio.create_task(asyncio.to_thread(http.post, "/cloud-desktops/host-box/stop", headers={"Idempotency-Key": "race-stop"}))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert not pending.done()
        assert await read_routing_run(run_id) is not None
    finally:
        proceed.set()
    admitted = await pending
    assert admitted.status_code == 202, admitted.text
    assert await read_routing_run(run_id) is None
    assert store.read_run(run_id) is None


@pytest.mark.parametrize(
    "path",
    [
        "/model/approved-model/invoke-with-response-stream",
        "/bedrock/invoke-with-response-stream",
        "/v1/chat/completions",
        "/v1/messages",
    ],
)
@pytest.mark.asyncio
async def test_host_run_cannot_enter_streaming_or_other_model_routes(active_host, path):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from src.proxy.routes import get_proxy_service, get_token_context, router

    factory, _, run_id = active_host
    app = FastAPI()
    app.include_router(router)

    async def authenticated(request: Request):
        request.state.token_context = worker_context()
        return request.state.token_context

    app.dependency_overrides[get_token_context] = authenticated
    app.dependency_overrides[get_proxy_service] = lambda: None
    response = TestClient(app).post(
        path,
        headers={"x-agent-runid": run_id},
        json={"model": "approved-model", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 403, response.text
    assert "host-box" not in response.text and "i-0123456789abcdef0" not in response.text
    async with factory() as db:
        assert (await db.scalar(select(DevBoxAccessSession))).terminated_at is None


@pytest.mark.asyncio
async def test_host_run_uses_one_provider_attempt_after_identity_check(active_host):
    from src.proxy.bedrock_enforcement import RoutingDecision
    from src.proxy.bedrock_principal import resolve_routing_principal
    from src.proxy.service import ProxyService

    factory, _, run_id = active_host
    identity = worker_context()
    async with factory() as db:
        await resolve_routing_principal(db, identity, agent_run_id=run_id)
    assert identity._dev_box_host_run_id == run_id

    class Pool:
        async def get_client(self, credentials, *, single_attempt=False):
            assert credentials is None
            return single_attempt

    service = ProxyService.__new__(ProxyService)
    service._pool_service = Pool()
    assert await service._client_for_request(RoutingDecision(), identity) is True
    assert await service._client_for_request(RoutingDecision(), worker_context()) is False


@pytest.mark.asyncio
async def test_release_waits_for_authoritative_revocation(active_host, monkeypatch):
    factory, store, run_id = active_host
    http = client(factory, context("alice-sub"))
    url = "/cloud-desktops/host-box/release"
    options = {"workspace_retention": "retain"}

    class Unavailable:
        def read_run(self, identifier):
            raise RuntimeError("run registry unavailable")

    monkeypatch.setattr("src.cloud_desktop.host_fencing.current_crew_store", Unavailable)
    refused = http.post(url, headers={"Idempotency-Key": "release"}, json=options)
    assert refused.status_code == 503
    async with factory() as db:
        assert (await db.get(DevBox, "host-box")).machine_generation == 1
        assert await db.scalar(select(func.count(DevBoxAccessSession.id))) == 1
    assert store.read_run(run_id) is not None

    monkeypatch.setattr("src.cloud_desktop.host_fencing.current_crew_store", lambda: store)
    admitted = http.post(url, headers={"Idempotency-Key": "release"}, json=options)
    assert admitted.status_code == 202, admitted.text
    assert store.read_run(run_id) is None
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        assert box.auth_readiness == "fenced" and box.desired_state == "released"


@pytest.mark.asyncio
async def test_host_model_route_requires_its_verified_instance_and_scrubs_audit(active_host, monkeypatch, caplog):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from src.crew.proxy import router as crew_router
    from src.proxy.bedrock_principal import BedrockRoutingIdentityError, resolve_routing_principal
    from src.proxy.routes import get_proxy_service, get_token_context, router

    factory, store, run_id = active_host
    token, owner, target = await prepare_host_model_token(monkeypatch, store, run_id)
    caller = [None]
    monkeypatch.setattr("src.auth.caller_provenance.verified_caller_identity", lambda _request: caller[0])
    app = FastAPI()
    app.include_router(router)
    app.include_router(crew_router)
    delivered = []

    async def deliver(selected_owner, selected_target, *, now):
        delivered.append((selected_owner, selected_target))

    monkeypatch.setattr("src.crew.handoff.deliver_model_token", deliver)
    calls = []
    logs = []

    async def authenticated(request: Request):
        request.state.token_context = worker_context()
        request.state.token_context._dev_box_instance_id = None
        request.state.token_context._dev_box_role_arn = None
        return request.state.token_context

    class Service:
        async def invoke_model(self, model_id, body, identity, *, agent_run_id, **_kwargs):
            async with factory() as db:
                assert (await resolve_routing_principal(db, identity, agent_run_id=agent_run_id))[1] == "alice"
            calls.append((model_id, body))
            return {"content": [{"text": "reply"}], "usage": {"input_tokens": 2, "output_tokens": 1}}

    app.dependency_overrides[get_token_context] = authenticated
    app.dependency_overrides[get_proxy_service] = Service

    class Audit:
        def log_chat_async(self, **facts):
            logs.append(facts)

    monkeypatch.setattr("src.proxy.routes.get_chat_logging_service", Audit)
    http = TestClient(app)
    request = {"messages": [{"role": "user", "content": "test"}]}
    headers = {"X-Agent-RunId": run_id, "Authorization": "sensitive-signature", "X-Adp-Crew-Model-Token": token}
    for actor in (
        None,
        "arn:aws:sts::000000000000:assumed-role/guest/i-11111111111111111",
        "arn:aws:sts::000000000000:assumed-role/guest/i-invalid",
        "arn:aws:sts::000000000000:assumed-role/other/i-0123456789abcdef0",
    ):
        caller[0] = actor
        refused = http.post("/model/approved-model/invoke", json=request, headers=headers)
        assert refused.status_code == 403, refused.text
        assert http.post("/crew/model-token/refresh", headers=headers).status_code == 403
        assert run_id not in refused.text and "i-0123456789abcdef0" not in refused.text
    assert not calls and not logs

    caller[0] = "arn:aws:sts::000000000000:assumed-role/guest/i-0123456789abcdef0"
    assert http.post("/crew/model-token/refresh", headers=headers).status_code == 204
    assert delivered == [(owner, target)]
    permitted = http.post("/model/approved-model/invoke", json=request, headers=headers)
    assert permitted.status_code == 200, permitted.text
    assert calls == [("approved-model", request)]
    receipts = store.table.query(KeyConditionExpression=Key("event_id").eq(f"crew-model#{run_id}"))["Items"]
    assert len(receipts) == 1 and receipts[0]["status"] == "confirmed"
    assert len(logs) == 1
    assert logs[0]["headers"] is None
    assert logs[0]["request_body"] == logs[0]["response_body"] == {}
    assert run_id not in str(logs[0]) and "sensitive-signature" not in str(logs[0])
    assert run_id not in caplog.text and "sensitive-signature" not in caplog.text and token not in caplog.text

    async with factory() as db:
        wrong = worker_context()
        wrong._dev_box_instance_id = "i-11111111111111111"
        with pytest.raises(BedrockRoutingIdentityError) as refused:
            await resolve_routing_principal(db, wrong, agent_run_id=run_id)
        assert refused.value.details["reason"] == "host_fenced"

    store.revoke_for_lease(
        tenant_id=owner.tenant_id, session_id=target.lease.target_ref, lease_generation=target.lease.generation, expected_run_id=run_id
    )
    denied = http.post("/model/approved-model/invoke", json=request, headers=headers)
    assert denied.status_code in (401, 403, 409)
    assert http.post("/crew/model-token/refresh", headers=headers).status_code in (401, 403, 409)
    assert delivered == [(owner, target)]
    assert calls == [("approved-model", request)]
    assert len(store.table.query(KeyConditionExpression=Key("event_id").eq(f"crew-model#{run_id}"))["Items"]) == 1


@pytest.mark.asyncio
async def test_signed_current_host_reports_assistant_heartbeat_and_consolidation(active_host, monkeypatch):
    import asyncio

    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from src.crew.proxy import get_token_context
    from src.crew.proxy import router as crew_router

    factory, store, run_id = active_host
    model_token, owner, target = await prepare_host_model_token(monkeypatch, store, run_id)
    async with factory.begin() as db:
        box = await db.get(DevBox, "host-box")
        box.auth_readiness = "ready"
        instance = await db.scalar(select(DevBoxInstance).where(DevBoxInstance.box_id == box.id))
        instance.observed_state = "running"
    monkeypatch.setattr("src.crew.proxy.get_session_factory", lambda: factory)
    monkeypatch.setattr("src.crew.handoff.deliver_model_token", lambda *_args, **_kwargs: asyncio.sleep(0))
    caller = [None]
    monkeypatch.setattr("src.auth.caller_provenance.verified_caller_identity", lambda _request: caller[0])
    app = FastAPI()
    app.include_router(crew_router)

    async def authenticated(request: Request):
        request.state.token_context = worker_context()
        request.state.token_context._dev_box_instance_id = None
        request.state.token_context._dev_box_role_arn = None
        return request.state.token_context

    app.dependency_overrides[get_token_context] = authenticated
    http = TestClient(app)
    headers = {"X-Agent-RunId": run_id, "Authorization": "fictional-signature"}
    path = "/crew/model-token/refresh"
    busy = {"busy": True, "consolidation_complete": False}
    assert http.post(path, json=busy, headers=headers).status_code == 403
    headers["X-Adp-Crew-Model-Token"] = model_token
    caller[0] = "arn:aws:sts::000000000000:assumed-role/other/i-0123456789abcdef0"
    assert http.post(path, json=busy, headers=headers).status_code == 403
    caller[0] = "arn:aws:sts::000000000000:assumed-role/guest/i-0123456789abcdef0"
    assert http.post(path, json=busy, headers={"X-Agent-RunId": run_id, "Authorization": "fictional-signature"}).status_code == 401
    assert http.post(path, json={"busy": True, "consolidation_complete": True}, headers=headers).status_code == 422
    assert http.post(path, json=busy, headers=headers).status_code == 204
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        assert box.assistant_activity_at and box.assistant_busy_until_at
        assert box.assistant_consolidated_at is None
        assert box.last_activity_at is None or box.last_activity_at < box.assistant_activity_at
    assert http.post(path, json={"busy": False, "consolidation_complete": True}, headers=headers).status_code == 204
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        assert box.assistant_consolidated_at and box.assistant_busy_until_at is None
    store.revoke_for_lease(
        tenant_id=owner.tenant_id, session_id=target.lease.target_ref, lease_generation=target.lease.generation, expected_run_id=run_id
    )
    assert http.post(path, json=busy, headers=headers).status_code in (401, 403, 409)
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        assert box.assistant_consolidated_at and box.assistant_busy_until_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("preloaded_common", [False, True])
async def test_existing_ec2_harness_checks_mounted_host_identity_and_fencing(approved, run_store, monkeypatch, preloaded_common):
    import asyncio
    import importlib.util
    from types import ModuleType

    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from src.proxy.bedrock_principal import resolve_routing_principal
    from src.proxy.routes import get_proxy_service, get_token_context, router

    factory, pending, _ = approved
    async with factory() as db:
        await db.delete(await db.get(DevBoxDevice, pending["device_id"]))
        await db.commit()
    common_before = ModuleType("common") if preloaded_common else None
    if common_before is None:
        monkeypatch.delitem(sys.modules, "common", raising=False)
    else:
        monkeypatch.setitem(sys.modules, "common", common_before)
    remote_root = Path(__file__).resolve().parents[4] / "tests/e2e/cli_uplift/remote"
    module_names = ("common", "assistant_client", "assistant_baseline", "assistant_isolation_clients", "dev_box_lifecycle")
    modules_before = {name: sys.modules.get(name) for name in module_names}
    with monkeypatch.context() as imports:
        for name in module_names:
            spec = importlib.util.spec_from_file_location(name, remote_root / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            imports.setitem(sys.modules, name, module)
            spec.loader.exec_module(module)
        harness = module
    assert all(sys.modules.get(name) is modules_before[name] for name in module_names)
    assert harness.common.__file__ == str(remote_root / "common.py")
    monkeypatch.setattr(
        "src.proxy.bedrock_principal.get_settings",
        lambda: type("Settings", (), {"webhook_events_table": run_store.table.name, "aws_region": "us-east-1"})(),
    )
    owner = client(factory, context("alice-sub"))
    foreign = client(factory, context("other-sub", "two"))
    sessions = {"a1": owner, "b1": foreign}

    def mounted_request(_gateway, session, path, *, method="GET", body=None, key=None):
        response = session.request(method, path, json=body, headers={"Idempotency-Key": key} if key else None)
        return response.status_code, response.json()

    monkeypatch.setattr(harness, "request", mounted_request)
    app = FastAPI()
    app.include_router(router)
    caller = ["arn:aws:sts::000000000000:assumed-role/guest/i-0123456789abcdef0"]
    monkeypatch.setattr("src.auth.caller_provenance.verified_caller_identity", lambda _request: caller[0])

    async def authenticated(request: Request):
        request.state.token_context = worker_context()
        request.state.token_context._dev_box_instance_id = None
        request.state.token_context._dev_box_role_arn = None
        return request.state.token_context

    class Model:
        async def invoke_model(self, _model_id, _body, identity, *, agent_run_id, **_kwargs):
            async with factory() as db:
                principal, human = await resolve_routing_principal(db, identity, agent_run_id=agent_run_id)
            return {"root_human_id": human, "tenant_id": principal.org_id, "usage": {"input_tokens": 1, "output_tokens": 1}}

    app.dependency_overrides[get_token_context] = authenticated
    app.dependency_overrides[get_proxy_service] = Model
    monkeypatch.setattr("src.proxy.routes.get_chat_logging_service", lambda: type("Audit", (), {"log_chat_async": lambda *_args, **_kwargs: None})())
    model = TestClient(app)

    model_token = [None]

    class Probe:
        def public_key_digest(self, _actor, _box_id):
            return "a" * 64

        def attest(self, _actor, _box_id, device_id, challenge):
            digest = hashlib.sha256(challenge.encode()).hexdigest()
            result = asyncio.run(reconcile_enrollment(factory, Instance(), InstanceProof(challenge_digest=digest), run_store, device_id))
            model_token[0], _, _ = asyncio.run(prepare_host_model_token(monkeypatch, run_store, result))
            return result

        def invoke(self, _actor, _box_id, run_id, variant):
            caller[0] = "arn:aws:sts::000000000000:assumed-role/guest/i-0123456789abcdef0"
            path = "/model/approved-model/invoke"
            if variant == "foreign_instance":
                caller[0] = "arn:aws:sts::000000000000:assumed-role/guest/i-11111111111111111"
            elif variant == "foreign_role":
                caller[0] = "arn:aws:sts::000000000000:assumed-role/other/i-0123456789abcdef0"
            elif variant == "foreign_run":
                run_id = "00000000-0000-4000-8000-000000000000"
            elif variant == "direct_bedrock":
                return 403, {}
            elif variant == "other_route":
                path = "/bedrock/invoke"
            response = model.post(path, json={"messages": []}, headers={"X-Agent-RunId": run_id, "X-Adp-Crew-Model-Token": model_token[0]})
            return response.status_code, response.json()

    probe = Probe()
    run_id = await asyncio.to_thread(
        harness.verify_host_identity,
        "https://example.invalid",
        sessions,
        {"a1": {"canonical_user_id": "alice", "tenant_id": "one"}},
        "a1",
        "host-box",
        probe,
    )
    assert run_store.read_run(run_id)["host_role_arn"] == "arn:aws:iam::000000000000:role/guest"
    monkeypatch.setattr("src.cloud_desktop.host_fencing.current_crew_store", lambda: run_store)
    assert owner.post("/cloud-desktops/host-box/stop", headers={"Idempotency-Key": "fence"}).status_code == 202
    stale_status, _ = await asyncio.to_thread(probe.invoke, "a1", "host-box", run_id, "stale")
    assert stale_status == 403


@pytest.mark.asyncio
async def test_controller_keeps_fencing_but_does_not_admit_host_without_gateway_grant(approved, run_store):
    from controller.worker import scan_once

    factory, approval, _ = approved
    run_store.host_role_arn = None
    witness = InstanceProof(challenge_digest=hashlib.sha256(approval["challenge"].encode()).hexdigest())
    assert await scan_once(factory, Instance(), witness, "test-deployment", None, "worker", run_store=run_store) == 0
    assert witness.requests == 0
    assert run_store.table.scan()["Count"] == 0
    async with factory() as db:
        assert (await db.get(DevBox, "host-box")).auth_readiness != "ready"
        assert await db.scalar(select(func.count(DevBoxAccessSession.id))) == 0
        assert (await db.get(DevBoxDevice, approval["device_id"])).enrollment_consumed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_readiness", ["ready", "unknown"])
async def test_automatic_lease_stop_fences_host_before_scheduling(active_host, auth_readiness):
    from controller.worker import schedule_stops
    from fastapi import HTTPException

    from src.shared.models.dev_box import DevBoxOperation

    factory, store, run_id = active_host
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        box.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        box.auth_readiness = auth_readiness
        await db.commit()

    class Unavailable:
        def read_run(self, _run_id):
            raise OSError("registry unavailable")

    with pytest.raises(HTTPException) as refused:
        await schedule_stops(factory, "test-deployment", run_store=Unavailable())
    assert refused.value.status_code == 503
    async with factory() as db:
        assert (await db.get(DevBox, "host-box")).desired_state == "running"
        assert await db.scalar(select(func.count(DevBoxOperation.id))) == 0
    assert store.read_run(run_id) is not None

    assert await schedule_stops(factory, "test-deployment", run_store=store) == 1
    assert store.read_run(run_id) is None
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        assert box.desired_state == "stopped" and box.auth_readiness == "fenced"
        assert (await db.scalar(select(DevBoxOperation))).action == "stop"


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [None, "database_image", "provider_image", "box_profile"])
async def test_profile_pinned_host_run_refuses_unapproved_instance(approved, run_store, mismatch):
    from controller.launch import LaunchTemplate

    factory, approval, _ = approved
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        instance = await db.scalar(select(DevBoxInstance).where(DevBoxInstance.box_id == box.id))
        instance.image_ref = "ami-unapproved" if mismatch == "database_image" else "ami-approved"
        if mismatch == "box_profile":
            box.profile = "other"
        await db.commit()
    provider = Instance(image_id="ami-unapproved" if mismatch == "provider_image" else "ami-approved")
    witness = InstanceProof(
        challenge_digest=hashlib.sha256(approval["challenge"].encode()).hexdigest(), profile_proof="crew-profile-ok\ncrew-key-ok\n"
    )
    template = LaunchTemplate(
        "lt-example", "4", "us-east-1", image_id="ami-approved", revision="r1", profile_revision="crew-r1", source_revision="a" * 40
    )
    run_id = await reconcile_enrollment(factory, provider, witness, run_store, approval["device_id"], launch_template=template)
    assert bool(run_id) is (mismatch is None)
    assert witness.requests == (1 if mismatch is None else 0)
    if mismatch:
        assert run_store.table.scan()["Count"] == 0
        async with factory() as db:
            assert (await db.get(DevBox, "host-box")).auth_readiness != "ready"
            assert (await db.get(DevBoxDevice, approval["device_id"])).enrollment_consumed_at is None
    else:
        async with factory() as db:
            assert (await db.get(DevBox, "host-box")).auth_readiness == "pending"
        assert run_store.read_run(run_id)["status"] == "provisioning"


@pytest.mark.asyncio
async def test_pinned_enrollment_cannot_route_guest_model_before_restore(approved, run_store, monkeypatch):
    from controller.launch import LaunchTemplate

    from src.cloud_desktop.host_identity import current_host
    from src.proxy.bedrock_principal import BedrockRoutingIdentityError, resolve_routing_principal

    factory, approval, _ = approved
    async with factory() as db:
        instance = await db.scalar(select(DevBoxInstance))
        instance.image_ref = "ami-approved"
        await db.commit()
    witness = InstanceProof(
        challenge_digest=hashlib.sha256(approval["challenge"].encode()).hexdigest(), profile_proof="crew-profile-ok\ncrew-key-ok\n"
    )
    template = LaunchTemplate(
        "lt-example", "4", "us-east-1", image_id="ami-approved", revision="r1", profile_revision="crew-r1", source_revision="a" * 40
    )
    run_id = await reconcile_enrollment(
        factory, Instance(image_id="ami-approved"), witness, run_store, approval["device_id"], launch_template=template
    )
    assert run_id and run_store.read_run(run_id)["status"] == "provisioning"
    monkeypatch.setattr(
        "src.proxy.bedrock_principal.get_settings",
        lambda: SimpleNamespace(webhook_events_table=run_store.table.name, aws_region="us-east-1"),
    )
    monkeypatch.setattr("src.proxy.bedrock_principal._get_context_table", lambda: None)
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        assert box.auth_readiness == "pending"
        assert (
            await current_host(
                db,
                box_id=box.id,
                generation=box.machine_generation,
                provider_instance_id="i-0123456789abcdef0",
                user_id=box.user_id,
                org_id=box.org_id,
                deployment_id=box.deployment_id,
                run_id=run_id,
            )
            is None
        )
        with pytest.raises(BedrockRoutingIdentityError) as denied:
            await resolve_routing_principal(db, worker_context(), agent_run_id=run_id)
        assert denied.value.details["reason"] == "unknown_run"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile_proof,status",
    [
        ("", "Success"),
        ("wrong-profile\n", "Success"),
        ("crew-profile-ok\n", "Success"),
        ("crew-profile-ok\nwrong-key\n", "Success"),
        ("crew-profile-ok\ncrew-key-ok\n", "Failed"),
    ],
)
async def test_pinned_host_run_requires_installed_profile(approved, run_store, profile_proof, status):
    from controller.launch import LaunchTemplate

    factory, approval, _ = approved
    async with factory() as db:
        instance = await db.scalar(select(DevBoxInstance))
        instance.image_ref = "ami-approved"
        await db.commit()
    witness = InstanceProof(challenge_digest=hashlib.sha256(approval["challenge"].encode()).hexdigest(), profile_proof=profile_proof, status=status)
    template = LaunchTemplate(
        "lt-example", "4", "us-east-1", image_id="ami-approved", revision="r1", profile_revision="crew-r1", source_revision="a" * 40
    )
    assert (
        await reconcile_enrollment(factory, Instance(image_id="ami-approved"), witness, run_store, approval["device_id"], launch_template=template)
        is None
    )
    assert witness.requests == 1 and run_store.table.scan()["Count"] == 0
    async with factory() as db:
        assert (await db.get(DevBox, "host-box")).auth_readiness != "ready"
        assert (await db.get(DevBoxDevice, approval["device_id"])).enrollment_consumed_at is None


@pytest.mark.asyncio
async def test_pinned_host_run_requires_release_source(approved, run_store):
    from controller.launch import LaunchTemplate

    factory, approval, _ = approved
    async with factory() as db:
        instance = await db.scalar(select(DevBoxInstance))
        instance.image_ref = "ami-approved"
        await db.commit()
    witness = InstanceProof(challenge_digest=hashlib.sha256(approval["challenge"].encode()).hexdigest())
    template = LaunchTemplate("lt-example", "4", "us-east-1", image_id="ami-approved", revision="r1", profile_revision="crew-r1")
    assert (
        await reconcile_enrollment(factory, Instance(image_id="ami-approved"), witness, run_store, approval["device_id"], launch_template=template)
        is None
    )
    assert witness.requests == 0 and run_store.table.scan()["Count"] == 0


def test_installed_profile_readback_checks_source_and_required_services(tmp_path):
    prefix = tmp_path / "crew"
    unit_dir = tmp_path / "units"
    revision = prefix / "crew-r1"
    (revision / "venv/bin").mkdir(parents=True)
    (revision / "host").mkdir()
    unit_dir.mkdir()
    (prefix / "current").symlink_to("crew-r1")
    (revision / "profile.json").write_text(json.dumps({"revision": "crew-r1", "source_revision": "a" * 40}))
    (revision / "venv/bin/kirocrew").touch()
    (revision / "host/crew-host-dashboard.js").touch()
    (revision / "host/crew-host-model.js").touch()
    (revision / "host/crew-host-bootstrap.js").touch()
    for name in ("adp-crew.service", "adp-crew-host.service"):
        (unit_dir / name).touch()
    command = crew_profile_command("crew-r1", "a" * 40).replace("/opt/adp/crew", str(prefix)).replace("/etc/systemd/system", str(unit_dir))

    def check():
        return subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)

    assert check().stdout == "crew-profile-ok\n"
    (revision / "profile.json").write_text(json.dumps({"revision": "crew-r1", "source_revision": "b" * 40}))
    assert check().returncode != 0
    (revision / "profile.json").write_text(json.dumps({"revision": "crew-r1", "source_revision": "a" * 40}))
    (unit_dir / "adp-crew-host.service").unlink()
    assert check().returncode != 0
    (unit_dir / "adp-crew-host.service").touch()
    (prefix / "current").unlink()
    (prefix / "current").symlink_to("different")
    assert check().returncode != 0


def test_pinned_host_readback_requires_own_matching_private_key(tmp_path):
    root = tmp_path / "adp"
    public = generate_key(root=root, trusted_uid=os.getuid())
    command = crew_key_command().replace("/run/adp", str(root)).replace("item.st_uid==0", f"item.st_uid=={os.getuid()}")

    def check():
        return subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)

    assert check().stdout == "crew-key-ok\n"
    key = root / "dev-box-private-key"
    key.chmod(0o644)
    assert check().returncode != 0
    key.chmod(0o600)
    public_path = root / "dev-box-public-key"
    public_path.write_bytes(b"wrong-key")
    assert check().returncode != 0
    public_path.write_bytes(public)
    key.rename(root / "old-private-key")
    key.symlink_to(root / "old-private-key")
    assert check().returncode != 0


@pytest.mark.asyncio
async def test_explicit_owner_enrollment_binds_one_inert_canonical_executor(approved):
    factory, approval, url = approved
    async with factory() as db:
        executor = await db.scalar(select(CrewExecutor))
        assert (executor.tenant_id, executor.user_id, executor.dev_box_id, executor.generation) == ("one", "alice", "host-box", 1)
        assert executor.target_kind == "ec2-instance" and executor.state == "provisioning"
        assert executor.lease_generation is None and executor.lease_expires_at is None
        assert executor.target_ref is None
        assert await db.scalar(select(func.count(CrewExecutor.id))) == 1
        old_device = await db.get(DevBoxDevice, approval["device_id"])
        old_device.revoked_at = datetime.now(UTC)
        instance = await db.scalar(select(DevBoxInstance))
        instance.enrollment_public_key = b"next observed key"
        await db.commit()
    digest = hashlib.sha256(b"next observed key").hexdigest()
    assert client(factory, context("alice-sub")).post(url, json={"public_key_digest": digest}).status_code == 201
    async with factory() as db:
        assert await db.scalar(select(func.count(CrewExecutor.id))) == 1
        executor = await db.scalar(select(CrewExecutor))
        assert executor.dev_box_id == "host-box" and executor.generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("from_pod", (False, True))
@pytest.mark.parametrize("conflicting_lease", (False, True))
async def test_verified_current_host_publishes_owner_lease_once(approved, run_store, monkeypatch, conflicting_lease, from_pod):
    from unittest.mock import AsyncMock

    from controller.launch import LaunchTemplate

    from src.crew.inventory import CrewExecutorInventory
    from src.crew.targets import CrewLeaseFencedError, CrewOwner, OwnerTargetResolver

    # This fixture models an attested host; its example private address is not
    # a live endpoint. Transport readiness has its own probe tests.
    answers = AsyncMock(return_value=True)
    monkeypatch.setattr("src.crew.host_readiness.crew_host_answers", answers)
    factory, approval, url = approved
    generation = 2 if from_pod else 1
    instance_ref = "i-0123456789abcdef1" if from_pod else "i-0123456789abcdef0"
    context_table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="host-lease-fixture",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
    )
    async with factory.kw["bind"].begin() as connection:
        await connection.run_sync(CrewCheckpoint.__table__.create, checkfirst=True)
    monkeypatch.setattr("src.crew.host_lease.current_crew_store", lambda: run_store)
    monkeypatch.setattr("src.orchestration.intake_wiring._get_context_table", lambda: context_table)
    if from_pod:
        pod_run = run_store.mint(
            tenant_id="one",
            user_id="alice",
            session_id="pod-session",
            lease_generation=1,
            lease_expires_at=int(time.time()) + 300,
            target_kind="eks-pod",
            target_ref="pod-name",
            executor_scope="user",
            now=int(time.time()),
        )
        run_row = run_store.read_run(pod_run)
        run_store.table.update_item(
            Key={"event_id": pod_run, "arrived_at": run_row["arrived_at"]},
            UpdateExpression="SET pod_uid = :uid",
            ExpressionAttributeValues={":uid": "pod-uid"},
        )
        run_store.table.update_item(
            Key={"event_id": "crew-owner#one#alice", "arrived_at": "crew"},
            UpdateExpression="SET pod_uid = :uid",
            ExpressionAttributeValues={":uid": "pod-uid"},
        )
        context_table.put_item(
            Item={
                "PK": "crew#one#alice",
                "SK": "executor",
                "tenantId": "one",
                "ownerUserId": "alice",
                "crewLeaseState": "ready",
                "crewLease": {
                    "tenant_id": "one",
                    "user_id": "alice",
                    "target_kind": "eks-pod",
                    "target_ref": "pod-uid",
                    "generation": 1,
                    "expires_at": int(time.time()) + 300,
                    "endpoint": "http://10.4.0.2:5476",
                },
            }
        )
        async with factory.begin() as db:
            previous = await db.scalar(select(CrewExecutor))
            previous.target_kind = "eks-pod"
            previous.dev_box_id = None
            previous.state = "ready"
            previous.target_ref = "pod-uid"
            previous.lease_generation = 1
            previous.lease_expires_at = int(time.time()) + 300
            box = await db.get(DevBox, "host-box")
            box.machine_generation = generation
            old_device = await db.get(DevBoxDevice, approval["device_id"])
            old_device.revoked_at = datetime.now(UTC)
            db.add(
                DevBoxInstance(
                    box_id="host-box",
                    user_id="alice",
                    org_id="one",
                    deployment_id="test-deployment",
                    machine_generation=generation,
                    provider_instance_id=instance_ref,
                    template_revision="r1",
                    observed_state="running",
                    enrollment_public_key=b"new observed key",
                )
            )
        enrolled = client(factory, context("alice-sub")).post(
            url,
            json={"public_key_digest": hashlib.sha256(b"new observed key").hexdigest()},
        )
        assert enrolled.status_code == 201, enrolled.text
        assert run_store.read_run(pod_run) is None
        assert "Item" not in context_table.get_item(Key={"PK": "crew#one#alice", "SK": "executor"})
        approval = enrolled.json()
    now = int(time.time())
    run_id = run_store.mint(
        tenant_id="one",
        user_id="alice",
        session_id="host-box",
        lease_generation=generation,
        lease_expires_at=now + 300,
        target_kind="ec2-instance",
        target_ref=instance_ref,
        deployment_id="test-deployment",
        provisioning=True,
        now=now,
    )
    async with factory.begin() as db:
        box = await db.get(DevBox, "host-box")
        box.auth_readiness = "pending"
        instance = await db.scalar(select(DevBoxInstance).where(DevBoxInstance.machine_generation == generation))
        instance.private_address = "10.4.0.3"
        instance.image_ref = "ami-approved"
        instance.certificate_fingerprint = "a" * 64
        device = await db.get(DevBoxDevice, approval["device_id"])
        device.enrollment_consumed_at = datetime.now(UTC)
        db.add(
            DevBoxAccessSession(
                box_id=box.id,
                user_id="alice",
                org_id="one",
                deployment_id="test-deployment",
                machine_generation=generation,
                device_id=device.id,
                provider_session_ref=run_id,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        executor = await db.scalar(select(CrewExecutor).where(CrewExecutor.generation == generation))
        executor.owner_key = "a" * 52
        executor.latest_verified_checkpoint_id = f"verified-{generation}"
        db.add(
            CrewCheckpoint(
                id=f"verified-{generation}",
                executor_id=executor.id,
                tenant_id="one",
                user_id="alice",
                owner_key=executor.owner_key,
                target_kind="eks-pod",
                generation=generation - 1 or 1,
                sequence=7,
                idempotency_key="prior",
                status="verified",
                manifest_sha256="b" * 64,
                manifest_version_id="v7",
                crew_version="0.11.2",
                archive_bucket="fixture-bucket",
                archive_key=f"crew-checkpoints/{executor.owner_key}/verified-{generation}/archive",
                archive_version_id="v7",
                archive_sha256="c" * 64,
                archive_size=100,
                manifest_bucket="fixture-bucket",
                manifest_key=f"crew-checkpoints/{executor.owner_key}/verified-{generation}/manifest",
                manifest_size=50,
            )
        )

    class ReadyProvider(Instance):
        def describe_instances(self, **arguments):
            assert arguments["InstanceIds"] == [instance_ref]
            tags = {
                "adp:component": "dev-box",
                "adp:deployment-id": "test-deployment",
                "adp:owner-id": "alice",
                "adp:box-id": "host-box",
                "adp:generation": str(generation),
            }
            return {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": instance_ref,
                                "ImageId": "ami-approved",
                                "State": {"Name": "running"},
                                "PrivateIpAddress": "10.4.0.3",
                                "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
                            }
                        ]
                    }
                ]
            }

    class Readback:
        def send_command(self, **arguments):
            command = arguments["Parameters"]["commands"][0]
            assert "crew-host-bootstrap.js" in command and run_id not in command
            assert json.loads(shlex.split(command)[-1])["run_id_sha256"] == hashlib.sha256(run_id.encode()).hexdigest()
            return {"Command": {"CommandId": "verified-restore"}}

        def get_command_invocation(self, **_arguments):
            return {"Status": "Success", "StandardOutputContent": "crew-profile-ok\ncrew-bootstrap-ok\ncrew-ready-ok\n"}

    template = LaunchTemplate(
        "lt-example",
        "4",
        "us-east-1",
        image_id="ami-approved",
        revision="r1",
        profile_revision="crew-r1",
        source_revision="a" * 40,
        gateway_execute_arn="arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev",
        crew_model_id="us.anthropic.example-v1:0",
    )
    assert await reconcile_host_readiness(factory, ReadyProvider(), Readback(), run_store, instance.id, launch_template=template)
    # #9455: the attested run is active before any owner-side resolution; the host's own routes
    # must not wait for somebody to open the dashboard (dev 2026-11-01/02: 11–14 min of 403s).
    assert run_store.read_run(run_id)["status"] == "active"
    monkeypatch.setattr("src.crew.host_lease.current_crew_store", lambda: run_store)
    resolver = OwnerTargetResolver(CrewExecutorInventory(factory), context_table, deployment_id="test-deployment")
    owner = CrewOwner("one", "alice")
    if conflicting_lease:
        context_table.put_item(
            Item={
                "PK": "crew#one#alice",
                "SK": "executor",
                "tenantId": "one",
                "ownerUserId": "alice",
                "crewLeaseState": "ready",
                "crewLease": {"generation": generation - 1, "expires_at": now + 300},
            }
        )
        with pytest.raises(CrewLeaseFencedError):
            await resolver.resolve(owner, now=now)
        # The conflicting mirror still fences the owner's resolution; it no longer withholds the
        # run's activation, which the controller completed at attestation.
        assert run_store.read_run(run_id)["status"] == "active"
        answers.assert_not_awaited()
        return
    target = await resolver.resolve(owner, now=now)
    assert target.lease.generation == generation and target.lease.target_ref == "host-box"
    assert target.host_run_id == run_id and target.host_fingerprint == "a" * 64
    assert run_store.read_run(run_id)["status"] == "active"
    assert (await resolver.resolve(owner, now=now)).endpoint == "https://10.4.0.3:8443"
    assert answers.await_count == 2
    assert all(call.args[0].host_run_id == run_id and call.args[0].lease.generation == generation for call in answers.await_args_list)
    async with factory() as db:
        executor = await db.scalar(select(CrewExecutor).where(CrewExecutor.generation == generation))
        assert (executor.state, executor.generation, executor.lease_generation) == ("ready", generation, generation)
        assert await db.scalar(select(func.count(DevBox.id))) == 1
        assert await db.scalar(select(func.count(CrewExecutor.id))) == generation
    lease = context_table.get_item(Key={"PK": "crew#one#alice", "SK": "executor"})["Item"]
    assert lease["crewLeaseState"] == "ready"
    assert lease["crewLease"]["generation"] == generation

    async with factory.begin() as db:
        box = await db.get(DevBox, "host-box")
        box.machine_generation = generation + 1
    with pytest.raises(CrewLeaseFencedError):
        await resolver.resolve(owner, now=now)
    assert context_table.get_item(Key={"PK": "crew#one#alice", "SK": "executor"})["Item"] == lease
    assert answers.await_count == 2


@pytest.mark.asyncio
async def test_unready_host_cannot_publish_owner_lease(approved, run_store, monkeypatch):
    from src.crew.host_lease import publish_host_lease
    from src.crew.targets import CrewLeaseFencedError, CrewOwner

    factory, _, _ = approved
    now = int(time.time())
    context_table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="conflicting-host-lease",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
    )
    context_table.put_item(
        Item={
            "PK": "crew#one#alice",
            "SK": "executor",
            "tenantId": "one",
            "ownerUserId": "alice",
            "crewLeaseState": "ready",
            "crewLease": {"generation": 1, "expires_at": now + 300},
        }
    )
    monkeypatch.setattr("src.crew.host_lease.current_crew_store", lambda: run_store)
    with pytest.raises(CrewLeaseFencedError):
        await publish_host_lease(factory, context_table, CrewOwner("one", "alice"), "test-deployment", now)
    async with factory() as db:
        executor = await db.scalar(select(CrewExecutor))
        assert executor.state == "provisioning" and executor.lease_generation is None


@pytest.mark.asyncio
async def test_changed_pod_lease_prevents_owner_handoff(approved, run_store, monkeypatch):
    factory, approval, url = approved
    table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="changed-pod-owner-lease",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"}, {"AttributeName": "SK", "AttributeType": "S"}],
    )
    table.put_item(
        Item={
            "PK": "crew#one#alice",
            "SK": "executor",
            "tenantId": "one",
            "ownerUserId": "alice",
            "crewLeaseState": "ready",
            "crewLease": {
                "tenant_id": "one",
                "user_id": "alice",
                "target_kind": "eks-pod",
                "target_ref": "pod-uid",
                "generation": 3,
                "expires_at": int(time.time()) + 300,
                "endpoint": "http://10.4.0.2:5476",
            },
        }
    )
    monkeypatch.setattr("src.crew.host_lease.current_crew_store", lambda: run_store)
    monkeypatch.setattr("src.orchestration.intake_wiring._get_context_table", lambda: table)
    async with factory.begin() as db:
        pod = await db.scalar(select(CrewExecutor))
        pod.target_kind = "eks-pod"
        pod.dev_box_id = None
        pod.state = "ready"
        pod.target_ref = "pod-uid"
        pod.lease_generation = 1
        pod.lease_expires_at = int(time.time()) + 300
        box = await db.get(DevBox, "host-box")
        box.machine_generation = 2
        old_device = await db.get(DevBoxDevice, approval["device_id"])
        old_device.revoked_at = datetime.now(UTC)
        db.add(
            DevBoxInstance(
                box_id=box.id,
                user_id="alice",
                org_id="one",
                deployment_id="test-deployment",
                machine_generation=2,
                provider_instance_id="i-0123456789abcdef1",
                template_revision="r1",
                observed_state="running",
                enrollment_public_key=b"new observed key",
            )
        )
    result = client(factory, context("alice-sub")).post(
        url,
        json={"public_key_digest": hashlib.sha256(b"new observed key").hexdigest()},
    )
    assert result.status_code == 409
    async with factory() as db:
        assert await db.scalar(select(func.count(DevBox.id))) == 1
        assert await db.scalar(select(func.count(CrewExecutor.id))) == 1
        assert (await db.scalar(select(CrewExecutor))).state == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("active_pod", [False, True])
async def test_owner_handoff_never_rewinds_pod_generation_or_mints_second_executor(approved, active_pod):
    factory, approval, url = approved
    async with factory() as db:
        executor = await db.scalar(select(CrewExecutor))
        executor.target_kind = "eks-pod"
        executor.dev_box_id = None
        executor.state = "ready" if active_pod else "deleted"
        old_device = await db.get(DevBoxDevice, approval["device_id"])
        old_device.revoked_at = datetime.now(UTC)
        instance = await db.scalar(select(DevBoxInstance))
        instance.enrollment_public_key = b"next observed key"
        await db.commit()
    digest = hashlib.sha256(b"next observed key").hexdigest()
    response = client(factory, context("alice-sub")).post(url, json={"public_key_digest": digest})
    assert response.status_code == 409
    async with factory() as db:
        assert await db.scalar(select(func.count(CrewExecutor.id))) == 1
        assert await db.scalar(select(func.count(DevBoxDevice.id))) == 1


@pytest.mark.asyncio
async def test_host_enrollment_inherits_pod_checkpoint_only_at_newer_box_generation(approved):
    factory, approval, url = approved
    async with factory() as db:
        previous = await db.scalar(select(CrewExecutor))
        previous.state = "deleted"
        previous.target_kind = "eks-pod"
        previous.dev_box_id = None
        previous.latest_verified_checkpoint_id = "verified-pod-capture"
        box = await db.get(DevBox, "host-box")
        box.machine_generation = 2
        old_device = await db.get(DevBoxDevice, approval["device_id"])
        old_device.revoked_at = datetime.now(UTC)
        db.add(
            DevBoxInstance(
                box_id="host-box",
                user_id="alice",
                org_id="one",
                deployment_id="test-deployment",
                machine_generation=2,
                provider_instance_id="i-new-observed",
                template_revision="r1",
                observed_state="running",
                enrollment_public_key=b"new observed key",
            )
        )
        await db.commit()
    digest = hashlib.sha256(b"new observed key").hexdigest()
    response = client(factory, context("alice-sub")).post(url, json={"public_key_digest": digest})
    assert response.status_code == 201, response.text
    async with factory() as db:
        rows = (await db.scalars(select(CrewExecutor).order_by(CrewExecutor.generation))).all()
        assert [(row.generation, row.target_kind, row.state) for row in rows] == [(1, "eks-pod", "deleted"), (2, "ec2-instance", "provisioning")]
        assert rows[1].dev_box_id == "host-box"
        assert rows[1].latest_verified_checkpoint_id == "verified-pod-capture"
        assert rows[1].lease_generation is None


async def test_controller_attests_box_then_stores_only_public_fingerprint(approved):
    from controller.launch import LaunchTemplate

    factory, _, _ = approved
    async with factory.begin() as db:
        instance = await db.scalar(select(DevBoxInstance))
        instance.private_address = "10.4.0.3"
        instance.image_ref = "ami-approved"
        instance_id = instance.id

    class OwnedBox(Instance):
        def describe_instances(self, **kwargs):
            response = super().describe_instances(**kwargs)
            response["Reservations"][0]["Instances"][0]["PrivateIpAddress"] = "10.4.0.3"
            return response

    class PublicFingerprint:
        def __init__(self):
            self.requests = []

        def send_command(self, **kwargs):
            self.requests.append(kwargs)
            return {"Command": {"CommandId": "certificate-proof"}}

        def get_command_invocation(self, **kwargs):
            return {"Status": "Success", "StandardOutputContent": "crew-profile-ok\n" + "a" * 64 + "\n"}

    witness = PublicFingerprint()
    template = LaunchTemplate(
        "lt-example", "4", "us-east-1", image_id="ami-approved", revision="r1", profile_revision="crew-r1", source_revision="a" * 40
    )
    assert await reconcile_host_fingerprint(factory, OwnedBox(image_id="ami-approved"), witness, instance_id, launch_template=template)
    assert len(witness.requests) == 1
    command = witness.requests[0]["Parameters"]["commands"][0]
    assert "--private-ip 10.4.0.3 --generation 1 --fingerprint" in command
    assert "server.key" not in command and "server.pem" not in command
    async with factory() as db:
        assert (await db.get(DevBoxInstance, instance_id)).certificate_fingerprint == "a" * 64
    async with factory.begin() as db:
        (await db.get(DevBox, "host-box")).machine_generation = 2
    assert not await reconcile_host_fingerprint(factory, OwnedBox(image_id="ami-approved"), witness, instance_id, launch_template=template)
    assert len(witness.requests) == 1


class BurnedKeyRotationProof:
    """Fake SSM for the expired-approval rotation: one shell command that retires ``old_digest`` and prints ``replacement``."""

    def __init__(self, old_digest, replacement, *, status="Failed"):
        self.old_digest = old_digest
        self.replacement = replacement
        self.status = status
        self.requests = 0

    def send_command(self, **arguments):
        self.requests += 1
        assert arguments["InstanceIds"] == ["i-0123456789abcdef0"]
        command = arguments["Parameters"]["commands"]
        assert len(command) == 1 and f"--rotate-key {self.old_digest}" in command[0]
        assert (
            command[0].index("rm -f /run/adp/crew-ready")
            < command[0].index("systemctl stop adp-crew-host.service")
            < command[0].index("--rotate-key")
        )
        assert arguments["CloudWatchOutputConfig"] == {"CloudWatchOutputEnabled": False}
        return {"Command": {"CommandId": "burned-rotate-command"}}

    def get_command_invocation(self, **arguments):
        assert arguments == {"CommandId": "burned-rotate-command", "InstanceId": "i-0123456789abcdef0"}
        return {
            "Status": self.status,
            "StandardOutputContent": f"crew-profile-ok\n{self.replacement.decode()}crew-key-ok\n" if self.status == "Success" else "",
        }


async def _burn_current_key(factory, approval, old_public):
    """Make the fixture's approval an expired, never-consumed row that names the host's current key."""
    async with factory() as db:
        instance = await db.scalar(select(DevBoxInstance))
        instance.image_ref = "ami-approved"
        instance.enrollment_public_key = old_public
        device = await db.get(DevBoxDevice, approval["device_id"])
        device.public_key_digest = hashlib.sha256(old_public).hexdigest()
        device.enrollment_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        device.sealed_enrollment = None
        await db.commit()
    return device.id


@pytest.mark.asyncio
async def test_expired_unconsumed_approval_rotates_the_burned_host_key(approved, tmp_path):
    from controller.launch import LaunchTemplate

    factory, approval, url = approved
    old_public = generate_key(root=tmp_path / "old", trusted_uid=os.getuid())
    replacement = generate_key(root=tmp_path / "replacement", trusted_uid=os.getuid())
    device_id = await _burn_current_key(factory, approval, old_public)
    template = LaunchTemplate(
        "lt-example", "4", "us-east-1", image_id="ami-approved", revision="r1", profile_revision="crew-r1", source_revision="a" * 40
    )
    provider = Instance(image_id="ami-approved")
    ssm = BurnedKeyRotationProof(hashlib.sha256(old_public).hexdigest(), replacement)

    # The owner cannot approve again while the current key is on file for the lapsed row.
    retry = client(factory, context("alice-sub")).post(url, json={"public_key_digest": hashlib.sha256(old_public).hexdigest()})
    assert retry.status_code == 409 and "new public key" in retry.json()["detail"]

    # A failed rotation leaves the row open so the next tick retries.
    assert not await reconcile_expired_enrollment(factory, provider, ssm, device_id, launch_template=template)
    assert ssm.requests == 1
    async with factory() as db:
        device = await db.get(DevBoxDevice, device_id)
        instance = await db.scalar(select(DevBoxInstance))
        assert device.revoked_at is None and instance.enrollment_public_key == old_public

    ssm.status = "Success"
    assert await reconcile_expired_enrollment(factory, provider, ssm, device_id, launch_template=template)
    assert ssm.requests == 2
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        device = await db.get(DevBoxDevice, device_id)
        instance = await db.scalar(select(DevBoxInstance))
        assert instance.enrollment_public_key == replacement
        assert device.revoked_at is not None and device.sealed_enrollment is None
        # Never enrolled: the box stays in its pre-enrollment readiness rather than pretending a run is pending.
        assert box.auth_readiness == "unknown" and box.status == "running"

    # Closed rows are not rotated twice, and the owner's next approval now succeeds with the replacement.
    assert not await reconcile_expired_enrollment(factory, provider, ssm, device_id, launch_template=template)
    assert ssm.requests == 2
    approved_again = client(factory, context("alice-sub")).post(url, json={"public_key_digest": hashlib.sha256(replacement).hexdigest()})
    assert approved_again.status_code == 201


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["pending_approval", "live_run", "fenced_host", "stale_generation", "retired_key"])
async def test_expired_approval_rotation_defers_to_live_state_and_only_closes_dead_rows(approved, tmp_path, guard):
    from controller.launch import LaunchTemplate

    factory, approval, _ = approved
    old_public = generate_key(root=tmp_path / "old", trusted_uid=os.getuid())
    replacement = generate_key(root=tmp_path / "replacement", trusted_uid=os.getuid())
    device_id = await _burn_current_key(factory, approval, old_public)
    now = datetime.now(UTC)
    async with factory() as db:
        device = await db.get(DevBoxDevice, device_id)
        box = await db.get(DevBox, "host-box")
        if guard == "pending_approval":
            db.add(
                DevBoxDevice(
                    id="sibling-approval",
                    box_id=device.box_id,
                    user_id=device.user_id,
                    org_id=device.org_id,
                    deployment_id=device.deployment_id,
                    machine_generation=device.machine_generation,
                    instance_id=device.instance_id,
                    public_key_digest="b" * 64,
                    enrollment_digest="c" * 64,
                    enrollment_expires_at=now + timedelta(minutes=4),
                    auth_epoch=0,
                )
            )
        elif guard == "live_run":
            db.add(
                DevBoxAccessSession(
                    box_id=device.box_id,
                    user_id=device.user_id,
                    org_id=device.org_id,
                    deployment_id=device.deployment_id,
                    machine_generation=device.machine_generation,
                    device_id=device.id,
                    expires_at=now + timedelta(minutes=4),
                    provider_session_ref="run-live",
                )
            )
        elif guard == "fenced_host":
            box.auth_readiness = "fenced"
        elif guard == "stale_generation":
            box.machine_generation = 2  # the box was replaced; the row names the retired generation
        elif guard == "retired_key":
            device.public_key_digest = "d" * 64
        await db.commit()
    template = LaunchTemplate(
        "lt-example", "4", "us-east-1", image_id="ami-approved", revision="r1", profile_revision="crew-r1", source_revision="a" * 40
    )
    ssm = BurnedKeyRotationProof(hashlib.sha256(old_public).hexdigest(), replacement, status="Success")
    assert not await reconcile_expired_enrollment(factory, Instance(image_id="ami-approved"), ssm, device_id, launch_template=template)
    assert ssm.requests == 0
    async with factory() as db:
        device = await db.get(DevBoxDevice, device_id)
        instance = await db.scalar(select(DevBoxInstance))
        assert instance.enrollment_public_key == old_public
        closed = guard in {"stale_generation", "retired_key"}
        assert (device.revoked_at is not None) is closed


@pytest.mark.asyncio
async def test_controller_tick_rotates_only_expired_unconsumed_approvals(approved, run_store, monkeypatch, tmp_path):
    from controller.launch import LaunchTemplate
    from controller.worker import scan_once

    factory, approval, _ = approved
    old_public = generate_key(root=tmp_path / "old", trusted_uid=os.getuid())
    device_id = await _burn_current_key(factory, approval, old_public)
    async with factory() as db:
        device = await db.get(DevBoxDevice, device_id)
        db.add(
            DevBoxDevice(
                id="live-approval",
                box_id=device.box_id,
                user_id=device.user_id,
                org_id=device.org_id,
                deployment_id=device.deployment_id,
                machine_generation=device.machine_generation,
                instance_id=device.instance_id,
                public_key_digest="b" * 64,
                enrollment_digest="c" * 64,
                enrollment_expires_at=datetime.now(UTC) + timedelta(minutes=4),
                auth_epoch=0,
            )
        )
        await db.commit()
    template = LaunchTemplate(
        "lt-example", "4", "us-east-1", image_id="ami-approved", revision="r1", profile_revision="crew-r1", source_revision="a" * 40
    )
    expired, live = [], []

    async def record_expired(_factory, _ec2, _ssm, observed_id, *, launch_template):
        expired.append((observed_id, launch_template.profile_revision))
        return False

    async def record_live(_factory, _ec2, _ssm, _store, observed_id, *, launch_template):
        live.append(observed_id)

    async def ignore(*_args, **_kwargs):
        return None

    monkeypatch.setattr("controller.worker.reconcile_expired_enrollment", record_expired)
    monkeypatch.setattr("controller.worker.reconcile_enrollment", record_live)
    monkeypatch.setattr("controller.worker.reconcile_host_fingerprint", ignore)
    await scan_once(
        factory,
        Instance(image_id="ami-approved"),
        InstanceProof(challenge_digest="unused"),
        "test-deployment",
        template,
        "worker",
        run_store=run_store,
    )
    assert expired == [(device_id, "crew-r1")]
    assert live == ["live-approval"]


@pytest.mark.asyncio
@pytest.mark.parametrize("old_lease", ["none", "expired", "live"])
async def test_replaced_box_retires_the_previous_generations_host_executor_on_approval(approved, old_lease):
    """After a replace, the executor bound to the retired machine must not block the new generation.

    Only an executor still holding an unexpired lease keeps refusing: ending that lease is the
    controller's fence, not the owner's approval.
    """
    factory, _, url = approved
    key = b"replacement generation host public key"
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        box.machine_generation = 2
        db.add(
            DevBoxInstance(
                box_id="host-box",
                user_id="alice",
                org_id="one",
                deployment_id="test-deployment",
                machine_generation=2,
                provider_instance_id="i-0123456789abcdef1",
                template_revision="r1",
                observed_state="running",
                enrollment_public_key=key,
            )
        )
        executor = await db.scalar(select(CrewExecutor))
        assert (executor.generation, executor.state) == (1, "provisioning")
        if old_lease != "none":
            executor.lease_generation = 1
            executor.lease_expires_at = int(time.time()) + (300 if old_lease == "live" else -1)
        await db.commit()
    response = client(factory, context("alice-sub")).post(url, json={"public_key_digest": hashlib.sha256(key).hexdigest()})
    async with factory() as db:
        rows = (await db.scalars(select(CrewExecutor).order_by(CrewExecutor.generation))).all()
    if old_lease == "live":
        assert response.status_code == 409
        assert [(row.generation, row.state) for row in rows] == [(1, "provisioning")]
        return
    assert response.status_code == 201, response.text
    assert [(row.generation, row.state, row.dev_box_id) for row in rows] == [(1, "deleted", "host-box"), (2, "provisioning", "host-box")]
    assert rows[0].lease_generation is None and rows[0].lease_expires_at is None and rows[0].target_ref is None


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["crew-profile-ok\ncrew-bootstrap-ok\ncrew-ready-ok\n", ""])
async def test_first_host_without_checkpoint_is_verified_with_a_fresh_scope(pending_host, proof):
    """An owner's first Crew host has no verified checkpoint (#8171); it is verified fresh, not refused."""
    from controller.launch import LaunchTemplate

    factory, run_store, run_id = pending_host
    async with factory.begin() as db:
        executor = await db.scalar(select(CrewExecutor).where(CrewExecutor.user_id == "alice"))
        executor.latest_verified_checkpoint_id = None
        box = await db.get(DevBox, "host-box")
        # The owner's retained workspace disk (storage.py attaches it as /dev/xvdf): readiness
        # must mount it as the Crew home first, addressed by volume id (#8223).
        db.add(
            DevBoxResource(
                id="disk-ws",
                box_id=box.id,
                user_id=box.user_id,
                org_id=box.org_id,
                deployment_id=box.deployment_id,
                machine_generation=box.machine_generation,
                resource_kind="workspace_disk",
                provider_resource_id="vol-09b67859b1a446c98",
                size_gb=100,
                encrypted=True,
                retention_state="retained",
                deletion_state="present",
            )
        )

    class Owned(Instance):
        def describe_instances(self, **kwargs):
            data = super().describe_instances(**kwargs)
            data["Reservations"][0]["Instances"][0]["PrivateIpAddress"] = "10.4.0.3"
            return data

    class Proof:
        def send_command(self, **kwargs):
            shell = kwargs["Parameters"]["commands"][0]
            assert subprocess.run(["bash", "-n", "-c", shell], capture_output=True).returncode == 0
            assert shell.startswith("WS=/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol09b67859b1a446c98;")
            # the profile readback names crew-host-bootstrap.js among its required files, so order
            # against the bootstrap *invocation* guard rather than the file name
            assert shell.index("mount -t xfs") < shell.index("crew-profile-ok") < shell.index("test -e /run/adp/crew-ready ||")
            assert "crew-host-bootstrap.js" in shell and "test -e /run/adp/crew-ready ||" in shell
            scope = json.loads(shlex.split(shell)[-1])
            assert scope["run_id_sha256"] == hashlib.sha256(run_id.encode()).hexdigest()
            assert scope["restore_manifest_version"] == 0
            assert scope["checkpoint_id"] is None and scope["manifest_sha256"] is None
            assert "verified-one" not in shell and "b" * 64 not in shell
            return {"Command": {"CommandId": "fresh-readback"}}

        def get_command_invocation(self, **kwargs):
            assert kwargs == {"CommandId": "fresh-readback", "InstanceId": "i-0123456789abcdef0"}
            return {"Status": "Success" if proof else "Failed", "StandardOutputContent": proof}

    template = LaunchTemplate(
        "lt-example",
        "4",
        "us-east-1",
        image_id="ami-approved",
        revision="r1",
        profile_revision="crew-r1",
        source_revision="a" * 40,
        gateway_execute_arn="arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev",
        crew_model_id="us.anthropic.example-v1:0",
    )
    verified = bool(proof)
    assert (
        await reconcile_host_readiness(
            factory, Owned(image_id="ami-approved"), Proof(), run_store, await _host_instance_id(factory), launch_template=template
        )
        is verified
    )
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        instance = await db.scalar(select(DevBoxInstance))
        executor = await db.scalar(select(CrewExecutor).where(CrewExecutor.user_id == "alice"))
        assert (box.auth_readiness == "ready") is verified
        assert (instance.readiness_class == "crew_ready") is verified
        assert executor.latest_verified_checkpoint_id is None


@pytest.mark.asyncio
async def test_first_host_context_is_fresh_and_cannot_take_a_restore_grant(pending_host, monkeypatch):
    """With no verified checkpoint the context route issues a fresh scope (#8171); the grant route stays closed."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from starlette.responses import JSONResponse

    from src.crew import checkpoint_routes as routes
    from src.crew.checkpoint_store import CrewCheckpointStore

    factory, run_store, run_id = pending_host
    async with factory.begin() as db:
        executor = await db.scalar(select(CrewExecutor).where(CrewExecutor.user_id == "alice"))
        executor.latest_verified_checkpoint_id = None
    monkeypatch.setattr(routes, "current_crew_store", lambda: run_store)
    monkeypatch.setattr(routes, "gateway_client_identity", lambda: SimpleNamespace(certificate="public-cert", fingerprint="f" * 64))

    async def public_keys():
        return JSONResponse({"keys": {"test": "public-key"}})

    monkeypatch.setattr(routes, "model_policy_keys", public_keys)
    monkeypatch.setattr(routes, "verified_caller_identity", lambda _request: "arn:aws:sts::000000000000:assumed-role/guest/i-0123456789abcdef0")
    identity = worker_context()
    identity.requires_host_identity = True
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.enabled] = lambda: None
    app.dependency_overrides[routes.get_store] = lambda: CrewCheckpointStore(factory)
    app.dependency_overrides[routes.get_token_context] = lambda: identity
    app.dependency_overrides[routes.get_objects] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        fresh = await client.post("/crew/host-restore/context")
        assert fresh.status_code == 200, fresh.text
        body = fresh.json()
        assert body["run_id"] == run_id
        assert body["checkpoint_id"] is None and body["manifest_sha256"] is None and body["restore_manifest_version"] == 0
        assert "verified-one" not in fresh.text
        # Nothing to pin: a grant for the owner's stale checkpoint row, or for no checkpoint, is refused.
        stale = await client.post("/crew/host-restore/grant", headers={"X-Agent-RunId": run_id}, json={"checkpoint_id": "verified-one"})
        assert stale.status_code == 409
        empty = await client.post("/crew/host-restore/grant", headers={"X-Agent-RunId": run_id}, json={"checkpoint_id": None})
        assert empty.status_code == 422


@pytest.mark.asyncio
async def test_fence_host_fences_a_pending_box_with_no_live_session(database):
    """A stalled enrollment leaves the box ``pending`` after its lease expires; fencing must retract it."""
    from src.cloud_desktop.host_fencing import fence_host

    async with database.begin() as db:
        db.add(
            DevBox(
                id="pending-box",
                user_id="alice",
                org_id="one",
                deployment_id="test-deployment",
                template_id="t1",
                template_revision="r1",
                profile="crew",
                machine_generation=1,
                status="running",
                desired_state="running",
                observed_state="running",
                auth_readiness="pending",
            )
        )
    async with database.begin() as db:
        box = await db.get(DevBox, "pending-box")
        await fence_host(db, box)
    async with database() as db:
        assert (await db.get(DevBox, "pending-box")).auth_readiness == "fenced"


@pytest.mark.asyncio
async def test_controller_stop_fence_unblocks_a_pending_box(pending_host):
    """The controller's serve-built fence lets reconcile_state stop a pending box the scheduler never pre-fenced."""
    import functools

    from controller import worker
    from controller.fencing import require_access_fenced

    factory, run_store, run_id = pending_host
    async with factory.begin() as db:
        session = await db.scalar(select(DevBoxAccessSession))
        session.terminated_at = datetime.now(UTC)
    fence = functools.partial(worker._host_access_fence, run_store)
    # The fence must never take a session factory: it would open a second session and
    # lock the box row its caller already holds (dev 2026-10-19 controller hang).
    assert "session_factory" not in inspect.signature(worker._host_access_fence).parameters
    async with factory() as db:
        box = await db.scalar(select(DevBox).where(DevBox.id == "host-box").with_for_update())
        assert box.auth_readiness == "pending"
        assert await require_access_fenced(box, "stop", fence, db=db) is None
        assert box.auth_readiness == "fenced", "fenced in place, on the caller's locked row"
        await db.commit()
    async with factory() as db:
        assert (await db.get(DevBox, "host-box")).auth_readiness == "fenced"
    # No fence callback (host registry absent) still refuses, so a box is never stopped unfenced.
    async with factory.begin() as db:
        (await db.get(DevBox, "host-box")).auth_readiness = "pending"
    async with factory() as db:
        box = await db.get(DevBox, "host-box")
        assert await require_access_fenced(box, "stop", None, db=db) == "identity_fence_required"
        # A fence without the caller's session is refused too: it could only re-lock the row.
        assert await require_access_fenced(box, "stop", fence) == "identity_fence_required"
