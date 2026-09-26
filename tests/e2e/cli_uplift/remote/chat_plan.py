"""Pure caller-side chat recovery intent, safe to retain outside the worker."""

import hashlib
import json


def recovery_plan(config):
    fixture = config["human_task_chat"]
    identity = json.dumps(
        [config["evaluation_id"], fixture["tenant_id"], fixture["canonical_user_id"]],
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()
    marker = "memory-" + digest[:12]
    messages = [
        f"Remember this label for our next turn: {marker}. Reply only NOTED.",
        "What label did I ask you to remember in our previous turn? Reply only with that label.",
    ]
    return {
        "schema": "hosted-chat-recovery-v1",
        "evaluation_id": config["evaluation_id"],
        "gateway": config["gateway_url"],
        "login_user_id": config["test_user_id"],
        "tenant_id": fixture["tenant_id"],
        "canonical_user_id": fixture["canonical_user_id"],
        "max_tasks": fixture["max_tasks"],
        "max_task_usd": fixture["max_task_usd"],
        "memory_label": marker,
        "turns": [
            {
                "action": "start" if index == 0 else "resume-original-session",
                "request_id": "chatdiag-" + digest[:48] + "-" + str(index),
                "message": message,
                "persona": "agent-task-investigator",
            }
            for index, message in enumerate(messages)
        ],
    }
