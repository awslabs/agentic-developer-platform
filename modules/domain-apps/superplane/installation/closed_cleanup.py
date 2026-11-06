"""Prepare, then publish exact reviewed owner-only cleanup permits.

No enrollment renewal, credential export, worker admission or cluster mutation.
Publication re-qualifies the source and compares exact saved bytes. Lost writes
are reconciled by consistent read, never by regenerating a timestamp/permit.
"""

import copy
from datetime import datetime, timezone
from uuid import uuid4

from .bootstrap_components_repeat import qualify_cleanup
from .provider_authority import contract, require


def owner(enrollment, session, permit=None):
    authority = contract().validate(enrollment["authority"])
    identity = session.client("sts").get_caller_identity()
    arn, role_id = enrollment["operator_role_arn"], enrollment["operator_role_id"]
    require(identity["Account"] == authority["account_id"])
    require(identity["UserId"].split(":")[0] == role_id)
    require(
        identity["Arn"].startswith(
            arn.replace(":iam:", ":sts:").replace(":role/", ":assumed-role/") + "/"
        )
    )
    if permit is not None:
        require(
            (permit["owner_role_arn"], permit["owner_role_id"], permit["authority"])
            == (arn, role_id, authority)
        )
    return authority


def prepare(
    enrollment,
    original,
    descriptor,
    session,
    *,
    protected,
    expires_at,
    service_source_sha256,
    read_snapshot,
    read_expiry,
    **source_options,
):
    from superplane_bootstrap.closed_cleanup_permit import (
        KIND,
        PREFIX,
        VIEW_POLICY,
        binding_digest,
        validate,
    )

    authority = owner(enrollment, session)
    qualified = qualify_cleanup(
        enrollment,
        original,
        descriptor,
        session,
        read_snapshot=read_snapshot,
        read_expiry=read_expiry,
        **source_options,
    )
    arn = next(
        g["principal_arn"]
        for g in qualified["retained_plan"]["grants"]
        if g["key"] == "registrar-entry"
    )
    registrar = session.client("iam").get_role(RoleName=arn.rsplit("/", 1)[1])["Role"]
    require(registrar["Arn"] == arn)
    value = {
        "kind": KIND,
        "attempt_id": str(uuid4()),
        "status": "active",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at,
        "owner_role_arn": enrollment["operator_role_arn"],
        "owner_role_id": enrollment["operator_role_id"],
        "authority": authority,
        "qualification": qualified,
        "descriptor": copy.deepcopy(descriptor),
        "operation": copy.deepcopy(protected["operation"]),
        "lease": copy.deepcopy(protected["lease"]),
        "artifact": copy.deepcopy(protected["artifact"]),
        "service_source_sha256": service_source_sha256,
        "verifier_policy_arn": VIEW_POLICY,
        "registrar": {"role_arn": arn, "role_id": registrar["RoleId"]},
    }
    value["permit_id"] = PREFIX + binding_digest(value)
    return validate(value)


def publish(enrollment, permit, session, *, requalify, previous=None):
    """Publish only the exact reviewed permit; renewal/revocation use exact CAS.

    requalify is the maintained owner collector for the initial permit, or the
    locked forward-transition reader for renewal. It returns the stable binding.
    Revocation needs no live cluster access and may only turn active into revoked.
    """
    from superplane_bootstrap.closed_cleanup_permit import (
        binding_digest,
        digest,
        encode_document,
        validate,
    )

    owner(enrollment, session, permit)
    if permit["status"] == "active":
        validate(permit)
        require(requalify() == binding_digest(permit))
    else:
        require(
            previous is not None
            and previous["status"] == "active"
            and permit == {**previous, "status": "revoked"}
        )
        validate(previous, fresh=False)
    if previous is not None:
        owner(enrollment, session, previous)
        require(binding_digest(previous) == binding_digest(permit))
    table = f"adp-{enrollment['environment']}-superplane-provider-authorities"
    ddb = session.client(
        "dynamodb",
        region_name=enrollment.get("management_region", permit["authority"]["region"]),
    )

    def item(value):
        return {
            "record_id": {"S": value["permit_id"]},
            "document": {"S": encode_document(value)},
            "revision": {"S": digest(value)},
        }

    key, successor = {"record_id": {"S": permit["permit_id"]}}, item(permit)

    def read():
        return ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")

    expected = item(previous) if previous else None
    actual = read()
    require(actual == expected or actual == successor)
    if actual != successor:
        args = (
            {
                "ConditionExpression": "revision = :old",
                "ExpressionAttributeValues": {":old": expected["revision"]},
            }
            if previous
            else {"ConditionExpression": "attribute_not_exists(record_id)"}
        )
        try:
            ddb.put_item(TableName=table, Item=successor, **args)
        except Exception:
            if read() != successor:
                raise
    require(read() == successor)
    return {
        "permit_id": permit["permit_id"],
        "revision": digest(permit),
        "written": True,
        "provider_enrollment_renewed": False,
        "worker_resumed": False,
    }


