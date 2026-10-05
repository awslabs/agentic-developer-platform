"""Trusted, atomic chat lease/launch admission; never called by sandbox tools."""

import os
from datetime import UTC, datetime

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.chat_authority import ChatRuntimeAuthority, ChatSessionLease
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, ChatLaunch, ChatLaunchStore
from src.agentauth.chat_user_turn import consume_user_turn, load_user_turn, user_turn_records, verify_user_turn
from src.agentauth.execution import ExecutionStatus
from src.agentauth.workload import VerifiedPod
from src.orchestration.chat_data_migration import _owner_fields, _owns_context_row

OPERATIONS = frozenset(
    {
        "history.read",
        "history.expand",
        "history.append",
        "memory.search",
        "memory.write",
        "artifact.create",
        "artifact.read",
        "draft.read",
        "draft.write",
        "session.share",
    }
)
SESSION_TTL_SECONDS = 90 * 24 * 60 * 60
LEASE_SECONDS = 300
_serializer = TypeSerializer()


def retention_seconds() -> int:
    try:
        retention = int(os.environ.get("SESSION_TTL_SECONDS", str(SESSION_TTL_SECONDS)))
        if retention > 0:
            return retention
    except ValueError:
        pass
    raise ChatAuthorizationUnavailableError("chat retention unconfigured")


def _encoded(values: dict) -> dict:
    return {name: _serializer.serialize(value) for name, value in values.items()}


def root_identity(authority: ChatRuntimeAuthority, run_id: str, digest: str, now: int):
    store = authority.store
    pointer = store._read(f"INVOCATION#{run_id}", "DISPATCH") or {}
    tenant_id = pointer.get("tenant_id", {}).get("S", "")
    if not tenant_id or pointer.get("envelope_digest") != {"S": digest}:
        raise ChatAuthorizationRefusedError("chat root unavailable")
    execution = store.authority.load_execution(invocation_id=run_id, tenant_id=tenant_id)
    if execution is None or not execution.repo or not execution.repo.startswith("chat/"):
        raise ChatAuthorizationRefusedError("chat root unavailable")
    grant = store.live_grant(invocation_id=run_id, tenant_id=tenant_id, attempt=execution.current_attempt, now=datetime.fromtimestamp(now, UTC))
    if grant.authority.kind != "chat_event" or execution.repo not in grant.repo_scope:
        raise ChatAuthorizationRefusedError("chat root unavailable")
    return execution, grant, execution.repo.removeprefix("chat/")


