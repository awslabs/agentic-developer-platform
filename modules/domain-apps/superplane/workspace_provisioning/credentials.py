"""Private renewable AWS sessions, always scoped to a freshly verified operation."""

from .runtime_config import LifecycleRefused


def assume_session(
    source,
    *,
    role_arn,
    region,
    verify,
    external_id=None,
    policy=None,
    session_name="superplane-lifecycle",
):
    import boto3
    import botocore.session
    import re
    from botocore.config import Config
    from botocore.credentials import RefreshableCredentials

    parts = role_arn.split(":", 5)
    if (
        len(parts) != 6
        or parts[:3] != ["arn", "aws", "iam"]
        or not parts[5].startswith("role/")
    ):
        raise LifecycleRefused("lifecycle requires an exact AWS IAM role")
    if not isinstance(session_name, str) or not re.fullmatch(
        r"[A-Za-z0-9_+=,.@-]{2,64}", session_name
    ):
        raise LifecycleRefused("AWS role session name is invalid")
    sts = source.client(
        "sts",
        region_name=region,
        config=Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 0}),
    )

    def refresh():
        verify()
        deadline_reader = getattr(source, "_superplane_authority_deadline", None)
        if deadline_reader is not None:
            from datetime import UTC, datetime, timedelta

            if datetime.now(UTC) + timedelta(seconds=900) >= deadline_reader():
                raise LifecycleRefused(
                    "remaining provider authority is shorter than the STS minimum"
                )
        arguments = {
            "RoleArn": role_arn,
            "RoleSessionName": session_name,
            "DurationSeconds": 900,
        }
        if external_id:
            arguments["ExternalId"] = external_id
        if policy:
            arguments["Policy"] = policy
        credentials = sts.assume_role(**arguments)["Credentials"]
        verify()
        if (
            deadline_reader is not None
            and credentials["Expiration"] > deadline_reader()
        ):
            raise LifecycleRefused(
                "actor credentials exceed current provider authority"
            )
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
    deadline_reader = getattr(source, "_superplane_authority_deadline", None)
    if (
        deadline_reader is not None
        and selected._credentials._expiry_time > deadline_reader()
    ):
        raise LifecycleRefused("actor credentials exceed current provider authority")
    if identity["Account"] != parts[4]:
        raise LifecycleRefused("assumed role provider account differs")
    session._superplane_source_session = source
    session._superplane_role_arn = role_arn
    session._superplane_external_id = external_id
    if getattr(source, "_superplane_authority_deadline", None) is not None:
        session._superplane_authority_deadline = source._superplane_authority_deadline
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


def canonical_role_identity_snapshot(sessions, source, roles, *, verify):
    """Fresh independent actor reads, bracketed once by live operation checks."""
    read_results = _actor_identity_results_reader(sessions, source, roles)

    def read():
        verify()
        values = read_results()
        verify()
        return _canonical_role_identities(values, roles)

    return read


def _bootstrap_actor_identity_reads(sessions, source, roles):
    """Read-only batch for the enclosing BootstrapClients.verify proof.

    That proof owns fresh operation checks before these actor reads and after
    its EKS target read. Credential refresh still checks its own authority.
    This private reader must not be used as a standalone authorization check.
    """
    read_results = _actor_identity_results_reader(sessions, source, roles)

    def read():
        return _canonical_role_identities(read_results(), roles)

    return read


def _actor_identity_results_reader(sessions, source, roles):
    """Construct bounded SDK reads without caching any observed result.

    Construct SDK clients on the owning thread. The returned reader uses only
    thread-safe client calls and joins every started read, including on failure.
    """
    from concurrent.futures import ThreadPoolExecutor

    if set(sessions) != set(roles):
        raise LifecycleRefused("bootstrap actor sessions are incomplete")
    sts = {actor: session.client("sts") for actor, session in sessions.items()}
    iam = source.client("iam")

    def read():
        with ThreadPoolExecutor(max_workers=6) as executor:
            identities = {
                actor: executor.submit(client.get_caller_identity)
                for actor, client in sts.items()
            }
            observed = {
                actor: executor.submit(iam.get_role, RoleName=arn.rsplit("/", 1)[1])
                for actor, arn in roles.items()
            }
            values = {
                actor: (identities[actor].result(), observed[actor].result()["Role"])
                for actor in roles
            }
        return values

    return read


def _canonical_role_identities(values, roles):
    from superplane_bootstrap.target import ProviderIdentity

    result = {}
    for actor, (identity, role) in values.items():
        arn = roles[actor]
        if (
            role.get("Arn") != arn
            or identity.get("Account") != arn.split(":")[4]
            or identity.get("UserId", "").split(":", 1)[0] != role.get("RoleId")
        ):
            raise LifecycleRefused(
                "bootstrap actor does not hold the exact approved IAM role"
            )
        result[actor] = ProviderIdentity(
            account_id=identity["Account"], principal_arn=arn
        )
    return result
