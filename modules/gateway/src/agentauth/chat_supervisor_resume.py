"""Recover the original admitted binding without issuing model authority."""

from botocore.exceptions import BotoCoreError, ClientError

from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, ChatLaunchStore
from src.agentauth.chat_crew_run import CrewRunConflictError, current_crew_store
from src.agentauth.chat_delivery import load_registered_delivery
from src.agentauth.chat_history_write import ChatHistoryConflictError
from src.agentauth.chat_pre_admission_cleanup import recover_bound_cleanup
from src.agentauth.chat_queued_completion import complete_queued_turn
from src.agentauth.chat_queued_terminal import finalize_queued_turn, queued_input_expired
from src.agentauth.chat_sandbox_creation import load_creation, reserve_creation
from src.agentauth.chat_session_mailbox import ChatSessionMailbox
from src.agentauth.chat_session_recovery import fence_expired_session, resume_persistent_creation
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
    if existing is None and "workload_binding" not in execution and "abort_command_id" not in execution and not queued_input_expired(execution, now):
        mailbox = ChatSessionMailbox(authority.context_table)
        session = mailbox.state(session_id=delivery.session_id, owner=(delivery.tenant_id, delivery.team_id, delivery.user_id), now=now)
        header = mailbox._header(delivery.session_id, (delivery.tenant_id, delivery.team_id, delivery.user_id), now)
        lease = (header or {}).get("chatLease", {})
        if session["mode"] == "persistent" and lease.get("run_id") and lease["run_id"] != body.run_id:
            owner = capabilities.launches.load(lease["run_id"])
            if owner.session_run_id:
                if (owner.tenant_id, owner.team_id, owner.user_id, owner.session_id) != (
                    delivery.tenant_id,
                    delivery.team_id,
                    delivery.user_id,
                    delivery.session_id,
                ):
                    raise ChatAuthorizationRefusedError("chat predecessor owner changed")
                if not fence_expired_session(authority, capabilities, owner, now=now, crew_store=current_crew_store()):
                    return {**response, "state": "session_pending", "session_mode": "persistent"}
                previous_delivery = load_registered_delivery(authority, owner.run_id, owner.tenant_id)
                previous = store._read(f"TENANT#{owner.tenant_id}", f"EXEC#{owner.run_id}") or {}
                teardown = store._read(f"CHAT-LAUNCH#{owner.run_id}", "TEARDOWN") or {}
                outbox = store._read(f"CHAT-DELIVERY#{owner.run_id}", "TERMINAL") or {}
                if "removed_at" not in teardown or "completion_receipt" not in outbox:
                    return {
                        **response,
                        "state": "session_recovery",
                        "recovery": {
                            **{field: getattr(previous_delivery, field) for field in ("run_id", "session_id", "task_id", "session_generation")},
                            "envelope_digest": previous["envelope_digest"]["S"],
                            "image_digest": owner.image_digest,
                        },
                    }
    if creation_image is not None:
        if existing is not None:
            raise ChatHistoryConflictError("chat already admitted")
        reserved = reserve_creation(authority, delivery, pointer, execution, creation_image, now=now)
        session = ChatSessionMailbox(authority.context_table).state(
            session_id=delivery.session_id, owner=(delivery.tenant_id, delivery.team_id, delivery.user_id), now=now
        )
        return {**response, **reserved, "session_mode": session["mode"]}
    if existing is None:
        creation = load_creation(authority, delivery, pointer, execution)
        if creation is not None and not removed:
            resumed = resume_persistent_creation(authority, capabilities, delivery, pointer, execution, creation, now=now)
            if resumed is not None:
                return {**response, **resumed}
        if "workload_binding" in execution or creation is not None:
            if removed and (execution.get("pod_name") != {"S": body.pod_name} or execution.get("workload_binding") != {"S": body.pod_uid}):
                raise ChatAuthorizationRefusedError("chat cleanup pod refused")
            # A supervisor that watched its reserved pod fail to start names it on resume (#8061);
            # the name must be the reserved one, the uid is only used when no exit is observable.
            named_name, named_uid = (None, None) if removed else (getattr(body, "pod_name", None), getattr(body, "pod_uid", None))
            if named_name is not None and execution.get("pod_name") != {"S": named_name}:
                raise ChatAuthorizationRefusedError("chat cleanup pod refused")
            cleanup = recover_bound_cleanup(authority, delivery, pointer, execution, removed=removed, now=now, creation=creation, pod_uid=named_uid)
            if cleanup["removed"]:
                if creation is not None and "crew_run_id" in creation:
                    crew_store = current_crew_store()
                    crew_run_id = creation["crew_run_id"]["S"]
                    try:
                        crew_store.revoke_for_lease(
                            tenant_id=delivery.tenant_id,
                            session_id=delivery.session_id,
                            lease_generation=int(creation["crew_lease_generation"]["N"]),
                            expected_run_id=crew_run_id,
                        )
                    except CrewRunConflictError:
                        if crew_store.read_run(crew_run_id) is not None:
                            raise ChatHistoryConflictError("crew creation binding changed; retry") from None
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
        if launch.session_run_id:
            response.update(session_mode="persistent", session_run_id=launch.session_run_id)
            creation = store._read(f"CHAT-LAUNCH#{launch.session_run_id}", "CREATION") or {}
            crew_run_id = creation.get("crew_run_id", {}).get("S")
            row = current_crew_store().read_run(crew_run_id) if crew_run_id else None
            if row and row.get("executor_scope") == "user":
                if (
                    row.get("kind") != "crew"
                    or row.get("tenant_id") != launch.tenant_id
                    or row.get("user_id") != launch.user_id
                    or row.get("target_kind") != "eks-pod"
                    or row.get("target_ref") != response["pod_name"]
                    or row.get("pod_uid") != launch.sandbox_uid
                    or creation.get("crew_lease_generation") != {"N": str(row.get("lease_generation"))}
                ):
                    raise ChatAuthorizationRefusedError("Crew recovery owner changed")
                response["crew_owner_pod"] = True
                if launch.run_id == launch.session_run_id and row.get("session_id") == launch.session_id:
                    response["crew_readiness"] = {"run_id": crew_run_id, "lease_generation": int(row["lease_generation"])}
            if fence_expired_session(authority, capabilities, launch, now=now, crew_store=current_crew_store()):
                response["recovery_required"] = True
                execution = store._read(f"TENANT#{tenant}", f"EXEC#{body.run_id}")
                checks[1] = {"ConditionCheck": evidence._unchanged(execution)}
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
