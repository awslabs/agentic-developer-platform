"""Trusted, atomic chat lease/launch admission; never called by sandbox tools."""

import hashlib
import os
from datetime import UTC, datetime
from decimal import Decimal
from ipaddress import IPv4Address, IPv4Network

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from src.agentauth.chat_authority import ChatRuntimeAuthority, ChatSessionLease
from src.agentauth.chat_capability import (
    ChatAuthorizationRefusedError,
    ChatAuthorizationUnavailableError,
    ChatLaunch,
    ChatLaunchStore,
    require_sandbox_run,
)
from src.agentauth.chat_user_turn import consume_user_turn, load_user_turn, user_turn_records, verify_user_turn
from src.agentauth.execution import ExecutionStatus
from src.agentauth.workload import VerifiedPod
from src.crew.targets import CrewLease
from src.orchestration.chat_data_migration import _owner_fields, _owns_context_row

OPERATIONS = frozenset(
    {
        "model.invoke",
        "tools.invoke",
        "turn.next",
        "turn.result",
        "history.read",
        "activity.read",
        "history.expand",
        "history.append",
        "memory.search",
        "memory.write",
        "artifact.create",
        "artifact.read",
        "draft.read",
        "draft.write",
        "session.share",
        "installation.status",
        "installation.failure",
    }
)
SESSION_TTL_SECONDS = 90 * 24 * 60 * 60
LEASE_SECONDS = 300
PERSISTENT_LEASE_SECONDS = 90
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


