"""Owner-only explicit session sharing; tenant equality never grants access.

The ACL is the only sharing mechanism. Members are added by their canonical user
id after a fresh directory check against the session's tenant and team, and every
later read re-checks both the ACL and current membership, so a revocation here or
in the directory takes effect on the next read without waiting for token expiry.
"""

import hashlib
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_authority import ChatRuntimeAuthority
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, Identifier
from src.agentauth.chat_history_store import ChatHistoryStore
from src.orchestration.chat_data_migration import _owner_fields

logger = logging.getLogger("bedrockgateway.agentauth.chat_session_acl")
OPERATION = "session.share"
MAX_ACL_MEMBERS = 100


class ChatSessionAclConflictError(Exception):
    """The ACL version, idempotency key or execution fence changed."""


class AclWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    idempotency_key: Identifier
    expected_version: int = Field(ge=0, le=99_999_999)
    add: list[Identifier] = Field(default_factory=list, max_length=MAX_ACL_MEMBERS)
    remove: list[Identifier] = Field(default_factory=list, max_length=MAX_ACL_MEMBERS)

    @model_validator(mode="after")
    def distinct_change(self):
        if not self.add and not self.remove:
            raise ValueError("an acl write must add or remove at least one member")
        if len(set(self.add)) != len(self.add) or len(set(self.remove)) != len(self.remove) or set(self.add) & set(self.remove):
            raise ValueError("acl members must be distinct and cannot be both added and removed")
        return self


def acl_version(header: dict) -> int:
    version = header.get("aclVersion", 0)
    if isinstance(version, bool) or not isinstance(version, int | Decimal) or int(version) != version or not 0 <= version <= 99_999_999:
        raise ChatAuthorizationUnavailableError("chat acl version unavailable")
    return int(version)


