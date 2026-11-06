"""Real owner/source/run authority and signed durable evidence; provider I/O only is replaced."""

import base64
import io
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha1, sha256
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa, utils
from fastapi import HTTPException
from l5_services.adapters.devops.release_verification import ReleaseVerificationAdapter
from l5_services.models.run import Run
from sqlalchemy import delete

from src.auth.aws_connection_authority import connection_binding
from src.l5_devops_queue_fixture import EXPECTED, SCENARIO, QueueFixture, build_queue_fixture
from src.l5_devops_queue_observer import observe_queue, worker_policy, worker_zip
from src.l5_devops_verification import VerificationReader
from src.l5_integration import GatewayConfigurationBackend
from src.products.models import ProductSourceBinding
from src.shared.models.organization import TeamMembership
from src.shared.models.vault import CredentialServiceDelegation
from tests.products.test_l5_devops_owned_backend import consent_fixture, devops_backend, l5_owner
from tests.products.test_l5_owner_delegation import request_grant

__all__ = ["consent_fixture", "devops_backend", "l5_owner"]
REAL_AUTHORIZE = GatewayConfigurationBackend.authorize_settings
SOURCE = (Path(__file__).resolve().parents[4] / "docs/l5-services/devopsagnent/fixtures/queue/index.mjs").read_bytes()


class AWS:
    def __init__(self, selection):
        self.selection = selection
        self.objects, self.calls, self.deleted, self.invokes = {}, [], [], []
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.lose_delete = self.lose_invoke = False
        self.clients = {name: Mock() for name in ("lambda", "iam", "sqs", "ec2", "sts")}
        self.clients["sts"].get_caller_identity.return_value = {"Account": selection["account_id"]}
        self.function = {
            "FunctionArn": selection["function_version_arn"],
            "Version": "1",
            "Runtime": "nodejs22.x",
            "Handler": "index.handler",
            "Role": selection["execution_role_arn"],
            "CodeSha256": base64.b64encode(sha256(worker_zip(SOURCE)).digest()).decode(),
            "PackageType": "Zip",
            "State": "Active",
            "Timeout": 30,
            "MemorySize": 128,
            "VpcConfig": {"SubnetIds": [selection["subnet_id"]], "SecurityGroupIds": [selection["security_group_id"]], "VpcId": selection["vpc_id"]},
        }
        self.clients["lambda"].get_function.return_value = {"Configuration": self.function}
        self.clients["lambda"].get_function_concurrency.return_value = {"ReservedConcurrentExecutions": 2}
        self.clients["lambda"].get_event_source_mapping.return_value = {
            "UUID": selection["mapping_id"],
            "FunctionArn": selection["function_version_arn"],
            "EventSourceArn": selection["queue_arn"],
            "State": "Enabled",
            "BatchSize": 5,
            "FunctionResponseTypes": ["ReportBatchItemFailures"],
        }
        self.clients["lambda"].invoke.side_effect = self.invoke
        self.clients["sqs"].get_queue_attributes.return_value = {
            "Attributes": {"QueueArn": selection["queue_arn"], "SqsManagedSseEnabled": "true", "VisibilityTimeout": "180"}
        }
        self.group = {
            "GroupId": selection["security_group_id"],
            "VpcId": selection["vpc_id"],
            "OwnerId": selection["account_id"],
            "IpPermissions": [],
            "IpPermissionsEgress": [],
        }
        self.clients["ec2"].describe_security_groups.return_value = {"SecurityGroups": [self.group]}
        self.clients["iam"].get_role.return_value = {
            "Role": {
                "Arn": selection["execution_role_arn"],
                "RoleId": "fixture-role",
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}],
                },
            }
        }
        self.clients["iam"].list_role_policies.return_value = {"PolicyNames": ["scoped-queue-read"]}
        self.clients["iam"].list_attached_role_policies.return_value = {"AttachedPolicies": []}
        self.clients["iam"].get_role_policy.return_value = {"PolicyDocument": worker_policy(selection["queue_arn"])}

    def client(self, name, **kwargs):
        return self if name in {"s3", "kms"} else self.clients[name]

    def sign(self, **args):
        assert args["MessageType"] == "DIGEST" and args["SigningAlgorithm"] == "RSASSA_PSS_SHA_256"
        signature = self.key.sign(args["Message"], padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), utils.Prehashed(hashes.SHA256()))
        return {"KeyId": args["KeyId"], "Signature": signature}

    def verify(self, **args):
        self.key.public_key().verify(
            args["Signature"], args["Message"], padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), utils.Prehashed(hashes.SHA256())
        )
        return {"KeyId": args["KeyId"], "SignatureValid": True}

    def put_object(self, **args):
        assert args["ExpectedBucketOwner"] == self.selection["account_id"] and args["IfNoneMatch"] == "*"
        if args["Key"] in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        version = str(uuid4())
        self.objects[args["Key"]] = (version, args["Body"], args["Metadata"])
        self.calls.append(("put", args["Key"], version))
        return {"VersionId": version}

    def get_object(self, **args):
        assert args["ExpectedBucketOwner"] == self.selection["account_id"]
        found = self.objects.get(args["Key"])
        if found is None or args.get("VersionId", found[0]) != found[0]:
            raise ClientError({"Error": {"Code": "NoSuchVersion" if "VersionId" in args else "NoSuchKey"}}, "GetObject")
        return {"VersionId": found[0], "Body": io.BytesIO(found[1]), "Metadata": found[2]}

    def delete_object(self, **args):
        assert args["ExpectedBucketOwner"] == self.selection["account_id"]
        assert self.objects[args["Key"]][0] == args["VersionId"]
        self.deleted.append(args)
        del self.objects[args["Key"]]
        if self.lose_delete:
            self.lose_delete = False
            raise TimeoutError("synthetic lost delete acknowledgement")
        return {"VersionId": args["VersionId"]}

    def invoke(self, **args):
        assert args["FunctionName"] == self.selection["function_version_arn"] and args["InvocationType"] == "RequestResponse"
        self.invokes.append(args)
        if self.lose_invoke:
            self.lose_invoke = False
            raise TimeoutError("synthetic lost invocation acknowledgement")
        payload = json.loads(args["Payload"])
        kind = next(key for key in EXPECTED if SCENARIO[key] == payload)
        return {
            "StatusCode": 200,
            "ExecutedVersion": "1",
            "ResponseMetadata": {"RequestId": str(uuid4())},
            "Payload": io.BytesIO(json.dumps(EXPECTED[kind]).encode()),
        }