def _crew_owner_checks(
    authority: ChatRuntimeAuthority,
    run_id: str,
    pod: VerifiedPod,
    now: int,
    store_factory,
    tenant_id: str,
    user_id: str,
    session_id: str,
    expected_owner_key: str | None,
) -> tuple[list[dict], bool]:
    creation = authority.store._read(f"CHAT-LAUNCH#{run_id}", "CREATION") or {}
    bound_run = creation.get("crew_run_id", {}).get("S")
    if not bound_run:
        return [], False
    crew_store = store_factory()
    row = crew_store.read_run(bound_run)
    if not row or row.get("kind") != "crew":
        raise ChatAuthorizationRefusedError("Crew run unavailable")
    if row.get("executor_scope") == "session":
        return [], False
    if row.get("executor_scope") != "user":
        raise ChatAuthorizationRefusedError("Crew run unavailable")
    generation = row.get("lease_generation")
    if (
        not isinstance(row.get("session_id"), str)
        or isinstance(generation, bool)
        or not isinstance(generation, int | Decimal)
        or int(generation) != generation
        or generation < 1
    ):
        raise ChatAuthorizationRefusedError("Crew founding session unavailable")
    shared = row["session_id"] != session_id
    marker = (
        crew_store.table.get_item(
            Key={"event_id": f"crew-lease#{tenant_id}#{session_id}#{generation}", "arrived_at": "crew"}, ConsistentRead=True
        ).get("Item")
        if shared
        else None
    )
    owner = crew_store.table.get_item(Key={"event_id": f"crew-owner#{tenant_id}#{user_id}", "arrived_at": "crew"}, ConsistentRead=True).get("Item")
    lease_row = authority.context_table.get_item(Key={"PK": f"crew#{tenant_id}#{user_id}", "SK": "executor"}, ConsistentRead=True).get("Item")
    try:
        lease = CrewLease.model_validate(lease_row["crewLease"])
    except (TypeError, KeyError, ValidationError):
        raise ChatAuthorizationRefusedError("Crew owner lease unavailable") from None
    try:
        pod_ip = IPv4Address(pod.ip)
    except (TypeError, ValueError):
        raise ChatAuthorizationRefusedError("Crew pod address unavailable") from None
    if not any(pod_ip in IPv4Network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise ChatAuthorizationRefusedError("Crew pod address unavailable")
    if (
        row.get("status") != "active"
        or row.get("root_human_id") != user_id
        or row.get("tenant_id") != tenant_id
        or row.get("user_id") != user_id
        or row.get("target_kind") != "eks-pod"
        or row.get("target_ref") != pod.name
        or row.get("pod_uid") != pod.uid
        or row.get("lease_generation") != lease.generation
        or row.get("lease_expires_at", 0) <= now
        or not owner
        or owner.get("run_id") != bound_run
        or owner.get("pod_uid") != pod.uid
        or owner.get("generation") != lease.generation
        or owner.get("lease_expires_at", 0) <= now
        or owner.get("target_ref") != pod.name
        or owner.get("session_count") not in range(1, 21)
        or owner.get("executor_scope") != "user"
        or owner.get("target_kind") != "eks-pod"
        or not lease_row
        or lease_row.get("tenantId") != tenant_id
        or lease_row.get("ownerUserId") != user_id
        or lease_row.get("crewLeaseState") not in {"provisioning", "ready"}
        or lease.tenant_id != tenant_id
        or lease.user_id != user_id
        or lease.target_ref != pod.uid
        or lease.target_kind != "eks-pod"
        or lease.endpoint != f"http://{pod_ip}:5476"
        or lease.expires_at <= now
        or pod.owner_key is None
        or pod.owner_key != expected_owner_key
        or creation.get("binding", {}).get("M", {}).get("session_id") != {"S": session_id}
        or creation.get("binding", {}).get("M", {}).get("tenant_id") != {"S": tenant_id}
        or creation.get("binding", {}).get("M", {}).get("user_id") != {"S": user_id}
        or creation.get("crew_lease_generation") != {"N": str(lease.generation)}
        or (
            shared
            and (
                lease_row.get("crewLeaseState") != "ready"
                or pod.session_hash != hashlib.sha256(row["session_id"].encode()).hexdigest()
                or not marker
                or marker.get("status") != "active"
                or marker.get("run_id") != bound_run
                or marker.get("run_arrived_at") != row["arrived_at"]
                or marker.get("expires_at", 0) <= now
            )
        )
    ):
        raise ChatAuthorizationRefusedError("Crew owner admission unavailable")
    return [
        *(
            [
                {
                    "ConditionCheck": {
                        "TableName": crew_store.table.name,
                        "Key": _encoded({"event_id": marker["event_id"], "arrived_at": "crew"}),
                        "ConditionExpression": "#status = :active AND run_id = :run AND run_arrived_at = :arrived AND expires_at > :now",
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": _encoded({":active": "active", ":run": bound_run, ":arrived": row["arrived_at"], ":now": now}),
                    }
                }
            ]
            if shared
            else []
        ),
        {
            "ConditionCheck": {
                "TableName": crew_store.table.name,
                "Key": _encoded({"event_id": row["event_id"], "arrived_at": row["arrived_at"]}),
                "ConditionExpression": "#status = :active AND pod_uid = :pod AND lease_generation = :generation AND lease_expires_at > :now",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": _encoded({":active": "active", ":pod": pod.uid, ":generation": lease.generation, ":now": now}),
            }
        },
        {
            "ConditionCheck": {
                "TableName": crew_store.table.name,
                "Key": _encoded({"event_id": owner["event_id"], "arrived_at": "crew"}),
                "ConditionExpression": "run_id = :run AND pod_uid = :pod AND generation = :generation AND lease_expires_at > :now",
                "ExpressionAttributeValues": _encoded({":run": bound_run, ":pod": pod.uid, ":generation": lease.generation, ":now": now}),
            }
        },
        {
            "ConditionCheck": {
                "TableName": authority.context_table.name,
                "Key": _encoded({"PK": lease_row["PK"], "SK": "executor"}),
                "ConditionExpression": (
                    "tenantId = :tenant AND ownerUserId = :user AND crewLease = :lease AND crewLeaseState = :state AND crewLease.expires_at > :now"
                ),
                "ExpressionAttributeValues": _encoded(
                    {":tenant": tenant_id, ":user": user_id, ":lease": lease_row["crewLease"], ":state": lease_row["crewLeaseState"], ":now": now}
                ),
            }
        },
    ], shared


def admit(
    authority: ChatRuntimeAuthority,
    *,
    run_id: str,
    digest: str,
    pod: VerifiedPod,
    team_id: str,
    now: int,
    crew_store_factory=None,
    crew_owner_key: str | None = None,
) -> ChatLaunch:
    from src.agentauth.chat_sandbox_creation import admission_creation_check

    require_sandbox_run(pod, run_id)
    store = authority.store
    execution, grant, session_id = root_identity(authority, run_id, digest, now)
    if not pod.image_digest or grant.expires_at is None:
        raise ChatAuthorizationRefusedError("chat launch unavailable")
    table = authority.context_table
    key = {"PK": f"session#{session_id}", "SK": "header"}
    selected = table.get_item(Key=key, ConsistentRead=True).get("Item")
    selected_mode = selected.get("sessionMode", "ephemeral") if selected else "ephemeral"
    expected_session_hash = hashlib.sha256(session_id.encode()).hexdigest() if selected_mode == "persistent" else None
    crew_checks, shared = (
        _crew_owner_checks(authority, run_id, pod, now, crew_store_factory, execution.tenant_id, grant.authority.human_id, session_id, crew_owner_key)
        if crew_store_factory
        else ([], False)
    )
    if (
        selected_mode not in {"ephemeral", "persistent"}
        or (shared and selected_mode != "persistent")
        or (not shared and pod.session_hash != expected_session_hash)
    ):
        raise ChatAuthorizationRefusedError("chat session sandbox mode changed")
    creation_check = None if shared else admission_creation_check(authority, run_id, execution.tenant_id, pod)
    if shared:
        if execution.status != ExecutionStatus.PENDING or execution.workload_binding is not None:
            if store._read(f"CHAT-LAUNCH#{run_id}", "LAUNCH") is None:
                raise ChatAuthorizationRefusedError("chat shared launch already bound")
    else:
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
    if shared:
        creation_check = admission_creation_check(authority, run_id, execution.tenant_id, pod, reused=True)
    header = table.get_item(Key=key, ConsistentRead=True).get("Item")
    if header and (header.get("sessionPendingMode") or header.get("sessionState") in {"ending", "recovering", "ended"}):
        raise ChatAuthorizationRefusedError("chat session ending")
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
        session_run_id=run_id if selected_mode == "persistent" else None,
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
    mode = header.get("sessionMode", "ephemeral") if header else "ephemeral"
    if mode != selected_mode:
        raise ChatAuthorizationRefusedError("chat session mode unavailable")
    lease_seconds = PERSISTENT_LEASE_SECONDS if mode == "persistent" else LEASE_SECONDS
    lease = ChatSessionLease(run_id=run_id, sandbox_uid=pod.uid, generation=launch.lease_generation, expires_at=min(now + lease_seconds, expires_at))
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
                        "sessionMode": mode,
                        "ttl": now + retention,
                        "createdAt": stamp,
                        "lastActivityAt": stamp,
                        "chatLease": lease.model_dump(),
                        "sessionState": "active" if mode == "persistent" else "idle",
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
        conditions.append("#mode = :mode" if "sessionMode" in header else "attribute_not_exists(#mode)")
        conditions.append("attribute_not_exists(sessionPendingMode)")
        conditions.append("sessionState = :session_state" if "sessionState" in header else "attribute_not_exists(sessionState)")
        names.update({"#status": "status", "#ttl": "ttl", "#lease": "chatLease", "#mode": "sessionMode"})
        values.update({":active": "active", ":ttl": header["ttl"], ":null": None, ":lease": lease.model_dump()})
        if "sessionState" in header:
            values[":session_state"] = header["sessionState"]
        if "sessionMode" in header:
            values[":mode"] = mode
        if previous:
            values[":previous"] = previous.model_dump()
        context_write = {
            "Update": {
                "TableName": table.name,
                "Key": _encoded(key),
                "UpdateExpression": "SET #lease = :lease, sessionState = :running",
                "ConditionExpression": " AND ".join(conditions),
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": _encoded({**values, ":running": "active" if mode == "persistent" else "idle"}),
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
    execution_guard = {
        "ConditionCheck": {
            "TableName": store.table,
            "Key": {"pk": {"S": f"TENANT#{launch.tenant_id}"}, "sk": {"S": f"EXEC#{run_id}"}},
            "ConditionExpression": (
                "#status = :active AND workload_binding = :pod AND current_attempt = :attempt "
                "AND current_credential_epoch = :epoch AND attribute_not_exists(abort_command_id) "
                "AND attribute_not_exists(chat_pre_admission_cleanup)"
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
    }
    if shared:
        execution_metadata = store._read(f"TENANT#{execution.tenant_id}", f"EXEC#{run_id}") or {}
        run_hash = hashlib.sha256(run_id.encode()).hexdigest()
        reserved_name = f"chat-turn-{run_hash[:12]}-{run_hash[12:32]}"
        if (
            execution_metadata.get("status") != {"S": "pending"}
            or execution_metadata.get("pod_name") != {"S": reserved_name}
            or "chat_sandbox_creation" not in execution_metadata
        ):
            raise ChatAuthorizationRefusedError("chat shared creation changed")
        execution_guard = {
            "Update": {
                "TableName": store.table,
                "Key": _encoded({"pk": f"TENANT#{launch.tenant_id}", "sk": f"EXEC#{run_id}"}),
                "UpdateExpression": "SET #status = :active, workload_binding = :pod, pod_name = :name, pod_ip = :ip",
                "ConditionExpression": (
                    "#status = :pending AND current_attempt = :attempt AND current_credential_epoch = :epoch "
                    "AND attribute_not_exists(workload_binding) AND envelope_digest = :digest AND pod_name = :reserved "
                    "AND chat_sandbox_creation = :creation AND attribute_not_exists(abort_command_id) "
                    "AND attribute_not_exists(chat_pre_admission_cleanup)"
                )
                + (" AND chat_user_turn = :user_turn" if user_turn is not None else ""),
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {
                    **_encoded(
                        {
                            ":active": "active",
                            ":pending": "pending",
                            ":pod": pod.uid,
                            ":name": pod.name,
                            ":ip": pod.ip,
                            ":attempt": launch.attempt,
                            ":epoch": launch.credential_epoch,
                            ":digest": digest,
                            ":reserved": reserved_name,
                            **({":user_turn": user_turn} if user_turn is not None else {}),
                        }
                    ),
                    ":creation": execution_metadata["chat_sandbox_creation"],
                },
            }
        }
    try:
        store.client.transact_write_items(
            TransactItems=[
                *(
                    [
                        {
                            "Put": {
                                "TableName": table.name,
                                "Item": _encoded(
                                    {
                                        "PK": "chat-notifications",
                                        "SK": f"cleanup#{run_id}",
                                        **_owner_fields(owner),
                                        "sessionId": session_id,
                                        "sandboxUid": pod.uid,
                                        "leaseGeneration": launch.lease_generation,
                                        "ttl": max(now + retention_seconds(), int(header["ttl"]) if header else 0),
                                    }
                                ),
                                "ConditionExpression": "attribute_not_exists(PK)",
                            }
                        }
                    ]
                    if mode == "persistent"
                    else []
                ),
                context_write,
                creation_check,
                *crew_checks,
                *user_writes,
                *handoff_checks,
                {"Put": {"TableName": store.table, "Item": launches.item(launch), "ConditionExpression": "attribute_not_exists(pk)"}},
                store._authority_check(grant),
                store._grant_check(grant, datetime.fromtimestamp(now, UTC)),
                execution_guard,
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
    try:
        header = authority.context_table.get_item(Key={"PK": f"session#{launch.session_id}", "SK": "header"}, ConsistentRead=True).get("Item")
        if not header:
            raise ChatAuthorizationRefusedError("chat session unavailable")
        mode = header.get("sessionMode", "ephemeral")
        if mode not in {"ephemeral", "persistent"}:
            raise ChatAuthorizationRefusedError("chat session mode unavailable")
        lease = ChatSessionLease(
            run_id=launch.run_id,
            sandbox_uid=launch.sandbox_uid,
            generation=launch.lease_generation,
            expires_at=min(now + (PERSISTENT_LEASE_SECONDS if mode == "persistent" else LEASE_SECONDS), launch.expires_at),
        )
        authority.context_table.update_item(
            Key={"PK": f"session#{launch.session_id}", "SK": "header"},
            UpdateExpression="SET #lease.expires_at = :expiry, #ttl = :ttl",
            ConditionExpression=(
                "#lease.run_id = :run AND #lease.sandbox_uid = :pod AND #lease.generation = :generation AND #lease.expires_at > :now "
                "AND orgId = :tenant AND tenantId = :tenant AND ownerUserId = :user AND teamId = :team AND #status = :active "
                "AND #ttl > :now AND #ttl = :previous_ttl AND "
                + ("#mode = :mode" if mode == "persistent" else "(attribute_not_exists(#mode) OR #mode = :mode)")
            ),
            ExpressionAttributeNames={"#lease": "chatLease", "#status": "status", "#ttl": "ttl", "#mode": "sessionMode"},
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
                ":mode": mode,
            },
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ChatAuthorizationRefusedError("chat lease changed") from None
        raise ChatAuthorizationUnavailableError("chat lease unavailable") from None
    except BotoCoreError:
        raise ChatAuthorizationUnavailableError("chat lease unavailable") from None
