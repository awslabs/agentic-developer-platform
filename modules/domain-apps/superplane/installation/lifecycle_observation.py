"""Explicit same-account recovery reads owned by the app installation."""

import re
from .config import require


def role_arn(env):
    return f"arn:aws:iam::{env['account_id']}:role/adp-{env['environment']}-superplane-lifecycle-observer"


def validate(env):
    value = env.get("lifecycle_observation")
    if value is None:
        return
    require(
        isinstance(value, dict) and set(value) == {"regions"},
        "Invalid lifecycle observation configuration",
    )
    regions = value["regions"]
    require(
        isinstance(regions, list)
        and 1 <= len(regions) <= 8
        and all(
            isinstance(r, str) and re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]", r)
            for r in regions
        )
        and len(set(regions)) == len(regions)
        and env.get("api_producer_role") is not None
        and env.get("recovery_transport") is not None
        and env.get("paid_worker", {}).get("mode") == "native-lifecycle",
        "Lifecycle observation requires exact regions, API identity, recovery transport and native lifecycle",
    )
