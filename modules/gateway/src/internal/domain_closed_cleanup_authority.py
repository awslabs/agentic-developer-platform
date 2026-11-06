"""Fresh authority for one sealed service command, separate from paid delivery."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import boto3
from botocore.config import Config

from src.internal import domain_provider_authority as providers
from src.internal.domain_closed_cleanup_source import native_paths, source_digest
from src.internal.domain_operation_store import aws_client
from src.internal.domain_provider_session import verify_session
from src.internal.sts_assume_service import assume_role
from src.shared.domain_provider_contract import digest

CONFIG = Config(connect_timeout=5, read_timeout=15, retries={"total_max_attempts": 1})


def bounded_policy(value):
    document = json.dumps(value, sort_keys=True, separators=(",", ":"))
    providers.require(len(document) <= 2048)
    return document


def read_permit(permit_id):
    native_paths()
    from superplane_bootstrap.closed_cleanup_permit import PREFIX, decode_document, validate

    providers.require(isinstance(permit_id, str) and permit_id.startswith(PREFIX) and len(permit_id) == len(PREFIX) + 64)
    table, _ = providers.tables()
    row = aws_client("dynamodb").get_item(TableName=table, Key={"record_id": {"S": permit_id}}, ConsistentRead=True).get("Item")
    providers.require(row is not None and set(row) == {"record_id", "document", "revision"})
    value = validate(decode_document(row["document"]["S"]))
    providers.require(value["permit_id"] == permit_id and row["revision"]["S"] == digest(value))
    providers.require(value["service_source_sha256"] == source_digest())
    return value


class ServiceAuthority:
    def __init__(self, permit, db, sm, boundary):
        self.permit, self.db, self.sm, self.boundary = permit, db, sm, boundary
        self.baseline = permit["authority"]
        self.deadline = datetime.fromisoformat(permit["expires_at"])
        self.sessions = {}

    def baseline_reader(self, credential_id):
        providers.require(read_permit(self.permit["permit_id"]) == self.permit)
        table, _ = providers.tables()
        row = aws_client("dynamodb").get_item(TableName=table, Key={"record_id": {"S": credential_id}}, ConsistentRead=True).get("Item")
        providers.require(row is not None and set(row) == {"record_id", "document", "revision"})
        providers.require(credential_id == self.baseline["credential_id"] and json.loads(row["document"]["S"]) == self.baseline)
        providers.require(row["revision"]["S"] == digest(self.baseline) and self.baseline["status"] == "active")
        # The expired enrollment supplies identity, not authorization. Only the
        # current owner permit grants this fixed cleanup operation.
        return self.baseline.copy()

    async def revalidate(self):
        await self.boundary()
        record = await providers._resolve(
            self.db,
            self.sm,
            self.baseline["credential_id"],
            read_authority=self.baseline_reader,
            subject=self.baseline["subject"],
            org_id=self.baseline["org_id"],
            workspace_id=self.baseline["workspace_id"],
        )
        providers.require(record == self.baseline and datetime.now(UTC) < self.deadline)
        await self.boundary()

    async def record_session(self, *, role_arn, role_id, expiry, policy):
        from superplane_bootstrap.closed_cleanup_journal import CleanupJournal

        def write():
            table, evidence = providers.tables()

            def current():
                providers.require(read_permit(self.permit["permit_id"]) == self.permit)

            journal = CleanupJournal(aws_client("dynamodb"), table, evidence, self.permit, current)
            journal.key += "/sessions"
            journal.append(
                journal.read(), phase="session-issued", role_arn=role_arn, role_id=role_id, expires_at=expiry.isoformat(), scope_sha256=digest(policy)
            )

        await asyncio.to_thread(write)

    async def provider(self, policy):
        await self.revalidate()
        key = "provider/" + digest(policy)
        cached = self.sessions.get(key)
        if cached and datetime.now(UTC) + timedelta(seconds=120) < cached[1] <= self.deadline:
            return cached[0]
        providers.require(datetime.now(UTC) + timedelta(seconds=930) < self.deadline)
        material = await providers.material(self.sm, self.baseline)
        await self.revalidate()
        issued = datetime.now(UTC)
        result = await asyncio.to_thread(
            assume_role,
            role_arn=self.baseline["role_arn"],
            external_id=material["external_id"],
            session_duration_seconds=900,
            default_region=self.baseline["region"],
            user_id=self.baseline["user_id"],
            agent_id="superplane-operation",
            task_id=self.permit["operation"]["operation_id"],
            label=self.baseline["label"],
            aws_region=self.baseline["region"],
            session_policy=bounded_policy(policy),
        )
        expiry = verify_session(result, self.baseline["role_arn"], self.deadline, issued_at=issued)
        providers.require(result.assumed_role_id.split(":", 1)[0] == self.baseline["role_id"])
        await self.record_session(role_arn=self.baseline["role_arn"], role_id=self.baseline["role_id"], expiry=expiry, policy=policy)
        await self.revalidate()
        session = boto3.Session(
            aws_access_key_id=result.access_key_id,
            aws_secret_access_key=result.secret_access_key,
            aws_session_token=result.session_token,
            region_name=self.baseline["region"],
        )
        self.sessions[key] = (session, expiry)
        return session

    async def registrar(self, policy):
        await self.revalidate()
        key = "registrar/" + digest(policy)
        cached = self.sessions.get(key)
        if cached and datetime.now(UTC) + timedelta(seconds=120) < cached[1] <= self.deadline:
            return cached[0]
        arn, role_id = self.permit["registrar"]["role_arn"], self.permit["registrar"]["role_id"]
        parent = await self.provider(
            {
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Action": ["sts:AssumeRole", "iam:GetRole"], "Resource": arn}],
            }
        )
        current = await asyncio.to_thread(parent.client("iam", config=CONFIG).get_role, RoleName=arn.rsplit("/", 1)[1])
        providers.require((current["Role"]["Arn"], current["Role"]["RoleId"]) == (arn, role_id))
        await self.revalidate()
        providers.require(datetime.now(UTC) + timedelta(seconds=930) < self.deadline)
        name = "sp-cleanup-" + self.permit["attempt_id"]
        response = await asyncio.to_thread(
            parent.client("sts", config=CONFIG).assume_role, RoleArn=arn, RoleSessionName=name, DurationSeconds=900, Policy=bounded_policy(policy)
        )
        credentials, assumed = response["Credentials"], response["AssumedRoleUser"]
        providers.require(assumed["AssumedRoleId"] == role_id + ":" + name)
        providers.require(assumed["Arn"] == arn.replace(":iam:", ":sts:").replace(":role/", ":assumed-role/") + "/" + name)
        providers.require(datetime.now(UTC) < credentials["Expiration"] <= self.deadline)
        await self.record_session(role_arn=arn, role_id=role_id, expiry=credentials["Expiration"], policy=policy)
        await self.revalidate()
        session = boto3.Session(
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
            region_name=self.baseline["region"],
        )
        self.sessions[key] = (session, credentials["Expiration"])
        return session