class ChatSessionAclWriter:
    def __init__(self, authority: ChatRuntimeAuthority, history: ChatHistoryStore):
        self.authority = authority
        self.history = history
        self.table = history.table

    def _owned(self, token: str, run_id: str, session_id: str, now: int):
        """Only the bound owner of this very session may read or change its ACL; ACL members cannot."""
        launch = self.history.capabilities.verify(token, run_id=run_id, session_id=session_id, operation=OPERATION, now=now)
        _, header = self.history._authorize(token, run_id, session_id, OPERATION, now)
        if (header["tenantId"], header["teamId"], header["ownerUserId"]) != (launch.tenant_id, launch.team_id, launch.user_id):
            raise ChatAuthorizationRefusedError("chat session sharing requires the owner")
        return launch, header

    def _audit(self, *, launch, session_id: str, operation: str, outcome: str, write: AclWrite | None = None) -> None:
        logger.info(
            "Chat session acl outcome",
            extra={
                "principal": launch.user_id if launch else "unverified",
                "tenant_id": launch.tenant_id if launch else None,
                "run_id": launch.run_id if launch else None,
                "session_id": session_id,
                "operation": operation,
                "added": sorted(write.add) if write else [],
                "removed": sorted(write.remove) if write else [],
                "outcome": outcome,
            },
        )

    def read(self, token: str, *, run_id: str, session_id: str, now: int) -> dict:
        launch, header = self._owned(token, run_id, session_id, now)
        self._audit(launch=launch, session_id=session_id, operation="acl.read", outcome="allowed")
        return {"acl": sorted(header.get("aclUserIds", [])), "version": acl_version(header)}

    def _receipt(self, session_id: str, key: str, digest: str, header: dict) -> dict | None:
        receipt = self.history._get(session_id, key)
        if receipt is None:
            return None
        self.history._check_row(receipt, header)
        if receipt.get("requestDigest") != digest:
            raise ChatSessionAclConflictError("chat acl idempotency key reused")
        result = receipt.get("result")
        if not isinstance(result, dict) or set(result) != {"acl", "version"} or not isinstance(result["acl"], list):
            raise ChatAuthorizationUnavailableError("chat acl receipt unavailable")
        return {"acl": list(result["acl"]), "version": int(result["version"])}

    def write(self, token: str, *, run_id: str, session_id: str, write: AclWrite, now: int) -> dict:
        launch = None
        try:
            launch, header = self._owned(token, run_id, session_id, now)
            result = self._write(token, launch, header, session_id, write, now)
        except ChatSessionAclConflictError:
            self._audit(launch=launch, session_id=session_id, operation="acl.write", outcome="conflict", write=write)
            raise
        except ChatAuthorizationRefusedError:
            self._audit(launch=launch, session_id=session_id, operation="acl.write", outcome="refused", write=write)
            raise
        except Exception:
            self._audit(launch=launch, session_id=session_id, operation="acl.write", outcome="unavailable", write=write)
            raise
        self._audit(launch=launch, session_id=session_id, operation="acl.write", outcome="allowed", write=write)
        return result

    def _write(self, token: str, launch, header: dict, session_id: str, write: AclWrite, now: int) -> dict:
        if launch.user_id in write.add or launch.user_id in write.remove:
            raise ChatAuthorizationRefusedError("chat session owner is not an acl member")
        digest = hashlib.sha256(json.dumps(write.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        receipt_key = "acl#" + hashlib.sha256(write.idempotency_key.encode()).hexdigest()
        receipt = self._receipt(session_id, receipt_key, digest, header)
        if receipt is not None:
            return receipt
        if acl_version(header) != write.expected_version or write.expected_version == 99_999_999:
            raise ChatSessionAclConflictError("chat acl version changed")
        previous = list(header.get("aclUserIds", []))
        # Only current members of the session's own tenant and team may be added; the
        # directory decides, never the model or the requesting user's claim.
        for member in write.add:
            self.history.capabilities._member(launch.tenant_id, member, header["teamId"])
        acl = sorted((set(previous) | set(write.add)) - set(write.remove))
        if len(acl) > MAX_ACL_MEMBERS:
            raise ChatAuthorizationRefusedError("chat acl too large")
        result = {"acl": acl, "version": write.expected_version + 1}
        owner = _owner_fields((launch.tenant_id, launch.team_id, launch.user_id))
        receipt_row = {**owner, "PK": header["PK"], "SK": receipt_key, "requestDigest": digest, "result": result, "runId": launch.run_id}
        return self._commit(token, launch=launch, header=header, previous=previous, result=result, receipt_row=receipt_row, digest=digest, now=now)

    def _commit(self, token, *, launch, header, previous, result, receipt_row, digest, now):
        owner = _owner_fields((launch.tenant_id, launch.team_id, launch.user_id))
        names = {f"#{field}": field for field in owner}
        values = {f":{field}": value for field, value in owner.items()}
        required = {"orgId", "tenantId", "teamId", "ownerUserId"}
        conditions = [
            f"#{field} = :{field}" if field in required else f"(attribute_not_exists(#{field}) OR #{field} = :null OR #{field} = :{field})"
            for field in owner
        ]
        conditions.extend(
            [
                "#status = :active AND #ttl > :now",
                "#lease.run_id = :run AND #lease.sandbox_uid = :pod AND #lease.generation = :generation AND #lease.expires_at > :now",
                "#acl = :previous_acl" if "aclUserIds" in header else "attribute_not_exists(#acl)",
                "#version = :previous_version" if "aclVersion" in header else "attribute_not_exists(#version)",
            ]
        )
        names.update({"#status": "status", "#ttl": "ttl", "#lease": "chatLease", "#acl": "aclUserIds", "#version": "aclVersion"})
        values.update(
            {
                ":null": None,
                ":active": "active",
                ":now": now,
                ":run": launch.run_id,
                ":pod": launch.sandbox_uid,
                ":generation": launch.lease_generation,
                ":acl": result["acl"],
                ":version": result["version"],
                ":stamp": datetime.fromtimestamp(now, UTC).isoformat(),
            }
        )
        if "aclUserIds" in header:
            values[":previous_acl"] = previous
        if "aclVersion" in header:
            values[":previous_version"] = result["version"] - 1
        store = self.authority.store
        instant = datetime.fromtimestamp(now, UTC)
        grant = store.live_grant(invocation_id=launch.run_id, tenant_id=launch.tenant_id, attempt=launch.attempt, now=instant)
        if grant.grant_id != launch.grant_id or grant.revocation_epoch != launch.grant_epoch:
            raise ChatAuthorizationRefusedError("chat grant changed")
        transaction = [
            {"Put": {"TableName": self.table.name, "Item": _encoded(receipt_row), "ConditionExpression": "attribute_not_exists(PK)"}},
            {
                "Update": {
                    "TableName": self.table.name,
                    "Key": _encoded({"PK": header["PK"], "SK": "header"}),
                    "UpdateExpression": "SET #acl = :acl, #version = :version, aclUpdatedAt = :stamp",
                    "ConditionExpression": " AND ".join(conditions),
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": _encoded(values),
                }
            },
            store._authority_check(grant),
            store._grant_check(grant, instant),
            {
                "ConditionCheck": {
                    "TableName": store.table,
                    "Key": _encoded({"pk": f"TENANT#{launch.tenant_id}", "sk": f"EXEC#{launch.run_id}"}),
                    "ConditionExpression": (
                        "#status = :active AND workload_binding = :pod AND current_attempt = :attempt "
                        "AND current_credential_epoch = :epoch AND attribute_not_exists(abort_command_id)"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": _encoded(
                        {":active": "active", ":pod": launch.sandbox_uid, ":attempt": launch.attempt, ":epoch": launch.credential_epoch}
                    ),
                }
            },
        ]
        try:
            store.client.transact_write_items(TransactItems=transaction)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException" and any(
                reason.get("Code") == "ConditionalCheckFailed" for reason in error.response.get("CancellationReasons", [])
            ):
                _, latest = self._owned(token, launch.run_id, launch.session_id, now)
                receipt = self._receipt(launch.session_id, receipt_row["SK"], digest, latest)
                if receipt is not None:
                    return receipt
                raise ChatSessionAclConflictError("chat acl state changed; reread before retry") from None
            raise ChatAuthorizationUnavailableError("chat acl write unavailable") from None
        except BotoCoreError:
            raise ChatAuthorizationUnavailableError("chat acl write unavailable") from None
        return result
