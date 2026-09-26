"""Pure immutable coding intent retained before a disposable worker starts."""

import hashlib
import json
import uuid


def command_uuid(identity, purpose):
    # Retain stable retry identities with the UUID4 shape required by Task API.
    # These are derived command IDs, not a source of random/security material.
    digest = hashlib.sha256((identity + ":" + purpose).encode()).digest()[:16]
    return str(uuid.UUID(bytes=digest, version=4))


def recovery_plan(config):
    fixture = config["human_task_coding"]
    identity = json.dumps(
        [
            config["evaluation_id"],
            fixture["tenant_id"],
            fixture["canonical_user_id"],
            fixture["persona"],
        ],
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return {
        "schema": "hosted-coding-recovery-v2",
        "evaluation_id": config["evaluation_id"],
        "gateway": config["gateway_url"],
        "login_user_id": fixture["login_user_id"],
        "canonical_user_id": fixture["canonical_user_id"],
        "tenant_id": fixture["tenant_id"],
        "request_id": "e42-" + digest[:48],
        "command_id": command_uuid(identity, "control"),
        "cleanup_command_id": command_uuid(identity, "cleanup"),
        "persona": fixture["persona"],
        "scenario": fixture["scenario"],
        "control_when": fixture.get("control_when", "observed"),
        "running_wait_seconds": fixture.get("running_wait_seconds", 30),
        "max_dispatches": fixture["max_dispatches"],
        "max_task_usd": fixture["max_task_usd"],
        "snapshot": fixture["snapshot"],
        "instructions": fixture["instructions"],
    }