@pytest.fixture
async def selected(devops_backend, test_client, db_session, monkeypatch):
    fixture = devops_backend
    monkeypatch.setattr(GatewayConfigurationBackend, "authorize_settings", REAL_AUTHORIZE)
    fixture.source_authority.reset_mock()
    for module in ("src.products.source_routes", "src.product_onboarding.source_discovery"):
        monkeypatch.setattr(module + ".assert_installation_owned_by", AsyncMock())
    fixture.credential.agent_consent = True
    fixture.credential.scopes = {**fixture.credential.scopes, "source": "imported_role"}
    fixture.credential.aws_verified_binding = connection_binding(fixture.credential)
    fixture.version.settings = {**fixture.version.settings, "source_refs": [fixture.head], "capabilities": ["release_verification"]}
    db_session.add(
        ProductSourceBinding(
            tenant_id=fixture.credential.org_id,
            project_id=fixture.project_id,
            kind="environment",
            provider="aws",
            credential_id=fixture.credential.id,
            namespace="fixture",
            native_id="123456789012",
            desired={},
            status="healthy",
        )
    )
    await db_session.delete(fixture.parent)
    await db_session.commit()
    response, *_ = await request_grant(test_client, fixture.path + "/devops", key="queue_fixture_consent_01")
    assert response.status_code == 200, response.text
    fixture.parent = await db_session.get(CredentialServiceDelegation, response.json()["delegation_id"])
    region, account = "us-east-1", "123456789012"
    selection = {
        "account_id": account,
        "region": region,
        "function_version_arn": f"arn:aws:lambda:{region}:{account}:function:queue-fixture:1",
        "execution_role_arn": f"arn:aws:iam::{account}:role/QueueWorker",
        "queue_arn": f"arn:aws:sqs:{region}:{account}:queue-fixture",
        "queue_url": f"https://sqs.{region}.amazonaws.com/{account}/queue-fixture",
        "mapping_id": str(uuid4()),
        "vpc_id": "vpc-aabb",
        "subnet_id": "subnet-aabb",
        "security_group_id": "sg-aabb",
        "bucket": "fixture-evidence",
        "prefix": "l5/devops/queue-fixture/" + "a" * 64 + "/",
        "signing_key_arn": f"arn:aws:kms:{region}:{account}:key/{uuid4()}",
        "producer": "queue-fixture-v1",
        "source_ref": fixture.head,
    }
    for name, kind, identifier in (
        ("deployment_ref", "deployment", selection["function_version_arn"]),
        ("target_ref", "verification_target", selection["security_group_id"]),
        ("credential_ref", "secret", selection["execution_role_arn"]),
    ):
        selection[name] = {**fixture.reference, "record_type": kind, "record_id": identifier}
    monkeypatch.setenv("L5_DEVOPS_QUEUE_FIXTURE", json.dumps(selection))
    aws = AWS(selection)
    monkeypatch.setattr("boto3.Session", lambda **kwargs: aws)
    monkeypatch.setattr("boto3.client", aws.client)
    original = __import__("src.l5_devops_reads", fromlist=["GitHubAppClient"]).GitHubAppClient.installation_request

    async def github(self, installation, method, path):
        if "/contents/index.mjs?ref=" in path:
            return {
                "type": "file",
                "encoding": "base64",
                "path": "index.mjs",
                "size": len(SOURCE),
                "content": base64.b64encode(SOURCE).decode(),
                "sha": sha1(b"blob " + str(len(SOURCE)).encode() + b"\0" + SOURCE, usedforsecurity=False).hexdigest(),
            }
        return await original(self, installation, method, path)

    monkeypatch.setattr("src.l5_devops_reads.GitHubAppClient.installation_request", github)
    producer = build_queue_fixture(fixture.backend, fixture.product_id, fixture.project_id, session=aws)
    observation = await producer.read_deployment(selection["deployment_ref"], selection["target_ref"])
    _, _, scope, _ = await producer.current()
    run = Run(
        tenant_id=fixture.credential.org_id,
        run_id=str(uuid4()),
        product_id=fixture.product_id,
        project_id=fixture.project_id,
        service_id="devops",
        configuration_id=fixture.config.configuration_id,
        configuration_revision=1,
        binding_id=fixture.binding.binding_id,
        binding_generation=1,
        trigger_id="fixture",
        input={"capability": "release_verification", "devops_input": producer.options},
        admission={
            "scope": scope,
            "configuration_hash": fixture.version.hash,
            "delegation_id": fixture.parent.id,
            "delegation_version": fixture.parent.revision,
            "binding_generation": fixture.binding.generation,
            "native_space_ref": fixture.binding.native_resource_ref,
            "provider_identity": fixture.binding.provider_identity,
            "verification": {**observation, "scenario_specs": producer.specs()},
        },
        native_job_ref={**fixture.reference, "record_type": "native_job", "record_id": "execution-one", "record_revision": "execution-one"},
        deadline_at=datetime.now(UTC) + timedelta(hours=1),
    )
    run.manifest = {"run_id": run.run_id, "scope": scope, "verification": run.admission["verification"], "scenarios": producer.options["scenarios"]}
    db_session.add(run)
    await db_session.commit()
    fixture.queue, fixture.aws, fixture.selection, fixture.run, fixture.producer = (
        QueueFixture(fixture.backend, fixture.product_id, fixture.project_id, session=aws, run_id=run.run_id),
        aws,
        selection,
        run,
        producer,
    )
    return fixture


