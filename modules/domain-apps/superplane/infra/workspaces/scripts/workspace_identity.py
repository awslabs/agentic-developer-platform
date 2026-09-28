"""Version 2 workspace identity, shared by preparation, ownership and apply."""

import hashlib
import json
import re


def validate_identity_ids(org_id, workspace_id):
    for name, value in (("org_id", org_id), ("workspace_id", workspace_id)):
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value
        ):
            raise ValueError(
                f"{name} must be an immutable bound ID (1-128 safe ASCII characters)"
            )


def infrastructure_id(org_id, workspace_id):
    validate_identity_ids(org_id, workspace_id)
    encoded = json.dumps(
        [org_id, workspace_id], separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode()).hexdigest()[:32]


def state_key(target):
    validate_identity_ids(target["org_id"], target["workspace_id"])
    return (
        f"{target['environment']}/modules/superplane-workspaces/v2/"
        f"{target['org_id']}/{target['workspace_id']}/terraform.tfstate"
    )
