"""Renewable in-memory paid provider credentials; ambient IAM is transport only."""

import asyncio
from datetime import UTC, datetime, timedelta
import re

import boto3
import botocore.session
from botocore.config import Config
from botocore.credentials import RefreshableCredentials
from harness_jobs.identity import (
    OperationRefused,
    admitted_credential_reference,
    admitted_credential_target,
)


async def session_for(
    authority, operation, region, *, verify=None, access_entry_arn=None
):
    loop = asyncio.get_running_loop()
    operation_id = operation.grant.lease.operation_id
    credential_id, service, _ = admitted_credential_reference(
        operation.request_payload, operation.plan_digest
    )
    provider, account = admitted_credential_target(
        operation.request_payload, operation.plan_digest
    )
    if provider != "aws" or service != "aws":
        raise OperationRefused("paid provider session requires AWS")
    pinned = None
    authority_deadline = None

    async def fetch():
        if verify is not None:
            await verify()
        return await authority.post(
            "/internal/v1/controller-execution/provider-session",
            {
                "operation_id": operation_id,
                "region": region,
                "access_entry_arn": access_entry_arn,
            },
        )

    def refresh():
        nonlocal pinned, authority_deadline
        future = asyncio.run_coroutine_threadsafe(fetch(), loop)
        try:
            response = future.result(timeout=60)
        except BaseException:
            future.cancel()
            raise OperationRefused("paid provider session unavailable") from None
        try:
            role = response["role_arn"]
            authority_deadline = datetime.fromisoformat(
                response["authority_expires_at"]
            )
            if response.get("access_entry_arn") != access_entry_arn:
                raise ValueError("session scope differs")
            expiry = datetime.fromisoformat(response["expiration"])
            role_match = re.fullmatch(
                r"arn:aws:iam::" + re.escape(account) + r":role/([A-Za-z0-9+=,.@_/-]+)",
                role,
            )
            if (
                response["version"] != 1
                or response["operation_id"] != operation_id
                or response["credential_id"] != credential_id
                or response["account_id"] != account
                or response["region"] != region
                or not role_match
                or expiry.tzinfo is None
                or authority_deadline.tzinfo is None
                or expiry > authority_deadline
                or not datetime.now(UTC)
                < expiry
                <= datetime.now(UTC) + timedelta(seconds=900)
            ):
                raise ValueError("session target differs")
            selected = (role, response["assumed_role_id"].split(":", 1)[0])
            if pinned is not None and pinned != selected:
                raise ValueError("provider role changed")
            static = boto3.Session(
                aws_access_key_id=response["access_key_id"],
                aws_secret_access_key=response["secret_access_key"],
                aws_session_token=response["session_token"],
                region_name=region,
            )
            identity = static.client(
                "sts",
                config=Config(
                    connect_timeout=5,
                    read_timeout=15,
                    retries={"total_max_attempts": 1},
                ),
            ).get_caller_identity()
            if (
                identity.get("Account") != account
                or identity.get("Arn") != response["assumed_role_arn"]
                or identity.get("UserId") != response["assumed_role_id"]
                or not re.fullmatch(r"AROA[A-Z0-9]{17}", selected[1])
                or not identity["Arn"].startswith(
                    f"arn:aws:sts::{account}:assumed-role/{role.rsplit('/', 1)[1]}/"
                )
            ):
                raise ValueError("AWS session identity differs")
            pinned = selected
            return {
                "access_key": response["access_key_id"],
                "secret_key": response["secret_access_key"],
                "token": response["session_token"],
                "expiry_time": response["expiration"],
            }
        except (KeyError, ValueError, TypeError):
            raise OperationRefused("paid provider session identity refused") from None

    def create():
        selected = botocore.session.get_session()
        selected._credentials = RefreshableCredentials.create_from_metadata(
            metadata=refresh(),
            refresh_using=refresh,
            method="superplane-paid-provider",
            advisory_timeout=120,
            mandatory_timeout=30,
        )
        selected.set_config_variable("region", region)
        selected.set_default_client_config(
            Config(
                connect_timeout=5, read_timeout=15, retries={"total_max_attempts": 1}
            )
        )
        session = boto3.Session(botocore_session=selected)
        session._superplane_role_arn = pinned[0]
        session._superplane_source_session = session
        session._superplane_external_id = None
        session._superplane_authority_deadline = lambda: authority_deadline

        def scoped_entry(entry):
            pending = asyncio.run_coroutine_threadsafe(
                session_for(
                    authority, operation, region, verify=verify, access_entry_arn=entry
                ),
                loop,
            )
            try:
                scoped = pending.result(timeout=90)
            except BaseException:
                pending.cancel()
                raise OperationRefused("scoped provider session unavailable") from None
            if scoped._superplane_role_arn != session._superplane_role_arn:
                raise OperationRefused("scoped provider role changed")
            return scoped

        session._superplane_scoped_entry = scoped_entry
        return session

    return await asyncio.to_thread(create)