def admit(authority: ChatRuntimeAuthority, *, run_id: str, digest: str, pod: VerifiedPod, team_id: str, now: int) -> ChatLaunch:
    store = authority.store
    execution, grant, session_id = root_identity(authority, run_id, digest, now)
    if not pod.image_digest or grant.expires_at is None:
        raise ChatAuthorizationRefusedError("chat launch unavailable")
    execution = store.bind(invocation_id=run_id, digest=digest, pod=pod, now=datetime.fromtimestamp(now, UTC))
    if store.authority.abort_intent(invocation_id=run_id, tenant_id=execution.tenant_id) is not None:
        raise ChatAuthorizationRefusedError("chat run ended")
    user_turn = load_user_turn(store, execution, digest)
    launches = ChatLaunchStore(store)
    existing = store._read(f"CHAT-LAUNCH#{run_id}", "LAUNCH")
    if existing is not None:
        launch = launches.load(run_id)
        if (
            launch.sandbox_uid != pod.uid
            or launch.image_digest != pod.image_digest
            or launch.tenant_id != execution.tenant_id
            or launch.user_id != grant.authority.human_id
            or launch.team_id != team_id
            or launch.session_id != session_id
            or not authority.current(launch, now)
        ):
            raise ChatAuthorizationRefusedError("chat launch already bound")
        if user_turn is not None:
            verify_user_turn(authority.context_table, launch, user_turn)
        return launch
    table = authority.context_table
    key = {"PK": f"session#{session_id}", "SK": "header"}
    header = table.get_item(Key=key, ConsistentRead=True).get("Item")
    owner = (execution.tenant_id, team_id, grant.authority.human_id)
    previous = None
    handoff_checks = []
    if header is not None:
        if not _owns_context_row(header, owner) or header.get("status") != "active" or header.get("ttl", 0) <= now:
            raise ChatAuthorizationRefusedError("chat session unavailable")
        if "chatLease" in header:
            previous = ChatSessionLease.model_validate(header["chatLease"])
            if previous.expires_at > now:
                if previous.run_id == run_id:
                    raise ChatAuthorizationRefusedError("chat session already leased")
                handoff_checks.append(
                    {
                        "ConditionCheck": {
                            "TableName": store.table,
                            "Key": _encoded({"pk": f"TENANT#{execution.tenant_id}", "sk": f"EXEC#{previous.run_id}"}),
                            "ConditionExpression": "#status IN (:completed, :cancelled, :revoked) AND #repo = :repo AND workload_binding = :pod",
                            "ExpressionAttributeNames": {"#status": "status", "#repo": "repo"},
                            "ExpressionAttributeValues": _encoded(
                                {
                                    ":completed": ExecutionStatus.COMPLETED.value,
                                    ":cancelled": ExecutionStatus.CANCELLED.value,
                                    ":revoked": ExecutionStatus.REVOKED.value,
                                    ":repo": f"chat/{session_id}",
                                    ":pod": previous.sandbox_uid,
                                }
                            ),
                        }
                    }
                )
    expires_at = int(grant.expires_at.timestamp())
    if pod.deadline_at:
        deadline = datetime.fromisoformat(pod.deadline_at.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            raise ChatAuthorizationRefusedError("chat deadline invalid")
        expires_at = min(expires_at, int(deadline.timestamp()))
    if expires_at <= now:
        raise ChatAuthorizationRefusedError("chat launch expired")
    launch = ChatLaunch(
        run_id=run_id,
        tenant_id=execution.tenant_id,
        user_id=grant.authority.human_id,
        team_id=team_id,
        session_id=session_id,
        sandbox_uid=pod.uid,
        image_digest=pod.image_digest,
        attempt=execution.current_attempt,
        credential_epoch=execution.current_credential_epoch,
        lease_generation=previous.generation + 1 if previous else 1,
        grant_id=grant.grant_id,
        grant_epoch=grant.revocation_epoch,
        operations=OPERATIONS,
        expires_at=expires_at,
    )
    lease = ChatSessionLease(run_id=run_id, sandbox_uid=pod.uid, generation=launch.lease_generation, expires_at=min(now + LEASE_SECONDS, expires_at))
    if header is None:
        stamp = datetime.fromtimestamp(now, UTC).isoformat()
        retention = retention_seconds()
        context_write = {
            "Put": {
                "TableName": table.name,
                "Item": _encoded(
                    {
                        **key,
                        **_owner_fields(owner),
                        "status": "active",
                        "ttl": now + retention,
                        "createdAt": stamp,
                        "lastActivityAt": stamp,
                        "chatLease": lease.model_dump(),
                    }
                ),
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        }
    else:
        fields = _owner_fields(owner)
        names = {f"#{field}": field for field in fields}
        values = {f":{field}": value for field, value in fields.items()}
        required = {"orgId", "tenantId", "teamId", "ownerUserId"}
        conditions = [
            f"#{field} = :{field}" if field in required else f"(attribute_not_exists(#{field}) OR #{field} = :null OR #{field} = :{field})"
            for field in fields
        ]
        conditions += ["#status = :active", "#ttl = :ttl", "#lease = :previous" if previous else "attribute_not_exists(#lease)"]
        names.update({"#status": "status", "#ttl": "ttl", "#lease": "chatLease"})
        values.update({":active": "active", ":ttl": header["ttl"], ":null": None, ":lease": lease.model_dump()})
        if previous:
            values[":previous"] = previous.model_dump()
        context_write = {
            "Update": {
                "TableName": table.name,
                "Key": _encoded(key),
                "UpdateExpression": "SET #lease = :lease",
                "ConditionExpression": " AND ".join(conditions),
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": _encoded(values),
            }
        }
    user_writes = []
    if user_turn is not None:
        payload, input_delete = consume_user_turn(table, launch, user_turn, digest, now)
        records, version, next_ordinal = user_turn_records(table, header, launch, payload)
        user_writes = [
            {"Put": {"TableName": table.name, "Item": _encoded(record), "ConditionExpression": "attribute_not_exists(PK)"}} for record in records
        ]
        user_writes.append(input_delete)
        if header is None:
            context_write["Put"]["Item"].update(_encoded({"historyVersion": version, "historyNextOrdinal": next_ordinal}))
        else:
            update = context_write["Update"]
            update["UpdateExpression"] += ", #version = :version, #next = :next, #ttl = :refreshed_ttl, lastActivityAt = :activity"
            update["ExpressionAttributeNames"].update({"#version": "historyVersion", "#next": "historyNextOrdinal"})
            update["ExpressionAttributeValues"].update(
                _encoded(
                    {
                        ":version": version,
                        ":next": next_ordinal,
                        ":refreshed_ttl": max(int(header["ttl"]), now + retention_seconds()),
                        ":activity": datetime.fromtimestamp(now, UTC).isoformat(),
                    }
                )
            )
            for field, name in (("historyVersion", "#version"), ("historyNextOrdinal", "#next")):
                if field in header:
                    value = f":previous_{field}"
                    update["ConditionExpression"] += f" AND {name} = {value}"
                    update["ExpressionAttributeValues"].update(_encoded({value: header[field]}))
                else:
                    update["ConditionExpression"] += f" AND attribute_not_exists({name})"
    try:
        store.client.transact_write_items(
            TransactItems=[
                context_write,
                *user_writes,
                *handoff_checks,
                {"Put": {"TableName": store.table, "Item": launches.item(launch), "ConditionExpression": "attribute_not_exists(pk)"}},
                store._authority_check(grant),
                store._grant_check(grant, datetime.fromtimestamp(now, UTC)),
                {
                    "ConditionCheck": {
                        "TableName": store.table,
                        "Key": {"pk": {"S": f"TENANT#{launch.tenant_id}"}, "sk": {"S": f"EXEC#{run_id}"}},
                        "ConditionExpression": (
                            "#status = :active AND workload_binding = :pod AND current_attempt = :attempt AND current_credential_epoch = :epoch"
                        )
                        + (" AND chat_user_turn = :user_turn" if user_turn is not None else ""),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": _encoded(
                            {
                                ":active": "active",
                                ":pod": pod.uid,
                                ":attempt": launch.attempt,
                                ":epoch": launch.credential_epoch,
                                **({":user_turn": user_turn} if user_turn is not None else {}),
                            }
                        ),
                    }
                },
            ]
        )
    except ClientError as error:
        reasons = error.response.get("CancellationReasons", [])
        if error.response.get("Error", {}).get("Code") == "TransactionCanceledException" and any(
            reason.get("Code") == "ConditionalCheckFailed" for reason in reasons
        ):
            raise ChatAuthorizationRefusedError("chat admission changed; retry") from None
        raise ChatAuthorizationUnavailableError("chat admission unavailable") from None
    except BotoCoreError:
        raise ChatAuthorizationUnavailableError("chat admission unavailable") from None
    return launch


def renew_lease(authority: ChatRuntimeAuthority, launch: ChatLaunch, *, now: int) -> None:
    if not authority.current(launch, now):
        raise ChatAuthorizationRefusedError("chat lease unavailable")
    lease = ChatSessionLease(
        run_id=launch.run_id,
        sandbox_uid=launch.sandbox_uid,
        generation=launch.lease_generation,
        expires_at=min(now + LEASE_SECONDS, launch.expires_at),
    )
    try:
        header = authority.context_table.get_item(Key={"PK": f"session#{launch.session_id}", "SK": "header"}, ConsistentRead=True).get("Item")
        if not header:
            raise ChatAuthorizationRefusedError("chat session unavailable")
        authority.context_table.update_item(
            Key={"PK": f"session#{launch.session_id}", "SK": "header"},
            UpdateExpression="SET #lease.expires_at = :expiry, #ttl = :ttl",
            ConditionExpression=(
                "#lease.run_id = :run AND #lease.sandbox_uid = :pod AND #lease.generation = :generation AND #lease.expires_at > :now "
                "AND orgId = :tenant AND tenantId = :tenant AND ownerUserId = :user AND teamId = :team AND #status = :active "
                "AND #ttl > :now AND #ttl = :previous_ttl"
            ),
            ExpressionAttributeNames={"#lease": "chatLease", "#status": "status", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":expiry": lease.expires_at,
                ":run": launch.run_id,
                ":pod": launch.sandbox_uid,
                ":generation": launch.lease_generation,
                ":now": now,
                ":tenant": launch.tenant_id,
                ":user": launch.user_id,
                ":team": launch.team_id,
                ":active": "active",
                ":ttl": max(int(header["ttl"]), now + retention_seconds()),
                ":previous_ttl": header["ttl"],
            },
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ChatAuthorizationRefusedError("chat lease changed") from None
        raise ChatAuthorizationUnavailableError("chat lease unavailable") from None
    except BotoCoreError:
        raise ChatAuthorizationUnavailableError("chat lease unavailable") from None
