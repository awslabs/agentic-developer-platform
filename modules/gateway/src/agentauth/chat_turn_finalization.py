"""Finalize admitted turns after trusted teardown, without publishing or replaying."""

import json
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_authority import ChatSessionLease
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, ChatLaunchStore
from src.agentauth.chat_history_store import history_version
from src.agentauth.chat_history_write import ChatHistoryConflictError, ChatHistoryWriter
from src.agentauth.chat_teardown import ChatTeardown
from src.agentauth.chat_terminal_delivery import prepare_terminal_delivery, terminal_delivery_digest, verify_terminal_delivery
from src.orchestration.chat_data_migration import _owns_context_row


class ChatTurnFinalizer(ChatHistoryWriter):
    def finalize(self, body, *, now):
        launch = self.history.capabilities.launches.load(body.run_id)
        evidence = ChatTeardown(self.authority, self.history.capabilities.launches)
        store = self.authority.store
        dispatch = store._read(f"INVOCATION#{launch.run_id}", "DISPATCH") or {}
        execution = store._read(f"TENANT#{launch.tenant_id}", f"EXEC#{launch.run_id}") or {}
        teardown = store._read(f"CHAT-LAUNCH#{launch.run_id}", "TEARDOWN") or {}
        binding = {
            "run_id": launch.run_id,
            "envelope_digest": body.envelope_digest,
            "tenant_id": launch.tenant_id,
            "session_id": launch.session_id,
            "pod_name": body.pod_name,
            "pod_uid": launch.sandbox_uid,
            "image_digest": launch.image_digest,
            "attempt": launch.attempt,
            "credential_epoch": launch.credential_epoch,
            "lease_generation": launch.lease_generation,
            "observation_scope": self.authority.workloads.observation_scope,
        }
        if (
            dispatch.get("tenant_id") != {"S": launch.tenant_id}
            or dispatch.get("envelope_digest") != {"S": body.envelope_digest}
            or execution.get("repo") != {"S": f"chat/{launch.session_id}"}
            or execution.get("current_attempt") != {"N": str(launch.attempt)}
            or execution.get("current_credential_epoch") != {"N": str(launch.credential_epoch)}
            or execution.get("workload_binding") != {"S": launch.sandbox_uid}
            or execution.get("pod_name") != {"S": body.pod_name}
            or body.pod_uid != launch.sandbox_uid
            or teardown.get("binding") != {"M": _encoded(binding)}
            or any("N" not in teardown.get(field, {}) for field in ("exited_at", "removed_at"))
        ):
            raise ChatAuthorizationRefusedError("chat finalization scope refused")
        _, accepted = self._accepted(launch)
        message, turn = accepted
        checks = [{"ConditionCheck": evidence._unchanged(record)} for record in (dispatch, teardown, ChatLaunchStore.item(launch))]
        if "chat_terminal" in execution:
            terminal = TypeDeserializer().deserialize(execution["chat_terminal"])
            if (
                not isinstance(terminal, dict)
                or turn.get("terminal_result") != terminal
                or turn.get("status") != terminal.get("outcome")
                or terminal.get("outcome") not in {"completed", "failed", "cancelled", "interrupted"}
                or terminal.get("terminal") is not True
                or any(
                    terminal.get(field) != getattr(launch, field) for field in ("run_id", "session_id", "attempt", "lease_generation", "sandbox_uid")
                )
                or execution.get("status") != {"S": "cancelled" if terminal.get("outcome") == "cancelled" else "completed"}
            ):
                raise ChatAuthorizationUnavailableError("chat terminal receipt inconsistent")
            for field in ("attempt", "lease_generation", "finalized_at"):
                value = terminal.get(field)
                if isinstance(value, bool) or not isinstance(value, int | Decimal) or int(value) != value or value < 1:
                    raise ChatAuthorizationUnavailableError("chat terminal receipt invalid")
                terminal[field] = int(value)
            delivery = verify_terminal_delivery(store, execution, launch, terminal)
            if delivery is not None:
                checks.append({"ConditionCheck": evidence._unchanged(delivery)})
            self._transact([*checks, {"ConditionCheck": evidence._unchanged(execution)}, self._unchanged(turn), self._unchanged(message)])
            return terminal
        if execution.get("status") != {"S": "active"} or turn.get("status") not in {"accepted", "interrupted"}:
            raise ChatAuthorizationRefusedError("chat turn already ended")
        header = self.history._get(launch.session_id, "header")
        if not header or not _owns_context_row(header, (launch.tenant_id, launch.team_id, launch.user_id)):
            raise ChatAuthorizationRefusedError("chat finalization owner changed")
        lease = ChatSessionLease.model_validate(header.get("chatLease"))
        if (lease.run_id, lease.sandbox_uid, lease.generation) != (launch.run_id, launch.sandbox_uid, launch.lease_generation):
            raise ChatAuthorizationRefusedError("chat finalization lease changed")
        if lease.expires_at != 1:
            seal = self._unchanged(header)["ConditionCheck"]
            seal["UpdateExpression"] = "SET chatLease.expires_at = :fenced"
            seal["ExpressionAttributeValues"][":fenced"] = {"N": "1"}
            self._transact([*checks, {"ConditionCheck": evidence._unchanged(execution)}, {"Update": seal}])
            header = {**header, "chatLease": {**header["chatLease"], "expires_at": 1}}
        accounting = self._accounting(launch)
        candidate = turn.get("result_candidate")
        outcome, reply = "interrupted", None
        if "abort_command_id" in execution:
            if execution.get("abort_requested_attempt") != {"N": str(launch.attempt)}:
                raise ChatAuthorizationRefusedError("chat cancellation attempt changed")
            outcome = "cancelled"
        elif candidate is not None:
            if not isinstance(candidate, dict):
                raise ChatAuthorizationRefusedError("chat result candidate invalid")
            expected = {
                "run_id": launch.run_id,
                "session_id": launch.session_id,
                "attempt": launch.attempt,
                "lease_generation": launch.lease_generation,
                "sandbox_uid": launch.sandbox_uid,
                "outcome": candidate.get("outcome"),
                "message_id": candidate.get("message_id"),
                "terminal": False,
            }
            if candidate != expected or candidate.get("outcome") not in {"completed", "failed"}:
                raise ChatAuthorizationRefusedError("chat result candidate changed")
            outcome = candidate["outcome"]
            if outcome == "completed":
                reply = self.history._get(launch.session_id, f"msg#{candidate['message_id']}")
                if (
                    not reply
                    or not _owns_context_row(reply, (launch.tenant_id, launch.team_id, launch.user_id))
                    or reply.get("role") != "assistant"
                    or reply.get("runId") != launch.run_id
                    or reply.get("leaseGeneration") != launch.lease_generation
                    or reply.get("userTurnId") != turn.get("ref")
                ):
                    raise ChatAuthorizationRefusedError("chat final reply changed")
                checks.append(self._unchanged(reply))
            elif candidate.get("message_id") is not None:
                raise ChatAuthorizationRefusedError("chat failed result changed")
        retryable = outcome == "interrupted" and accounting == "not_used" and self._unchanged_history(header, turn)
        terminal = {
            "run_id": launch.run_id,
            "session_id": launch.session_id,
            "attempt": launch.attempt,
            "lease_generation": launch.lease_generation,
            "sandbox_uid": launch.sandbox_uid,
            "outcome": outcome,
            "message_id": candidate["message_id"] if reply else None,
            "retryable": retryable,
            "automatic_replay_permitted": False,
            "accounting_status": accounting,
            "terminal": True,
            "finalized_at": now,
        }
        update_turn = self._unchanged(turn)["ConditionCheck"]
        update_turn["ConditionExpression"] += " AND attribute_not_exists(terminal_result)"
        if candidate is None:
            update_turn["ConditionExpression"] += " AND attribute_not_exists(result_candidate)"
        update_turn["UpdateExpression"] = (
            "SET #outcome = :outcome, terminal_result = :terminal, retryable = :retryable, automaticReplayPermitted = :no_replay"
        )
        update_turn["ExpressionAttributeNames"]["#outcome"] = "status"
        update_turn["ExpressionAttributeValues"].update(
            _encoded({":outcome": outcome, ":terminal": terminal, ":retryable": retryable, ":no_replay": False})
        )
        update_execution = evidence._unchanged(execution)
        update_execution["ConditionExpression"] += " AND attribute_not_exists(chat_terminal)"
        if "abort_command_id" not in execution:
            update_execution["ConditionExpression"] += " AND attribute_not_exists(abort_command_id)"
        update_execution["UpdateExpression"] = "SET #outcome = :outcome, chat_terminal = :terminal"
        update_execution["ExpressionAttributeNames"]["#outcome"] = "status"
        update_execution["ExpressionAttributeValues"].update(
            _encoded({":outcome": "cancelled" if outcome == "cancelled" else "completed", ":terminal": terminal})
        )
        delivery = prepare_terminal_delivery(execution, launch, terminal, reply)
        if delivery is not None:
            update_execution["ConditionExpression"] += " AND attribute_not_exists(chat_terminal_delivery_digest)"
            update_execution["UpdateExpression"] += ", chat_terminal_delivery_digest = :delivery_digest"
            update_execution["ExpressionAttributeValues"][":delivery_digest"] = terminal_delivery_digest(delivery)
            checks.append({"Put": {"TableName": store.table, "Item": delivery, "ConditionExpression": "attribute_not_exists(pk)"}})
        self._transact([*checks, self._unchanged(header), self._unchanged(message), {"Update": update_turn}, {"Update": update_execution}])
        return terminal

    def _transact(self, transactions):
        try:
            self.authority.store.client.transact_write_items(TransactItems=transactions)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise ChatHistoryConflictError("chat finalization changed; retry reconciliation") from None
            raise ChatAuthorizationUnavailableError("chat finalization unavailable") from None
        except BotoCoreError:
            raise ChatAuthorizationUnavailableError("chat finalization unavailable") from None

    @staticmethod
    def _unchanged_history(header, turn):
        accepted_version, accepted_next = turn.get("acceptedHistoryVersion"), turn.get("acceptedNextOrdinal")
        if any(
            isinstance(value, bool) or not isinstance(value, int | Decimal) or int(value) != value or value < 1
            for value in (accepted_version, accepted_next)
        ):
            raise ChatAuthorizationUnavailableError("chat accepted history unavailable")
        return history_version(header) == accepted_version and header.get("historyNextOrdinal") == accepted_next

    def _accounting(self, launch):
        store = self.authority.store
        request = {
            "TableName": store.table,
            "KeyConditionExpression": "pk = :run AND begins_with(sk, :prefix)",
            "ExpressionAttributeValues": {":run": {"S": f"CHAT-MODEL#{launch.run_id}"}, ":prefix": {"S": "OP#"}},
            "ConsistentRead": True,
        }
        status = "not_used"
        try:
            while True:
                page = store.client.query(**request)
                for item in page.get("Items", []):
                    operation = json.loads(item["document"]["S"])
                    if any(
                        operation.get(field) != getattr(launch, field) for field in ("run_id", "session_id", "sandbox_uid", "lease_generation")
                    ) or item.get("sk") != {"S": f"OP#{operation['operation_id']}"}:
                        raise ValueError
                    settled = (
                        operation.get("status") == "confirmed"
                        and operation.get("reservation_status") == "settled"
                        and operation.get("usage_logged") is True
                    ) or (operation.get("status") == "rejected" and operation.get("reservation_status") in {"released", "not_reserved"})
                    status = "settled" if settled and status != "unresolved" else "unresolved"
                if not page.get("LastEvaluatedKey"):
                    return status
                request["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        except (BotoCoreError, ClientError, KeyError, TypeError, ValueError, AttributeError):
            raise ChatAuthorizationUnavailableError("chat final accounting unavailable") from None
