"""Atomic exclusion between model execution and fixed qualification probes."""

from typing import Literal

from src.agentauth.chat_admission import _encoded


def execution_mode_update(authority, launch, mode: Literal["model", "qualification"]):
    return {
        "Update": {
            "TableName": authority.store.table,
            "Key": _encoded({"pk": f"TENANT#{launch.tenant_id}", "sk": f"EXEC#{launch.run_id}"}),
            "UpdateExpression": "SET chat_execution_kind = :kind",
            "ConditionExpression": (
                "#status = :active AND workload_binding = :pod AND current_attempt = :attempt "
                "AND current_credential_epoch = :epoch AND attribute_not_exists(abort_command_id) "
                "AND attribute_not_exists(chat_turn_sealed) "
                "AND (attribute_not_exists(chat_execution_kind) OR chat_execution_kind = :kind)"
            ),
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": _encoded(
                {
                    ":active": "active",
                    ":pod": launch.sandbox_uid,
                    ":attempt": launch.attempt,
                    ":epoch": launch.credential_epoch,
                    ":kind": mode,
                }
            ),
        }
    }