async def test_published_real_observations_consumed_by_existing_signed_reader(selected, monkeypatch):
    reference = await selected.producer.publish_admission()
    reader = VerificationReader(selected.backend, session=selected.aws, reference=reference)
    observed = await reader.read_deployment(selected.selection["deployment_ref"], selected.selection["target_ref"])
    assert observed["deployed_sha"] == selected.head["record_revision"]
    assert await reader.read_scenarios(selected.producer.options["scenarios"]) == selected.producer.specs()
    selected.source_authority.assert_not_awaited()
    assert selected.aws.invokes == [] and selected.aws.deleted == []


async def test_persisted_run_oracle_replay_and_exact_cleanup(selected):
    first = await selected.queue.run_oracle()
    assert first[0]["status"] == "passed" and len(selected.aws.invokes) == 2
    assert await selected.queue.run_oracle() == first and len(selected.aws.invokes) == 2
    assert await selected.queue.read_results(selected.selection["deployment_ref"], "execution-one") == first
    selected.aws.objects["foreign-unrelated-object"] = ("foreign-version", b"unrelated", {})
    result = await ReleaseVerificationAdapter(selected.queue)._cleanup(selected.run, selected.run.native_job_ref)
    assert result[0]["state"] == "absent" and len(selected.aws.deleted) == 1
    assert "foreign-unrelated-object" in selected.aws.objects
    assert await ReleaseVerificationAdapter(selected.queue)._cleanup(selected.run, selected.run.native_job_ref) == result
    assert len(selected.aws.deleted) == 1 and await selected.queue.run_oracle() == first and len(selected.aws.invokes) == 2
    assert all(args["VersionId"] and args["Key"].endswith("/payload") for args in selected.aws.deleted)


