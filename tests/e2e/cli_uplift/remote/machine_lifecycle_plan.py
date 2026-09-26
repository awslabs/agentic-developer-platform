"""Pure identities retained outside EC2 before owned canonical metadata writes."""

import hashlib
import json
import uuid


def recovery_plan(config):
    identity = json.dumps(
        [
            config["evaluation_id"],
            config["gateway_url"],
            config["machine_lifecycle"],
            "machine-lifecycle-v1",
        ],
        sort_keys=True,
    )
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:32]
    return {
        "version": 1,
        "purpose": "machine_lifecycle",
        "evaluation_id": config["evaluation_id"],
        "gateway": config["gateway_url"],
        "fixture": config["machine_lifecycle"],
        "display_name": "owned-machine-" + suffix,
        "registration_id": str(uuid.uuid5(uuid.NAMESPACE_URL, identity + ":register")),
        "duplicate_id": str(uuid.uuid5(uuid.NAMESPACE_URL, identity + ":duplicate")),
        "aliases": [
            {
                "alias_source": "eventbridge",
                "alias_id": "adp-evaluation-machine-" + suffix,
            },
            {
                "alias_source": "github_actions",
                "alias_id": "adp-evaluation-machine-secondary-" + suffix,
            },
        ],
        "cleanup": "Revoke only recorded aliases and retire exact owned principal; retain history. No provider identity or credential is created.",
    }
