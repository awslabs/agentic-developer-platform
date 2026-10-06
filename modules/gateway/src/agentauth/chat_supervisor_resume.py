"""Recover the original admitted binding without issuing model authority."""

from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, ChatLaunchStore
from src.agentauth.chat_delivery import load_registered_delivery
from src.agentauth.chat_history_write import ChatHistoryConflictError
from src.agentauth.chat_pre_admission_cleanup import recover_bound_cleanup
from src.agentauth.chat_queued_completion import complete_queued_turn
from src.agentauth.chat_queued_terminal import finalize_queued_turn, queued_input_expired
from src.agentauth.chat_sandbox_creation import load_creation, reserve_creation
from src.agentauth.chat_teardown import ChatTeardown
from src.agentauth.chat_terminal_publication import publish_pre_admission_terminal_delivery, publish_queued_terminal_delivery


def resume_supervisor_turn(authority, capabilities, body, role, bindings, *, now, removed=False, creation_image=None):
    store = authority.store
    pointer = store._read(f"INVOCATION#{body.run_id}", "DISPATCH") or {}
    tenant = pointer.get("tenant_id", {}).get("S")
    if not tenant or pointer.get("envelope_digest") != {"S": body.envelope_digest}:
        raise ChatAuthorizationRefusedError("chat recovery root refused")
    execution = store._read(f"TENANT#{tenant}", f"EXEC#{body.run_id}") or {}
    if not any(
        binding.chat_supervisor_role == role and binding.tenant_id == tenant and execution.get("persona", {}).get("S") in binding.personas
        for binding in bindings
    ):
        raise ChatAuthorizationRefusedError("chat recovery supervisor refused")
    delivery = load_registered_delivery(authority, body.run_id, tenant)
    if execution.get("repo") != {"S": f"chat/{delivery.session_id}"}:
        raise ChatAuthorizationRefusedError("chat recovery session refused")
    response = {field: getattr(delivery, field) for field in ("run_id", "session_id", "task_id", "session_generation")}
    evidence = ChatTeardown(authority, capabilities.launches)
    checks = [{"ConditionCheck": evidence._unchanged(item)} for item in (pointer, execution)]
    existing = store._read(f"CHAT-LAUNCH#{body.run_id}", "LAUNCH")
    if creation_image is not None:
        if existing is not None:
            raise ChatHistoryConflictError("chat already admitted")
        return {**response, **reserve_creation(authority, delivery, pointer, execution, creation_image, now=now)}
    if existing is None:
        creation = load_creation(authority, delivery, pointer, execution)
        if "workload_binding" in execution or creation is not None:
            if removed and (execution.get("pod_name") != {"S": body.pod_name} or execution.get("workload_binding") != {"S": body.pod_uid}):
                raise ChatAuthorizationRefusedError("chat cleanup pod refused")
            cleanup = recover_bound_cleanup(authority, delivery, pointer, execution, removed=removed, now=now, creation=creation)
            if cleanup["removed"]:
                publish_pre_admission_terminal_delivery(authority, delivery)
            return {**response, **cleanup}
        if removed:
            raise ChatAuthorizationRefusedError("chat cleanup binding required")
        if "abort_command_id" in execution or queued_input_expired(execution, now):
            terminal = finalize_queued_turn(authority, capabilities, delivery, pointer, execution, now=now)
            publish_queued_terminal_delivery(authority, delivery, terminal)
            completion = complete_queued_turn(authority, delivery, pointer, now=now)
            return {**response, "state": "queued_completed", "completion": completion}
        if execution.get("status") != {"S": "pending"} or any(
            field in execution for field in ("pod_name", "abort_command_id", "chat_pre_admission_cleanup")
        ):
            raise ChatHistoryConflictError("chat pre-admission reconciliation required")
        checks[1]["ConditionCheck"]["ConditionExpression"] += (
            " AND attribute_not_exists(workload_binding) AND attribute_not_exists(abort_command_id)"
            " AND attribute_not_exists(pod_name) AND attribute_not_exists(chat_pre_admission_cleanup)"
        )
        response["state"] = "unstarted"
        checks.append(
            {
                "ConditionCheck": {
                    "TableName": store.table,
                    "Key": {"pk": {"S": f"CHAT-LAUNCH#{body.run_id}"}, "sk": {"S": "LAUNCH"}},
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            }
        )
    else:
        launch = capabilities.launches.load(body.run_id)
        if (
            any(getattr(launch, field) != getattr(delivery, field) for field in ("run_id", "tenant_id", "user_id", "team_id", "session_id"))
            or execution.get("current_attempt") != {"N": str(launch.attempt)}
            or execution.get("current_credential_epoch") != {"N": str(launch.credential_epoch)}
            or execution.get("workload_binding") != {"S": launch.sandbox_uid}
            or execution.get("status", {}).get("S") not in {"active", "completed", "cancelled"}
            or not execution.get("pod_name", {}).get("S")
        ):
            raise ChatAuthorizationRefusedError("chat recovery binding changed")
        response.update(
            state="admitted",
            pod_name=execution["pod_name"]["S"],
            **{field: getattr(launch, field) for field in ("sandbox_uid", "attempt", "lease_generation", "image_digest")},
        )
        checks.append({"ConditionCheck": evidence._unchanged(ChatLaunchStore.item(launch))})
    try:
        store.client.transact_write_items(TransactItems=checks)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            raise ChatHistoryConflictError("chat recovery changed; retry") from None
        raise ChatAuthorizationUnavailableError("chat recovery unavailable") from None
    except BotoCoreError:
        raise ChatAuthorizationUnavailableError("chat recovery unavailable") from None
    return response
