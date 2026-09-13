"""Protected pending dispatch and immutable pod-to-invocation bootstrap."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.execution import ExecutionRecord, ExecutionStatus
from src.agentauth.grants import DelegatedGrant, GrantRefusedError
from src.agentauth.run_credential import mint_credential
from src.agentauth.store import AgentAuthorityStore, AuthorityStoreError, _deserialize_grant
from src.agentauth.workload import VerifiedPod


def envelope_digest(envelope: dict) -> str:
    return hashlib.sha256(json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _iso(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _key(pk: str, sk: str) -> dict:
    return {"pk": {"S": pk}, "sk": {"S": sk}}


class BootstrapRefusedError(Exception):
    """A verified pod could not claim the requested protected dispatch."""


class BootstrapStore:
    def __init__(self, *, table_name: str, dynamodb_client) -> None:
        if not table_name:
            raise AuthorityStoreError("authority table is required")
        self.table = table_name
        self.client = dynamodb_client
        self.authority = AgentAuthorityStore(table_name=table_name, dynamodb_client=dynamodb_client)

    def _read(self, pk: str, sk: str) -> dict | None:
        try:
            return self.client.get_item(TableName=self.table, Key=_key(pk, sk), ConsistentRead=True).get("Item")
        except (ClientError, BotoCoreError):
            raise AuthorityStoreError("authority store unavailable") from None

    def _put(self, item: dict) -> dict:
        return {"Put": {"TableName": self.table, "Item": item, "ConditionExpression": "attribute_not_exists(pk)"}}

    def provision_pending(
        self,
        *,
        envelope: dict,
        grant: DelegatedGrant,
        now: datetime,
        execution_metadata: dict | None = None,
        grant_metadata: dict | None = None,
        events_table: str | None = None,
        event_item: dict | None = None,
    ) -> None:
        """Called only after the trusted ingress validates the human event.

        The authority row is provisioned by that validator, never by bootstrap.
        Repeating an identical committed dispatch is safe; a different envelope
        or grant cannot replace it. Publication happens only after this returns.
        """
        invocation_id = envelope["message_id"]
        tenant_id = envelope["tenant_id"]
        repo = envelope["source_ref"]["repo"]
        arrived_at = envelope["arrived_at"]
        if (
            not all(isinstance(v, str) and v for v in (invocation_id, tenant_id, repo, arrived_at))
            or grant.tenant_id != tenant_id
            or grant.principal != f"{invocation_id}#1"
            or repo not in grant.repo_scope
            or grant.expires_at is None
            or not grant.is_live(now)
            or grant.authority.org_id != tenant_id
        ):
            raise BootstrapRefusedError("invalid protected dispatch")
        digest = envelope_digest(envelope)
        pk = f"TENANT#{tenant_id}"
        execution = {
            **_key(pk, f"EXEC#{invocation_id}"),
            "invocation_id": {"S": invocation_id},
            "tenant_id": {"S": tenant_id},
            "current_attempt": {"N": "1"},
            "status": {"S": "pending"},
            "current_credential_epoch": {"N": "1"},
            "min_acceptable_credential_epoch": {"N": "1"},
            "repo": {"S": repo},
            "arrived_at": {"S": arrived_at},
            "persona": {"S": envelope["persona"]},
            "envelope_digest": {"S": digest},
        }
        if grant.flow_id:
            execution["flow_id"] = {"S": grant.flow_id}
        parent = envelope.get("correlation", {}).get("parent_principal")
        if parent:
            execution["parent_principal"] = {"S": parent}
        grant_item = self._grant_item(grant)
        execution_metadata = execution_metadata or {}
        grant_metadata = grant_metadata or {}
        if set(execution_metadata) - {"issue_number", "installation_id", "chain_depth", "orchestration_node_id", "orchestration_node_attempt"}:
            raise BootstrapRefusedError("invalid dispatch metadata")
        if set(grant_metadata) - {"dispatch_personas", "max_total_dispatches", "work_item_issue"}:
            raise BootstrapRefusedError("invalid grant metadata")
        execution.update(execution_metadata)
        grant_item.update(grant_metadata)
        lookup = {
            **_key(f"INVOCATION#{invocation_id}", "DISPATCH"),
            "tenant_id": {"S": tenant_id},
            "envelope_digest": {"S": digest},
            "grant_digest": {"S": envelope_digest(grant_item)},
            "arrived_at": {"S": arrived_at},
            "execution_metadata_digest": {"S": envelope_digest(execution_metadata)},
        }
        authority_check = self._authority_check(grant)
        transaction = [self._put(execution), self._put(grant_item), self._put(lookup), authority_check]
        if events_table or event_item:
            if (
                not events_table
                or not event_item
                or event_item.get("event_id") != {"S": invocation_id}
                or event_item.get("arrived_at") != {"S": arrived_at}
            ):
                raise BootstrapRefusedError("invalid dispatch event")
            transaction.append({"Put": {"TableName": events_table, "Item": event_item, "ConditionExpression": "attribute_not_exists(event_id)"}})
        try:
            self.client.transact_write_items(TransactItems=transaction)
        except (ClientError, BotoCoreError):
            existing = self._read(f"INVOCATION#{invocation_id}", "DISPATCH")
            if existing != lookup:
                raise BootstrapRefusedError("dispatch conflict or unavailable") from None
            # A committed retry must still have live authority before publishing.
            self.live_grant(invocation_id=invocation_id, tenant_id=tenant_id, attempt=1, now=now)

    @staticmethod
    def _grant_item(grant: DelegatedGrant) -> dict:
        item = {
            **_key(f"TENANT#{grant.tenant_id}", f"GRANT#{grant.principal}"),
            "grant_id": {"S": grant.grant_id},
            "tenant_id": {"S": grant.tenant_id},
            "principal": {"S": grant.principal},
            "authority_kind": {"S": grant.authority.kind},
            "authority_reference_id": {"S": grant.authority.reference_id},
            "authority_human_id": {"S": grant.authority.human_id},
            "authority_org_id": {"S": grant.authority.org_id},
            "revocation_epoch": {"N": str(grant.revocation_epoch)},
            "revoked": {"BOOL": grant.revoked},
            "max_dispatch_concurrency": {"N": str(grant.max_dispatch_concurrency)},
            "max_chain_depth": {"N": str(grant.max_chain_depth)},
        }
        for name in ("allowed_actions", "target_run_ids", "target_relationships", "repo_scope", "delegable_actions"):
            values = sorted(str(v) for v in getattr(grant, name))
            if values:
                item[name] = {"SS": values}
        if grant.flow_id:
            item["flow_id"] = {"S": grant.flow_id}
        if grant.expires_at is not None:
            item["expires_at"] = {"S": _iso(grant.expires_at)}
        return item

    def _authority_check(self, grant: DelegatedGrant) -> dict:
        return {
            "ConditionCheck": {
                "TableName": self.table,
                "Key": _key(f"TENANT#{grant.tenant_id}", f"AUTHORITY#{grant.authority.reference_id}"),
                "ConditionExpression": (
                    "#st = :active AND human_id = :human AND authority_kind = :kind AND (attribute_not_exists(expires_at) OR expires_at > :now)"
                ),
                "ExpressionAttributeNames": {"#st": "status"},
                "ExpressionAttributeValues": {
                    ":active": {"S": "active"},
                    ":human": {"S": grant.authority.human_id},
                    ":kind": {"S": grant.authority.kind},
                    ":now": {"S": _iso(datetime.now(UTC))},
                },
            }
        }

    def _grant_check(self, grant: DelegatedGrant, now: datetime) -> dict:
        return {
            "ConditionCheck": {
                "TableName": self.table,
                "Key": _key(f"TENANT#{grant.tenant_id}", f"GRANT#{grant.principal}"),
                "ConditionExpression": "revoked = :false AND revocation_epoch = :epoch AND expires_at > :now",
                "ExpressionAttributeValues": {":false": {"BOOL": False}, ":epoch": {"N": str(grant.revocation_epoch)}, ":now": {"S": _iso(now)}},
            }
        }

    def live_grant(
        self, *, invocation_id: str, tenant_id: str, attempt: int, now: datetime, _ancestors: frozenset[str] = frozenset()
    ) -> DelegatedGrant:
        if invocation_id in _ancestors or len(_ancestors) > 8:
            raise BootstrapRefusedError("delegation lineage refused")
        grant = self.authority.load_grant(principal=f"{invocation_id}#{attempt}", tenant_id=tenant_id)
        if (
            grant is None
            or grant.expires_at is None
            or not grant.is_live(now)
            or grant.tenant_id != tenant_id
            or grant.principal != f"{invocation_id}#{attempt}"
            or grant.authority.org_id != tenant_id
        ):
            raise BootstrapRefusedError("authority refused")
        authority = self._read(f"TENANT#{tenant_id}", f"AUTHORITY#{grant.authority.reference_id}")
        if (
            authority is None
            or authority.get("status") != {"S": "active"}
            or authority.get("human_id") != {"S": grant.authority.human_id}
            or authority.get("authority_kind") != {"S": grant.authority.kind}
        ):
            raise BootstrapRefusedError("authority refused")
        if "expires_at" in authority:
            try:
                expires = datetime.strptime(authority["expires_at"]["S"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            except (KeyError, TypeError, ValueError):
                raise BootstrapRefusedError("authority refused") from None
            if expires <= now:
                raise BootstrapRefusedError("authority refused")
        raw_grant = self._read(f"TENANT#{tenant_id}", f"GRANT#{grant.principal}") or {}
        try:
            if _deserialize_grant(raw_grant) != grant:
                raise BootstrapRefusedError("authority changed during validation")
        except (GrantRefusedError, KeyError, TypeError, ValueError):
            raise BootstrapRefusedError("authority unavailable") from None
        if "launch_authority_reference_id" in raw_grant:
            try:
                launch = self._read(f"TENANT#{tenant_id}", f"AUTHORITY#{raw_grant['launch_authority_reference_id']['S']}")
                if (
                    not launch
                    or launch.get("status") != {"S": "active"}
                    or launch.get("human_id") != raw_grant["launch_authority_human_id"]
                    or launch.get("authority_kind") != raw_grant["launch_authority_kind"]
                ):
                    raise BootstrapRefusedError("coordinator launch revoked")
                if "expires_at" in launch:
                    launch_expiry = datetime.strptime(launch["expires_at"]["S"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
                    if launch_expiry <= now:
                        raise BootstrapRefusedError("coordinator launch expired")
            except (KeyError, TypeError, ValueError):
                raise BootstrapRefusedError("coordinator launch unavailable") from None
        execution = self._read(f"TENANT#{tenant_id}", f"EXEC#{invocation_id}")
        if execution and "parent_grant_id" in execution:
            try:
                parent_id, parent_attempt = execution["parent_principal"]["S"].rsplit("#", 1)
                parent = self._read(f"TENANT#{tenant_id}", f"EXEC#{parent_id}")
                if not parent or parent.get("status", {}).get("S") in {"cancelled", "revoked"}:
                    raise BootstrapRefusedError("parent delegation refused")
                parent_grant = self.live_grant(
                    invocation_id=parent_id,
                    tenant_id=tenant_id,
                    attempt=int(parent_attempt),
                    now=now,
                    _ancestors=_ancestors | {invocation_id},
                )
                if (
                    parent_grant.grant_id != execution["parent_grant_id"]["S"]
                    or parent_grant.revocation_epoch != int(execution["parent_grant_epoch"]["N"])
                    or parent_grant.authority != grant.authority
                    or not grant.allowed_actions <= parent_grant.delegable_actions
                ):
                    raise BootstrapRefusedError("parent delegation changed")
            except (KeyError, TypeError, ValueError):
                raise BootstrapRefusedError("parent delegation unavailable") from None
        return grant

    def bind(self, *, invocation_id: str, digest: str, pod: VerifiedPod, now: datetime) -> ExecutionRecord:
        lookup = self._read(f"INVOCATION#{invocation_id}", "DISPATCH")
        if lookup is None or lookup.get("envelope_digest") != {"S": digest}:
            raise BootstrapRefusedError("bootstrap refused")
        tenant_id = lookup["tenant_id"]["S"]
        record = self.authority.load_execution(invocation_id=invocation_id, tenant_id=tenant_id)
        if record is None:
            raise BootstrapRefusedError("bootstrap refused")
        grant = self.live_grant(invocation_id=invocation_id, tenant_id=tenant_id, attempt=record.current_attempt, now=now)
        pod_item = {
            **_key(f"POD#{pod.uid}", "BINDING"),
            "invocation_id": {"S": invocation_id},
            "tenant_id": {"S": tenant_id},
            "attempt": {"N": str(record.current_attempt)},
        }
        existing = self._read(f"POD#{pod.uid}", "BINDING")
        if existing is not None:
            if existing != pod_item or record.workload_binding != pod.uid or record.status != ExecutionStatus.ACTIVE:
                raise BootstrapRefusedError("bootstrap refused")
            return record
        if record.status != ExecutionStatus.PENDING or record.workload_binding is not None:
            raise BootstrapRefusedError("bootstrap refused")
        try:
            self.client.transact_write_items(
                TransactItems=[
                    self._put(pod_item),
                    {
                        "Update": {
                            "TableName": self.table,
                            "Key": _key(f"TENANT#{tenant_id}", f"EXEC#{invocation_id}"),
                            "UpdateExpression": "SET #st = :active, workload_binding = :pod, pod_name = :name, pod_ip = :ip",
                            "ConditionExpression": (
                                "#st = :pending AND current_attempt = :attempt "
                                "AND attribute_not_exists(workload_binding) AND envelope_digest = :digest"
                            ),
                            "ExpressionAttributeNames": {"#st": "status"},
                            "ExpressionAttributeValues": {
                                ":active": {"S": "active"},
                                ":pending": {"S": "pending"},
                                ":attempt": {"N": str(record.current_attempt)},
                                ":pod": {"S": pod.uid},
                                ":name": {"S": pod.name},
                                ":ip": {"S": pod.ip},
                                ":digest": {"S": digest},
                            },
                        }
                    },
                    self._grant_check(grant, now),
                    self._authority_check(grant),
                ]
            )
        except (ClientError, BotoCoreError):
            # Recover an ambiguous commit only if both sides of the immutable
            # binding match. Never reset a pod binding or supersede an attempt.
            if self._read(f"POD#{pod.uid}", "BINDING") != pod_item:
                raise BootstrapRefusedError("bootstrap conflict or unavailable") from None
        expected_attempt = record.current_attempt
        record = self.authority.load_execution(invocation_id=invocation_id, tenant_id=tenant_id)
        if (
            record is None
            or record.status != ExecutionStatus.ACTIVE
            or record.workload_binding != pod.uid
            or record.current_attempt != expected_attempt
        ):
            raise BootstrapRefusedError("bootstrap refused")
        self.live_grant(invocation_id=invocation_id, tenant_id=tenant_id, attempt=record.current_attempt, now=now)
        return record


def issue_bound_credential(record: ExecutionRecord, *, now: datetime, env: dict[str, str] | None = None) -> dict:
    token = mint_credential(
        invocation_id=record.invocation_id,
        tenant_id=record.tenant_id,
        attempt=record.current_attempt,
        credential_epoch=record.current_credential_epoch,
        flow_id=record.flow_id,
        now=now,
        env=env,
    )
    return {
        "credential": token,
        "invocation_id": record.invocation_id,
        "attempt": record.current_attempt,
        "credential_epoch": record.current_credential_epoch,
        "expires_in": 900,
    }
