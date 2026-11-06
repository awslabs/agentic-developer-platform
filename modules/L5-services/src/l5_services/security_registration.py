"""Use only the selected native space's observed source registration."""

import re
from datetime import UTC, datetime, timedelta


def bucket_name(value):
    """Security Agent returns a bucket name or ARN for the same native resource."""
    if not isinstance(value, str):
        return None
    name = value.removeprefix("arn:aws:s3:::")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", name) or ".." in name:
        return None
    return name


def observed_registration(resources, account_id):
    if not isinstance(resources, dict) or not isinstance(account_id, str):
        return None
    roles, buckets = resources.get("iamRoles"), resources.get("s3Buckets")
    if not isinstance(roles, list) or not isinstance(buckets, list) or len(roles) != 1 or len(buckets) != 1:
        return None
    role, bucket = roles[0], bucket_name(buckets[0])
    if (not isinstance(role, str) or bucket is None
            or not re.fullmatch(rf"arn:aws:iam::{re.escape(account_id)}:role/[A-Za-z0-9_+=,.@/-]+", role)):
        return None
    return {"service_role": role, "bucket": bucket}


def current_registration(binding, *, now=None):
    if (binding.state != "ready" or binding.gaps or not isinstance(binding.readiness, list)
            or not 1 <= len(binding.readiness) <= 2):
        return None
    recorded = binding.readiness[0]
    if (len(binding.readiness) == 2 and (
            not isinstance(binding.readiness[1], dict)
            or binding.readiness[1].get("kind") not in {
                "threat_model", "threat_model_outcome_unknown", "threat_model_selection_required"})):
        return None
    if not isinstance(recorded, dict) or recorded.get("kind") != "code_review_source_registration":
        return None
    registration = recorded.get("registration")
    identity = binding.provider_identity or {}
    if (not isinstance(registration, dict) or set(registration) != {"service_role", "bucket"}
            or observed_registration({"iamRoles": [registration["service_role"]],
                                      "s3Buckets": [f'arn:aws:s3:::{registration["bucket"]}']},
                                     identity.get("account_id")) != registration):
        return None
    try:
        observed_at = datetime.fromisoformat(recorded["observed_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, TypeError, ValueError):
        return None
    now = now or datetime.now(UTC)
    if observed_at.tzinfo is None or not observed_at <= now < observed_at + timedelta(minutes=15):
        return None
    return registration


def current_model(binding, *, now=None):
    if current_registration(binding, now=now) is None or len(binding.readiness) != 2:
        return None
    model = binding.readiness[1]
    reference = model.get("native_model_ref") or {}
    space = binding.native_resource_ref or {}
    if (model.get("kind") != "threat_model" or not isinstance(reference, dict)
            or reference.get("record_type") != "threat_model"
            or not all(reference.get(field) == space.get(field) and reference.get(field)
                       for field in ("tenant_id", "project_id", "source_connection_id"))
            or not reference.get("record_id") or not reference.get("record_revision")):
        return None
    try:
        observed_at = datetime.fromisoformat(model["observed_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, TypeError, ValueError):
        return None
    now = now or datetime.now(UTC)
    if observed_at.tzinfo is None or not observed_at <= now < observed_at + timedelta(minutes=15):
        return None
    return reference
