"""Lease-scoped run rows for persistent chat model attribution."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from src.crew.targets import TargetKind


class CrewRunConflictError(Exception):
    pass


class CrewRunStore:
    def __init__(self, table, *, host_role_arn=None):
        self.table = table
        self.host_role_arn = host_role_arn

    def mint(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        lease_generation: int,
        lease_expires_at: int,
        target_kind: TargetKind,
        target_ref: str,
        now: int,
        creation_update: dict | None = None,
        deployment_id: str | None = None,
        executor_scope: str = "session",
        provisioning: bool = False,
        owner_lease_check: dict | None = None,
    ) -> str:
        if not all((tenant_id, user_id, session_id, target_ref)) or target_kind not in ("eks-pod", "ec2-instance"):
            raise ValueError("crew run owner and target required")
        if lease_generation < 1 or lease_expires_at <= now:
            raise ValueError("crew run requires a live lease")
        if target_kind == "ec2-instance" and not deployment_id:
            raise ValueError("EC2 runs require the recorded deployment")
        if target_kind == "ec2-instance" and not self.host_role_arn:
            raise ValueError("EC2 runs require the approved guest role")
        if executor_scope not in ("session", "user") or (target_kind == "ec2-instance" and executor_scope != "session"):
            raise ValueError("unsupported Crew executor scope")
        if provisioning and not (
            (executor_scope == "user" and target_kind == "eks-pod") or (executor_scope == "session" and target_kind == "ec2-instance")
        ):
            raise ValueError("only a user-scoped pod or session-scoped EC2 host can prepare a Crew binding")
        run_id = str(uuid4())
        arrived_at = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        marker = {
            "event_id": f"crew-lease#{tenant_id}#{session_id}#{lease_generation}",
            "arrived_at": "crew",
            "run_id": run_id,
            "run_arrived_at": arrived_at,
            "status": "provisioning" if provisioning else "active",
            "expires_at": lease_expires_at + 86400,
        }
        owner_marker = {
            "event_id": f"crew-owner#{tenant_id}#{user_id}",
            "arrived_at": "crew",
            "run_id": run_id,
            "lease_expires_at": lease_expires_at,
            "executor_scope": executor_scope,
            "generation": lease_generation,
            "target_kind": target_kind,
            "target_ref": target_ref,
            "session_count": 1,
            "expires_at": lease_expires_at + 86400,
        }
        row = {
            "event_id": run_id,
            "arrived_at": arrived_at,
            "run_id": run_id,
            "kind": "crew",
            "root_human_id": user_id,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "lease_generation": lease_generation,
            "executor_scope": executor_scope,
            "lease_expires_at": lease_expires_at,
            "target_kind": target_kind,
            "target_ref": target_ref,
            "is_human_rooted": True,
            "status": "provisioning" if provisioning else "active",
            "expires_at": lease_expires_at + 86400,
        }
        if deployment_id is not None:
            row["deployment_id"] = deployment_id
        if target_kind == "ec2-instance":
            row["host_role_arn"] = self.host_role_arn
        reservation = []
        if creation_update is not None:
            reservation = [
                {
                    "Update": {
                        **creation_update,
                        "UpdateExpression": "SET crew_run_id = :crew_run_id, crew_lease_generation = :crew_generation",
                        "ExpressionAttributeValues": {
                            **creation_update["ExpressionAttributeValues"],
                            ":crew_run_id": run_id,
                            ":crew_generation": lease_generation,
                        },
                    }
                }
            ]
        try:
            self.table.meta.client.transact_write_items(
                TransactItems=[
                    *reservation,
                    *([{"ConditionCheck": owner_lease_check}] if owner_lease_check is not None else []),
                    *(
                        [
                            {
                                "Put": {
                                    "TableName": self.table.name,
                                    "Item": owner_marker,
                                    "ConditionExpression": "attribute_not_exists(event_id) OR lease_expires_at <= :now",
                                    "ExpressionAttributeValues": {":now": now},
                                }
                            }
                        ]
                        if executor_scope == "user"
                        else []
                    ),
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": marker,
                            "ConditionExpression": "attribute_not_exists(event_id)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": row,
                            "ConditionExpression": "attribute_not_exists(event_id)",
                        }
                    },
                ]
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException" and any(
                reason.get("Code") == "ConditionalCheckFailed" for reason in exc.response.get("CancellationReasons", [])
            ):
                raise CrewRunConflictError("crew lease already bound") from exc
            raise
        return run_id

    def activate_for_host(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        lease_generation: int,
        target_ref: str,
        deployment_id: str,
        now: int,
    ) -> None:
        if not all((run_id, tenant_id, user_id, session_id, target_ref, deployment_id, self.host_role_arn)) or lease_generation < 1:
            raise ValueError("Crew host activation requires a recorded owner, deployment and guest role")
        row = self.read_run(run_id)
        if (
            row is None
            or row.get("status") != "provisioning"
            or row.get("kind") != "crew"
            or row.get("executor_scope") != "session"
            or row.get("tenant_id") != tenant_id
            or row.get("user_id") != user_id
            or row.get("root_human_id") != user_id
            or row.get("session_id") != session_id
            or row.get("lease_generation") != lease_generation
            or row.get("target_kind") != "ec2-instance"
            or row.get("target_ref") != target_ref
            or row.get("deployment_id") != deployment_id
            or row.get("host_role_arn") != self.host_role_arn
            or row.get("lease_expires_at", 0) <= now
        ):
            raise CrewRunConflictError("prepared Crew host run changed")
        marker = {"event_id": f"crew-lease#{tenant_id}#{session_id}#{lease_generation}", "arrived_at": "crew"}
        values = {
            ":prepared": "provisioning",
            ":active": "active",
            ":run": run_id,
            ":arrived": row["arrived_at"],
            ":tenant": tenant_id,
            ":user": user_id,
            ":session": session_id,
            ":generation": lease_generation,
            ":kind": "crew",
            ":scope": "session",
            ":target_kind": "ec2-instance",
            ":target": target_ref,
            ":deployment": deployment_id,
            ":role": self.host_role_arn,
            ":now": now,
        }
        try:
            self.table.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": marker,
                            "UpdateExpression": "SET #status = :active",
                            "ConditionExpression": ("#status = :prepared AND run_id = :run AND run_arrived_at = :arrived AND expires_at > :now"),
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": {key: values[key] for key in (":prepared", ":active", ":run", ":arrived", ":now")},
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": {"event_id": run_id, "arrived_at": row["arrived_at"]},
                            "UpdateExpression": "SET #status = :active",
                            "ConditionExpression": (
                                "#status = :prepared AND #kind = :kind AND executor_scope = :scope "
                                "AND tenant_id = :tenant AND user_id = :user AND root_human_id = :user "
                                "AND session_id = :session AND lease_generation = :generation "
                                "AND target_kind = :target_kind AND target_ref = :target "
                                "AND deployment_id = :deployment AND host_role_arn = :role "
                                "AND lease_expires_at > :now"
                            ),
                            "ExpressionAttributeNames": {"#status": "status", "#kind": "kind"},
                            # Only the placeholders this item's expressions use: DynamoDB rejects a
                            # transaction whose item declares an unused value (ValidationException
                            # "Value provided in ExpressionAttributeValues unused in expressions"),
                            # which is what every GET /crew/target hit once a Dev Box host became
                            # ready on dev 2026-10-19 (#8223). moto does not enforce this.
                            "ExpressionAttributeValues": {key: value for key, value in values.items() if key not in (":run", ":arrived")},
                        }
                    },
                ]
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException" and any(
                reason.get("Code") == "ConditionalCheckFailed" for reason in exc.response.get("CancellationReasons", [])
            ):
                raise CrewRunConflictError("Crew host lease changed during activation") from exc
            raise

    def activate_for_pod(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        lease_generation: int,
        target_ref: str,
        pod_uid: str,
        now: int,
        owner_lease_expires_at: int,
        lease_check: dict,
        creation_check: dict,
        session_check: dict,
    ) -> None:
        if (
            not all((run_id, tenant_id, user_id, session_id, target_ref, pod_uid, lease_check, creation_check, session_check))
            or lease_generation < 1
            or owner_lease_expires_at <= now
        ):
            raise ValueError("Crew activation requires a verified pod and owner")
        row = self.read_run(run_id)
        if (
            row is None
            or row.get("status") != "provisioning"
            or row.get("kind") != "crew"
            or row.get("executor_scope") != "user"
            or row.get("tenant_id") != tenant_id
            or row.get("user_id") != user_id
            or row.get("root_human_id") != user_id
            or row.get("session_id") != session_id
            or row.get("lease_generation") != lease_generation
            or row.get("target_kind") != "eks-pod"
            or row.get("target_ref") != target_ref
            or row.get("lease_expires_at", 0) <= now
        ):
            raise CrewRunConflictError("prepared Crew run changed")
        expires_at = min(int(row["lease_expires_at"]), owner_lease_expires_at)
        try:
            self.table.meta.client.transact_write_items(
                TransactItems=[
                    {"ConditionCheck": lease_check},
                    {"ConditionCheck": creation_check},
                    {"ConditionCheck": session_check},
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": {"event_id": f"crew-owner#{tenant_id}#{user_id}", "arrived_at": "crew"},
                            "UpdateExpression": "SET pod_uid = :uid, lease_expires_at = :expires, expires_at = :ttl",
                            "ConditionExpression": (
                                "run_id = :run AND executor_scope = :scope AND generation = :generation "
                                "AND target_kind = :kind AND target_ref = :target AND lease_expires_at > :now "
                                "AND session_count = :one AND attribute_not_exists(pod_uid)"
                            ),
                            "ExpressionAttributeValues": {
                                ":run": run_id,
                                ":scope": "user",
                                ":generation": lease_generation,
                                ":kind": "eks-pod",
                                ":target": target_ref,
                                ":now": now,
                                ":one": 1,
                                ":uid": pod_uid,
                                ":expires": expires_at,
                                ":ttl": expires_at + 86400,
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": {"event_id": run_id, "arrived_at": row["arrived_at"]},
                            "UpdateExpression": "SET #status = :active, pod_uid = :uid, lease_expires_at = :expires, expires_at = :ttl",
                            "ConditionExpression": (
                                "#status = :prepared AND #kind = :crew AND tenant_id = :tenant AND user_id = :user "
                                "AND session_id = :session AND lease_generation = :generation AND target_ref = :target "
                                "AND lease_expires_at > :now AND attribute_not_exists(pod_uid)"
                            ),
                            "ExpressionAttributeNames": {"#status": "status", "#kind": "kind"},
                            "ExpressionAttributeValues": {
                                ":active": "active",
                                ":prepared": "provisioning",
                                ":crew": "crew",
                                ":tenant": tenant_id,
                                ":user": user_id,
                                ":session": session_id,
                                ":generation": lease_generation,
                                ":target": target_ref,
                                ":now": now,
                                ":uid": pod_uid,
                                ":expires": expires_at,
                                ":ttl": expires_at + 86400,
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": {"event_id": f"crew-lease#{tenant_id}#{session_id}#{lease_generation}", "arrived_at": "crew"},
                            "UpdateExpression": "SET #status = :active, expires_at = :ttl",
                            "ConditionExpression": "#status = :prepared AND run_id = :run",
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": {
                                ":active": "active",
                                ":prepared": "provisioning",
                                ":run": run_id,
                                ":ttl": expires_at + 86400,
                            },
                        }
                    },
                ]
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException" and any(
                reason.get("Code") == "ConditionalCheckFailed" for reason in exc.response.get("CancellationReasons", [])
            ):
                raise CrewRunConflictError("prepared Crew run changed during activation") from exc
            raise

    def attach_session(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        lease_generation: int,
        target_kind: TargetKind,
        target_ref: str,
        now: int,
        creation_update: dict | None = None,
        max_sessions: int = 20,
        owner_lease_check: dict | None = None,
        pod_uid: str | None = None,
    ) -> str:
        if (
            not all((tenant_id, user_id, session_id, target_ref))
            or lease_generation < 1
            or not isinstance(max_sessions, int)
            or isinstance(max_sessions, bool)
            or not 1 <= max_sessions <= 20
        ):
            raise ValueError("Crew attachment requires an owner, session and generation")
        owner_key = {"event_id": f"crew-owner#{tenant_id}#{user_id}", "arrived_at": "crew"}
        owner = self.table.get_item(Key=owner_key, ConsistentRead=True).get("Item")
        run_id = owner.get("run_id") if owner else None
        row = self.read_run(run_id) if isinstance(run_id, str) else None
        if (
            owner is None
            or row is None
            or owner.get("executor_scope") != "user"
            or owner.get("generation") != lease_generation
            or owner.get("target_kind") != target_kind
            or owner.get("target_ref") != target_ref
            or owner.get("lease_expires_at", 0) <= now
            or owner.get("session_count", 1) >= max_sessions
            or row.get("status") != "active"
            or row.get("executor_scope") != "user"
            or row.get("tenant_id") != tenant_id
            or row.get("user_id") != user_id
            or row.get("root_human_id") != user_id
            or row.get("session_id") == session_id
            or row.get("lease_generation") != lease_generation
            or row.get("target_kind") != target_kind
            or row.get("target_ref") != target_ref
            or row.get("lease_expires_at", 0) <= now
            or (
                owner_lease_check is not None
                and target_kind == "eks-pod"
                and (not pod_uid or owner.get("pod_uid") != pod_uid or row.get("pod_uid") != pod_uid)
            )
        ):
            raise CrewRunConflictError("Crew owner lease unavailable")
        pod_guard = owner_lease_check is not None and target_kind == "eks-pod"
        session_marker = {
            "event_id": f"crew-lease#{tenant_id}#{session_id}#{lease_generation}",
            "arrived_at": "crew",
            "run_id": run_id,
            "run_arrived_at": row["arrived_at"],
            "status": "active",
            "expires_at": min(owner["lease_expires_at"], row["lease_expires_at"]) + 86400,
        }
        reservation = []
        if creation_update is not None:
            reservation = [
                {
                    "Update": {
                        **creation_update,
                        "UpdateExpression": "SET crew_run_id = :crew_run_id, crew_lease_generation = :crew_generation",
                        "ExpressionAttributeValues": {
                            **creation_update["ExpressionAttributeValues"],
                            ":crew_run_id": run_id,
                            ":crew_generation": lease_generation,
                        },
                    }
                }
            ]
        try:
            self.table.meta.client.transact_write_items(
                TransactItems=[
                    *reservation,
                    *([{"ConditionCheck": owner_lease_check}] if owner_lease_check is not None else []),
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": owner_key,
                            "UpdateExpression": "SET session_count = if_not_exists(session_count, :one) + :one",
                            "ConditionExpression": (
                                "run_id = :run AND executor_scope = :scope AND generation = :generation "
                                "AND target_kind = :kind AND target_ref = :target AND lease_expires_at > :now "
                                + ("AND pod_uid = :pod_uid " if pod_guard else "")
                                + "AND (attribute_not_exists(session_count) OR session_count < :max)"
                            ),
                            "ExpressionAttributeValues": {
                                ":run": run_id,
                                ":scope": "user",
                                ":generation": lease_generation,
                                ":kind": target_kind,
                                ":target": target_ref,
                                ":now": now,
                                ":one": 1,
                                ":max": max_sessions,
                                **({":pod_uid": pod_uid} if pod_guard else {}),
                            },
                        }
                    },
                    {
                        "ConditionCheck": {
                            "TableName": self.table.name,
                            "Key": {"event_id": run_id, "arrived_at": row["arrived_at"]},
                            "ConditionExpression": (
                                "#status = :active AND executor_scope = :scope AND tenant_id = :tenant "
                                "AND user_id = :user AND lease_generation = :generation AND lease_expires_at > :now"
                                + (" AND pod_uid = :pod_uid" if pod_guard else "")
                            ),
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": {
                                ":active": "active",
                                ":scope": "user",
                                ":tenant": tenant_id,
                                ":user": user_id,
                                ":generation": lease_generation,
                                ":now": now,
                                **({":pod_uid": pod_uid} if pod_guard else {}),
                            },
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": session_marker,
                            "ConditionExpression": "attribute_not_exists(event_id)",
                        }
                    },
                ]
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException" and any(
                reason.get("Code") == "ConditionalCheckFailed" for reason in exc.response.get("CancellationReasons", [])
            ):
                raise CrewRunConflictError("Crew owner lease changed during attachment") from exc
            raise
        return run_id

    def renew_for_lease(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        lease_generation: int,
        target_ref: str,
        run_id: str,
        lease_expires_at: int,
        now: int,
    ) -> None:
        row = self.read_run(run_id)
        if (
            row is None
            or row.get("kind") != "crew"
            or row.get("target_kind") != "ec2-instance"
            or row.get("status") != "active"
            or row.get("tenant_id") != tenant_id
            or row.get("user_id") != user_id
            or row.get("root_human_id") != user_id
            or row.get("session_id") != session_id
            or row.get("lease_generation") != lease_generation
            or row.get("target_ref") != target_ref
            or row.get("lease_expires_at", 0) <= now
            or lease_expires_at <= now
        ):
            raise CrewRunConflictError("host lease is no longer current")
        marker = {"event_id": f"crew-lease#{tenant_id}#{session_id}#{lease_generation}", "arrived_at": "crew"}
        owner_marker = {"event_id": f"crew-owner#{tenant_id}#{user_id}", "arrived_at": "crew"}
        try:
            self.table.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": {"event_id": run_id, "arrived_at": row["arrived_at"]},
                            "UpdateExpression": "SET lease_expires_at = :expires, expires_at = :ttl",
                            "ConditionExpression": "#kind = :crew AND #status = :active AND lease_expires_at > :now AND target_ref = :target",
                            "ExpressionAttributeNames": {"#kind": "kind", "#status": "status"},
                            "ExpressionAttributeValues": {
                                ":crew": "crew",
                                ":active": "active",
                                ":expires": lease_expires_at,
                                ":ttl": lease_expires_at + 86400,
                                ":now": now,
                                ":target": target_ref,
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table.name,
                            "Key": marker,
                            "UpdateExpression": "SET expires_at = :ttl",
                            "ConditionExpression": "run_id = :run",
                            "ExpressionAttributeValues": {":ttl": lease_expires_at + 86400, ":run": run_id},
                        }
                    },
                    *(
                        [
                            {
                                "Update": {
                                    "TableName": self.table.name,
                                    "Key": owner_marker,
                                    "UpdateExpression": "SET lease_expires_at = :expires, expires_at = :ttl",
                                    "ConditionExpression": "run_id = :run AND lease_expires_at > :now",
                                    "ExpressionAttributeValues": {
                                        ":expires": lease_expires_at,
                                        ":ttl": lease_expires_at + 86400,
                                        ":now": now,
                                        ":run": run_id,
                                    },
                                }
                            }
                        ]
                        if row.get("executor_scope") == "user"
                        else []
                    ),
                ]
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException" and any(
                reason.get("Code") == "ConditionalCheckFailed" for reason in exc.response.get("CancellationReasons", [])
            ):
                raise CrewRunConflictError("host lease changed during renewal") from exc
            raise

    def read_run(self, run_id: str) -> dict | None:
        response = self.table.query(KeyConditionExpression=Key("event_id").eq(run_id), ScanIndexForward=False, ConsistentRead=True, Limit=1)
        return next(iter(response.get("Items", [])), None)

    def revoke_for_lease(self, *, tenant_id: str, session_id: str, lease_generation: int, expected_run_id: str | None = None) -> bool:
        for attempt in range(3):
            try:
                return self._revoke_for_lease_once(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    lease_generation=lease_generation,
                    expected_run_id=expected_run_id,
                )
            except CrewRunConflictError:
                if attempt == 2:
                    raise
        raise CrewRunConflictError("crew lease changed during revocation")

    def _revoke_for_lease_once(self, *, tenant_id: str, session_id: str, lease_generation: int, expected_run_id: str | None) -> bool:
        marker_key = {"event_id": f"crew-lease#{tenant_id}#{session_id}#{lease_generation}", "arrived_at": "crew"}
        marker = self.table.get_item(Key=marker_key, ConsistentRead=True).get("Item")
        if marker is None:
            return False
        run_id = marker["run_id"]
        if expected_run_id is not None and run_id != expected_run_id:
            raise CrewRunConflictError("crew lease changed during revocation")
        row_key = {"event_id": run_id, "arrived_at": marker.get("run_arrived_at", "crew")}
        row = self.table.get_item(Key=row_key, ConsistentRead=True).get("Item")
        if row is None:
            self.table.delete_item(Key=marker_key, ConditionExpression="run_id = :run", ExpressionAttributeValues={":run": run_id})
            return False
        owner_key = {"event_id": f"crew-owner#{tenant_id}#{row['user_id']}", "arrived_at": "crew"}
        user_scoped = row.get("executor_scope") == "user"
        count = 1
        if user_scoped:
            owner = self.table.get_item(Key=owner_key, ConsistentRead=True).get("Item")
            if owner is None or owner.get("run_id") != run_id or owner.get("generation") != lease_generation:
                raise CrewRunConflictError("crew owner lease changed during revocation")
            count = owner.get("session_count", 1)
            if isinstance(count, bool) or not isinstance(count, int | Decimal) or count < 1:
                raise CrewRunConflictError("crew session count unavailable")
        row_condition = "#kind = :crew AND tenant_id = :tenant AND lease_generation = :generation"
        row_values = {":crew": "crew", ":tenant": tenant_id, ":generation": lease_generation}
        if user_scoped:
            row_condition += " AND user_id = :user AND executor_scope = :scope"
            row_values.update({":user": row["user_id"], ":scope": "user"})
        else:
            row_condition += " AND session_id = :session"
            row_values[":session"] = session_id
        row_action = {
            "TableName": self.table.name,
            "Key": row_key,
            "ConditionExpression": row_condition,
            "ExpressionAttributeNames": {"#kind": "kind"},
            "ExpressionAttributeValues": row_values,
        }
        owner_action = []
        if user_scoped and count > 1:
            owner_action = [
                {
                    "Update": {
                        "TableName": self.table.name,
                        "Key": owner_key,
                        "UpdateExpression": "ADD session_count :minus_one",
                        "ConditionExpression": "run_id = :run AND generation = :generation AND session_count > :one",
                        "ExpressionAttributeValues": {":run": run_id, ":generation": lease_generation, ":minus_one": -1, ":one": 1},
                    }
                }
            ]
        elif user_scoped:
            owner_action = [
                {
                    "Delete": {
                        "TableName": self.table.name,
                        "Key": owner_key,
                        "ConditionExpression": (
                            "run_id = :run AND generation = :generation AND (session_count = :one OR attribute_not_exists(session_count))"
                        ),
                        "ExpressionAttributeValues": {":run": run_id, ":generation": lease_generation, ":one": 1},
                    }
                }
            ]
        try:
            self.table.meta.client.transact_write_items(
                TransactItems=[
                    {"ConditionCheck" if user_scoped and count > 1 else "Delete": row_action},
                    {
                        "Delete": {
                            "TableName": self.table.name,
                            "Key": marker_key,
                            "ConditionExpression": "run_id = :run",
                            "ExpressionAttributeValues": {":run": run_id},
                        }
                    },
                    *owner_action,
                ]
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException" and any(
                reason.get("Code") == "ConditionalCheckFailed" for reason in exc.response.get("CancellationReasons", [])
            ):
                raise CrewRunConflictError("crew lease changed during revocation") from exc
            raise
        return True


def current_crew_store() -> CrewRunStore:
    from src.budget.run_binding import _get_dynamodb_table
    from src.shared.config import get_settings

    settings = get_settings()
    return CrewRunStore(_get_dynamodb_table(settings.webhook_events_table, settings.aws_region))
