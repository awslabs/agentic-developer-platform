"""Private renewable AWS sessions, always scoped to a freshly verified operation."""

from .runtime_config import LifecycleRefused


def assume_session(source, *, role_arn, region, verify, external_id=None, policy=None):
    import boto3
    import botocore.session
    from botocore.config import Config
    from botocore.credentials import RefreshableCredentials

    parts = role_arn.split(":", 5)
    if (
        len(parts) != 6
        or parts[:3] != ["arn", "aws", "iam"]
        or not parts[5].startswith("role/")
    ):
        raise LifecycleRefused("lifecycle requires an exact AWS IAM role")
    sts = source.client(
        "sts",
        region_name=region,
        config=Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 0}),
    )

    def refresh():
        verify()
        arguments = {
            "RoleArn": role_arn,
            "RoleSessionName": "superplane-lifecycle",
            "DurationSeconds": 900,
        }
        if external_id:
            arguments["ExternalId"] = external_id
        if policy:
            arguments["Policy"] = policy
        credentials = sts.assume_role(**arguments)["Credentials"]
        verify()
        return {
            "access_key": credentials["AccessKeyId"],
            "secret_key": credentials["SecretAccessKey"],
            "token": credentials["SessionToken"],
            "expiry_time": credentials["Expiration"].isoformat(),
        }

    selected = botocore.session.get_session()
    selected._credentials = RefreshableCredentials.create_from_metadata(
        metadata=refresh(),
        refresh_using=refresh,
        method="superplane-operation-role",
        advisory_timeout=120,
        mandatory_timeout=30,
    )
    selected.set_config_variable("region", region)
    selected.set_default_client_config(
        Config(connect_timeout=5, read_timeout=15, retries={"total_max_attempts": 1})
    )
    session = boto3.Session(botocore_session=selected)
    identity = session.client(
        "sts",
        region_name=region,
        config=Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 0}),
    ).get_caller_identity()
    verify()
    if identity["Account"] != parts[4]:
        raise LifecycleRefused("assumed role provider account differs")
    session._superplane_source_session = source
    session._superplane_role_arn = role_arn
    session._superplane_external_id = external_id
    return session


def canonical_role_identity(session, source, role_arn, *, verify):
    from superplane_bootstrap.target import ProviderIdentity

    verify()
    identity = session.client("sts").get_caller_identity()
    role = source.client("iam").get_role(RoleName=role_arn.rsplit("/", 1)[1])["Role"]
    verify()
    if (
        role.get("Arn") != role_arn
        or identity.get("Account") != role_arn.split(":")[4]
        or identity.get("UserId", "").split(":", 1)[0] != role.get("RoleId")
    ):
        raise LifecycleRefused(
            "bootstrap actor does not hold the exact approved IAM role"
        )
    return ProviderIdentity(account_id=identity["Account"], principal_arn=role_arn)