async def test_unknown_invocation_never_repeats_and_owned_data_can_be_cleaned(selected):
    selected.aws.lose_invoke = True
    with pytest.raises(TimeoutError):
        await selected.queue.run_oracle()
    with pytest.raises(ValueError, match="queue_oracle_outcome_unknown"):
        await selected.queue.run_oracle()
    assert len(selected.aws.invokes) == 1
    result = await ReleaseVerificationAdapter(selected.queue)._cleanup(selected.run, selected.run.native_job_ref)
    assert result[0]["state"] == "absent"


async def test_cleanup_recovers_lost_ack_without_second_delete(selected):
    await selected.queue.run_oracle()
    selected.aws.lose_delete = True
    _, owner, _, token = await selected.queue.identity()
    with pytest.raises(TimeoutError):
        await selected.queue.delete_fixture_data(owner["fixture_ref"], owner_run_id=owner["owner_run_id"], idempotency_key=token)
    await selected.queue.delete_fixture_data(owner["fixture_ref"], owner_run_id=owner["owner_run_id"], idempotency_key=token)
    assert len(selected.aws.deleted) == 1


@pytest.mark.parametrize("change", ["wrong-version", "function-error", "wrong-result", "missing-request-id"])
async def test_independent_oracle_preserves_provider_failure(selected, change):
    def invoke(**args):
        result = selected.aws.invoke(**args)
        if change == "wrong-version":
            result["ExecutedVersion"] = "2"
        elif change == "function-error":
            result["FunctionError"] = "Unhandled"
        elif change == "wrong-result":
            result["Payload"] = io.BytesIO(b'{"batchItemFailures":[{"itemIdentifier":"unexpected"}]}')
        else:
            result["ResponseMetadata"] = {}
        return result

    selected.aws.clients["lambda"].invoke.side_effect = invoke
    rows = await selected.queue.run_oracle()
    assert rows[0]["status"] == "failed"
    assert rows[0]["positive"]["observed_outcome"] == "unexpected_result"
    assert rows[0]["negative"]["observed_outcome"] == "unexpected_result"
    assert len(selected.aws.invokes) == 2


async def test_cleanup_rejects_foreign_identity_and_ambiguous_absence(selected):
    await selected.queue.run_oracle()
    _, owner, _, token = await selected.queue.identity()
    with pytest.raises(ValueError):
        await selected.queue.delete_fixture_data(owner["fixture_ref"], owner_run_id="other-run", idempotency_key=token)
    assert selected.aws.deleted == []
    original = selected.aws.get_object

    def denied(**args):
        if args["Key"].endswith("/payload"):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
        return original(**args)

    selected.aws.get_object = denied
    with pytest.raises(ClientError):
        await selected.queue.read_fixture_data(owner["fixture_ref"])
    assert selected.aws.deleted == []


