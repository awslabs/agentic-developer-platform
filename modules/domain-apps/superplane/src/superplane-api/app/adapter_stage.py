"""Read-only verification in the installed API container, without its lifespan."""

import asyncio
import os

import botocore.config
import botocore.session

from app.config import settings
from app.management import management_only
from app.operation_activation import dispatch_enabled


async def verify_stage(expected, *, active=False):
    from app.composition import compose
    from app.installation import capability_details, capabilities_from, database_check

    if management_only() is active or dispatch_enabled() is not active:
        raise ValueError("adapter verification mode differs from expected stage")
    producer, vault = expected["dispatcher"], expected["vault"]
    if (
        settings.superplane_operation_gateway_url != producer["endpoint"]
        or settings.superplane_operation_gateway_region != producer["region"]
        or settings.adp_gateway_internal_url != vault["url"]
        or os.environ.get("AWS_ROLE_ARN") != producer["role_arn"]
        or os.environ.get("AWS_EC2_METADATA_DISABLED") != "true"
        or os.environ.get("AWS_STS_REGIONAL_ENDPOINTS") != "regional"
        or not os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE")
        or any(
            os.environ.get(k)
            for k in (
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "AWS_PROFILE",
                "AWS_SHARED_CREDENTIALS_FILE",
                "AWS_CONFIG_FILE",
                "AWS_ENDPOINT_URL",
                "AWS_ENDPOINT_URL_STS",
                "AWS_CA_BUNDLE",
                "AWS_CONTAINER_CREDENTIALS_FULL_URI",
                "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
            )
        )
    ):
        raise ValueError(
            "API adapter transport or workload identity differs from stage"
        )
    source = os.environ.get("SUPERPLANE_SOURCE_REVISION")
    release = os.environ.get("SUPERPLANE_RELEASE_ID")
    if source != expected["source_revision"] or release != expected["release_id"]:
        raise ValueError("API source or release differs from stage")
    database = await database_check()
    if (
        database["revision"] != expected["schema_head"]
        or database["schema"] != expected["schema"]
    ):
        raise ValueError("API database differs from stage")

    composition = compose()
    await composition.aopen()
    try:
        ports = capabilities_from(await capability_details())
        if len(ports) != 4 or not all(ports.values()):
            raise ValueError("actual composed API port verification failed")
        if composition.dispatcher is None or not await composition.dispatcher.ready(
            expected["org_id"]
        ):
            raise ValueError("actual tenant producer readiness failed")

        def caller_identity():
            session = botocore.session.get_session()
            credentials = session.get_credentials()
            if (
                credentials is None
                or credentials.method != "assume-role-with-web-identity"
            ):
                raise ValueError("API is not using its workload web identity")
            client = session.create_client(
                "sts",
                region_name=producer["region"],
                config=botocore.config.Config(
                    connect_timeout=3,
                    read_timeout=5,
                    retries={"max_attempts": 0},
                    proxies={},
                ),
            )
            try:
                return client.get_caller_identity()
            finally:
                client.close()

        identity = await asyncio.to_thread(caller_identity)
        role = producer["role_arn"].rsplit("/", 1)[1]
        prefix = f"arn:aws:sts::{expected['account_id']}:assumed-role/{role}/"
        if identity.get("Account") != expected["account_id"] or not identity.get(
            "Arn", ""
        ).startswith(prefix):
            raise ValueError(
                "observed API workload identity differs from selected role"
            )
        return {
            "stage_version": 1,
            "management_only": not active,
            "dispatch_enabled": active,
            "paid_admission_enabled": active,
            "release_id": release,
            "source_revision": source,
            "database": database,
            "capabilities": ports,
            "producer_ready": True,
            "role_arn": producer["role_arn"],
            "observed_role_arn": identity["Arn"],
            "org_id": expected["org_id"],
            "vault_url": vault["url"],
            "producer_endpoint": producer["endpoint"],
            "configuration_verified": True,
            "production_ready": False,
            "credential_positive_control_verified": False,
        }
    finally:
        await composition.aclose()
