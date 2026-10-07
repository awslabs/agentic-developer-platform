"""Durable sandbox outcomes, not terminal delivery or proof of teardown."""

from datetime import UTC, datetime
from typing import Literal

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, model_validator

from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, Identifier
from src.agentauth.chat_history_write import ChatHistoryConflictError, ChatHistoryWriter
from src.agentauth.chat_session_mailbox import ChatSessionMailbox


class TurnResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    outcome: Literal["completed", "failed"]
    message_id: Identifier | None = None

    @model_validator(mode="after")
    def consistent_outcome(self):
        if (self.outcome == "completed") != (self.message_id is not None):
            raise ValueError("only successful outcomes reference an assistant message")
        return self


class ChatTurnResultWriter(ChatHistoryWriter):
    def commit(self, token, *, run_id, session_id, result: TurnResult, now):
        launch, header = self.history._authorize(token, run_id, session_id, "turn.result", now)
        if session_id != launch.session_id or (header["tenantId"], header["teamId"], header["ownerUserId"]) != (
            launch.tenant_id,
            launch.team_id,
            launch.user_id,
        ):
            raise ChatAuthorizationRefusedError("chat result ownership refused")
        user_turn, accepted = self._accepted(launch)
        receipt = accepted[1]
        candidate = {
            "run_id": launch.run_id,
            "session_id": launch.session_id,
            "attempt": launch.attempt,
            "lease_generation": launch.lease_generation,
            "sandbox_uid": launch.sandbox_uid,
            "outcome": result.outcome,
            "message_id": result.message_id,
            "terminal": False,
        }
        if receipt.get("status") != "accepted":
            raise ChatAuthorizationRefusedError("chat result turn ended")
        existing = receipt.get("result_candidate")
        if existing is not None:
            if existing != candidate:
                raise ChatHistoryConflictError("chat result already recorded")
            return candidate
        checks = [self._unchanged(header), self._unchanged(accepted[0])]
        mode = header.get("sessionMode", "ephemeral")
        if mode not in ("ephemeral", "persistent"):
            raise ChatAuthorizationUnavailableError("chat session mode unavailable")
        if mode == "persistent":
            user_message = self.history._get(session_id, f"msg#{receipt['ref']}")
            if user_message is None:
                raise ChatAuthorizationRefusedError("chat initial message unavailable")
            self.history._check_row(user_message, header)
            if (
                user_message.get("role") != "user"
                or user_message.get("runId") != run_id
                or user_message.get("leaseGeneration") != launch.lease_generation
            ):
                raise ChatAuthorizationRefusedError("chat initial message changed")
            sequence = ChatSessionMailbox(self.table).initial_cursor(
                session_id=session_id,
                owner=(launch.tenant_id, launch.team_id, launch.user_id),
                run_id=run_id,
                sandbox_uid=launch.sandbox_uid,
                generation=launch.lease_generation,
                message=user_message["content"],
                now=now,
            )
            mailbox_entry = self.history._get(session_id, f"mailbox#{sequence:08d}")
            if mailbox_entry is None or mailbox_entry.get("status") != "queued":
                raise ChatAuthorizationRefusedError("chat initial mailbox turn unavailable")
            mailbox_update = self._unchanged(mailbox_entry)["ConditionCheck"]
            mailbox_update["UpdateExpression"] = "SET #status = :outcome, resultRecordedAt = :now"
            mailbox_update["ExpressionAttributeNames"]["#status"] = "status"
            mailbox_update["ExpressionAttributeValues"].update(_encoded({":outcome": "result_recorded", ":now": now}))
            checks.append({"Update": mailbox_update})
        if result.message_id is not None:
            message = self.history._get(session_id, f"msg#{result.message_id}")
            if message is None:
                raise ChatAuthorizationRefusedError("chat result message unavailable")
            self.history._check_row(message, header)
            if (
                message.get("role") != "assistant"
                or message.get("runId") != launch.run_id
                or message.get("leaseGeneration") != launch.lease_generation
                or message.get("userTurnId") != receipt["ref"]
            ):
                raise ChatAuthorizationRefusedError("chat result message refused")
            checks.append(self._unchanged(message))
        update = self._unchanged(receipt)["ConditionCheck"]
        update["ConditionExpression"] += " AND attribute_not_exists(result_candidate)"
        update["UpdateExpression"] = "SET result_candidate = :result, resultRecordedAt = :now"
        update["ExpressionAttributeValues"].update(_encoded({":result": candidate, ":now": now}))
        store = self.authority.store
        instant = datetime.fromtimestamp(now, UTC)
        grant = store.live_grant(invocation_id=run_id, tenant_id=launch.tenant_id, attempt=launch.attempt, now=instant)
        if grant.grant_id != launch.grant_id or grant.revocation_epoch != launch.grant_epoch:
            raise ChatAuthorizationRefusedError("chat result grant changed")
        checks.extend(
            [
                {"Update": update},
                store._authority_check(grant),
                store._grant_check(grant, instant),
                {
                    "ConditionCheck": {
                        "TableName": store.table,
                        "Key": _encoded({"pk": f"TENANT#{launch.tenant_id}", "sk": f"EXEC#{run_id}"}),
                        "ConditionExpression": (
                            "#status = :active AND workload_binding = :pod AND current_attempt = :attempt "
                            "AND current_credential_epoch = :epoch AND attribute_not_exists(abort_command_id) AND chat_user_turn = :input"
                        ),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": _encoded(
                            {
                                ":active": "active",
                                ":pod": launch.sandbox_uid,
                                ":attempt": launch.attempt,
                                ":epoch": launch.credential_epoch,
                                ":input": user_turn,
                            }
                        ),
                    }
                },
            ]
        )
        if launch.session_run_id:
            execution_update = checks[-1].pop("ConditionCheck")
            execution_update["ConditionExpression"] += " AND attribute_not_exists(chat_turn_sealed)"
            execution_update["UpdateExpression"] = "SET chat_turn_sealed = :sealed"
            execution_update["ExpressionAttributeValues"][":sealed"] = {"BOOL": True}
            checks[-1]["Update"] = execution_update
        try:
            store.client.transact_write_items(TransactItems=checks)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                self.history._authorize(token, run_id, session_id, "turn.result", now)
                _, latest = self._accepted(launch)
                if latest[1].get("status") == "accepted" and latest[1].get("result_candidate") == candidate:
                    return candidate
                raise ChatHistoryConflictError("chat result changed; reread before retry") from None
            raise ChatAuthorizationUnavailableError("chat result unavailable") from None
        except BotoCoreError:
            raise ChatAuthorizationUnavailableError("chat result unavailable") from None
        return candidate