async def test_read_recorded_response_after_interrupted_result_write_does_not_reinvoke(selected):
    original = selected.queue.evidence.create
    interrupt = True

    def create(name, data):
        nonlocal interrupt
        if name.endswith("/positive/result") and interrupt:
            interrupt = False
            raise TimeoutError("synthetic reporting interruption")
        return original(name, data)

    selected.queue.evidence.create = create
    with pytest.raises(TimeoutError):
        await selected.queue.run_oracle()
    assert len(selected.aws.invokes) == 1
    result = await selected.queue.run_oracle()
    assert result[0]["status"] == "passed" and len(selected.aws.invokes) == 2


async def test_mutable_isolation_rechecked_before_each_invocation(selected):
    def invoke(**args):
        response = selected.aws.invoke(**args)
        selected.aws.group["IpPermissionsEgress"] = [{"IpProtocol": "-1"}]
        return response

    selected.aws.clients["lambda"].invoke.side_effect = invoke
    with pytest.raises(ValueError, match="deny network"):
        await selected.queue.run_oracle()
    assert len(selected.aws.invokes) == 1


@pytest.mark.parametrize("state", ["cancel_requested", "cancelled", "expired", "blocked", "failed", "succeeded"])
async def test_stopped_run_cannot_start_new_oracle_work(selected, db_session, state):
    selected.run.state = state
    await db_session.commit()
    with pytest.raises(ValueError, match="no longer admits"):
        await selected.queue.run_oracle()
    assert selected.aws.invokes == [] and selected.aws.deleted == []


@pytest.mark.parametrize("change", ["source", "owner", "membership", "expired", "revoked", "run-source", "execution", "configuration", "binding"])
async def test_real_authority_changes_deny_before_invocation(selected, db_session, change):
    if change == "source":
        selected.source.status = "revoked"
    elif change == "owner":
        selected.config.owner_id = "different-owner"
    elif change == "membership":
        await db_session.execute(delete(TeamMembership))
    elif change == "expired":
        selected.parent.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "revoked":
        selected.parent.revoked_at = datetime.now(UTC)
    elif change == "run-source":
        selected.run.input = {**selected.run.input, "devops_input": {**selected.queue.options, "source_ref": selected.base}}
    elif change == "execution":
        selected.run.native_job_ref = None
    elif change == "configuration":
        selected.version.hash = "c" * 64
    else:
        selected.binding.generation += 1
    await db_session.commit()
    with pytest.raises((HTTPException, ValueError)):
        await selected.queue.run_oracle()
    assert selected.aws.invokes == [] and selected.aws.deleted == []


@pytest.mark.parametrize("change", ["code", "mutable-version", "egress", "admin", "managed", "other-queue", "other-mapping"])
async def test_real_provider_observations_reject_unsafe_target(selected, change):
    if change == "code":
        selected.aws.function["CodeSha256"] = "other-code"
    elif change == "mutable-version":
        selected.selection["function_version_arn"] = selected.selection["function_version_arn"].rsplit(":", 1)[0] + ":$LATEST"
    elif change == "egress":
        selected.aws.group["IpPermissionsEgress"] = [{"IpProtocol": "-1"}]
    elif change == "admin":
        selected.aws.clients["iam"].get_role_policy.return_value = {
            "PolicyDocument": {"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
        }
    elif change == "managed":
        selected.aws.clients["iam"].list_attached_role_policies.return_value = {"AttachedPolicies": [{"PolicyArn": "admin"}]}
    elif change == "other-queue":
        selected.aws.clients["sqs"].get_queue_attributes.return_value["Attributes"]["QueueArn"] = "other"
    else:
        selected.aws.clients["lambda"].get_event_source_mapping.return_value["FunctionArn"] = "other"
    with pytest.raises(ValueError):
        observe_queue(selected.aws, selected.selection, SOURCE)
    assert selected.aws.invokes == [] and selected.aws.deleted == []
