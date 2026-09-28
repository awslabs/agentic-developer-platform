"""Pure pre-dispatch recovery identities; no credentials or remote operations."""

import hashlib
import json
import uuid


def recovery_plan(config):
    fixture = config["vault_lifecycle"]
    identity = json.dumps(
        [
            config["evaluation_id"],
            fixture["tenant_id"],
            fixture["canonical_user_id"],
            "vault-lifecycle-v1",
        ]
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return {
        "version": 1,
        "purpose": "vault_lifecycle",
        "evaluation_id": config["evaluation_id"],
        "gateway": config["gateway_url"],
        "tenant_id": fixture["tenant_id"],
        "canonical_user_id": fixture["canonical_user_id"],
        "login_user_id": fixture["login_user_id"],
        "operation_id": str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
        "provider": "discord",
        "provider_user_id": "adp-evaluation-" + digest[:32],
        "service": "adp-evaluation",
        "label": "owned-vault-" + digest[:12],
    }


def plan_digest(plan):
    return hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