def successor(previous, *, expires_at=None):
    """Prepare an inert exact renewal/revocation for separate owner review."""
    from superplane_bootstrap.closed_cleanup_permit import validate

    validate(previous, fresh=False)
    value = copy.deepcopy(previous)
    if expires_at is None:
        value["status"] = "revoked"
    else:
        value.update(
            created_at=datetime.now(timezone.utc).isoformat(), expires_at=expires_at
        )
        validate(value)
        require(
            datetime.fromisoformat(expires_at)
            > datetime.fromisoformat(previous["expires_at"])
        )
    return value


def verify_teardown(enrollment, permit, session):
    """Independent owner gate after service cleanup, IAM teardown and revocation.

    This does not infer absence from a failed call. The native evidence collector
    still proves original namespace/components/journals separately.
    """
    from superplane_bootstrap.closed_cleanup_journal import CleanupJournal
    from superplane_bootstrap.closed_cleanup_permit import (
        binding_digest,
        digest,
        encode_document,
        validate,
    )
    from superplane_bootstrap.eks_grants import _absent

    validate(permit, fresh=False)
    owner(enrollment, session, permit)
    authority, qualification = permit["authority"], permit["qualification"]
    table = f"adp-{enrollment['environment']}-superplane-provider-authorities"
    evidence = f"adp-{enrollment['environment']}-superplane-provider-evidence"
    ddb = session.client(
        "dynamodb", region_name=enrollment.get("management_region", authority["region"])
    )
    revoked = {**permit, "status": "revoked"}
    expected = {
        "record_id": {"S": permit["permit_id"]},
        "document": {"S": encode_document(revoked)},
        "revision": {"S": digest(revoked)},
    }
    require(
        ddb.get_item(
            TableName=table,
            Key={"record_id": expected["record_id"]},
            ConsistentRead=True,
        ).get("Item")
        == expected
    )
    journal = CleanupJournal(ddb, table, evidence, permit, lambda: None)
    progress = journal.read()
    require(
        progress["phase"] == "complete"
        and progress["result"]["generation"] == qualification["generation"]
    )
    eks = session.client("eks", region_name=authority["region"])
    principals = {authority["role_arn"]} | {
        g["principal_arn"]
        for g in qualification["retained_plan"]["grants"]
        if g["kind"] == "eks-entry"
    }
    for principal in principals:
        try:
            eks.describe_access_entry(
                clusterName=qualification["cluster_arn"].rsplit("/", 1)[1],
                principalArn=principal,
            )
        except Exception as error:
            require(_absent(error))
        else:
            require(False)
    policy_arn = (
        f"arn:aws:iam::{authority['account_id']}:policy/sp-closed-cleanup-"
        + permit["permit_id"].split(":")[1][:24]
    )
    try:
        session.client("iam").get_policy(PolicyArn=policy_arn)
    except Exception as error:
        require(
            getattr(error, "response", {}).get("Error", {}).get("Code")
            == "NoSuchEntity"
        )
    else:
        require(False)
    # Every issuance is append-only; checking just the latest row could miss an
    # older, longer session after clocks or issuance order changed.
    cursor, expirations = None, []
    while True:
        page = ddb.query(
            TableName=evidence,
            KeyConditionExpression="record_id = :key",
            ExpressionAttributeValues={":key": {"S": journal.key + "/sessions"}},
            ConsistentRead=True,
            **({"ExclusiveStartKey": cursor} if cursor else {}),
        )
        for row in page.get("Items", []):
            import json

            value = json.loads(row["document"]["S"])
            require(
                row["revision"]["S"] == digest(value)
                and row["authority_binding"]["S"] == binding_digest(permit)
            )
            require(
                value["phase"] == "session-issued"
                and value["role_arn"]
                in {authority["role_arn"], permit["registrar"]["role_arn"]}
            )
            expirations.append(datetime.fromisoformat(value["expires_at"]))
        next_cursor = page.get("LastEvaluatedKey")
        if not next_cursor:
            break
        require(next_cursor != cursor)
        cursor = next_cursor
    require(expirations and max(expirations) <= datetime.now(timezone.utc))
    return {
        "cleanup_permit_id": permit["permit_id"],
        "service_access_removed": True,
        "temporary_iam_policy_absent": True,
        "permit_revoked": True,
        "cleanup_sessions_expired": True,
    }
