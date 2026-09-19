"""Validate and materialize the server-resolved input for an amendment author."""

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

MAX_BASE_DOCUMENT_BYTES = 128 * 1024
AMENDMENT_BASE_PATH_ENV = "ADP_AMENDMENT_BASE_PATH"


class AuthoringInputError(ValueError):
    """A stable refusal, safe to report without echoing submitted plan data."""


def materialize_authoring_input(envelope: dict, *, directory: str | None = None) -> str | None:
    context = envelope.get("orchestration")
    if not isinstance(context, dict) or not context.get("request_id"):
        return None
    payload = envelope.get("payload")
    snapshot = payload.get("amendment_base") if isinstance(payload, dict) else None
    if not isinstance(snapshot, dict):
        raise AuthoringInputError("authoring_input_base_missing")
    if type(snapshot.get("version")) is not int or snapshot["version"] != 1:
        raise AuthoringInputError("authoring_input_version_invalid")
    intent = envelope.get("intent")
    if (
        envelope.get("persona") != "aidlc"
        or envelope.get("channel") != "orchestration"
        or not isinstance(intent, dict)
        or intent.get("trigger") != "engine_replan"
    ):
        raise AuthoringInputError("authoring_input_binding_mismatch")
    expected = {
        "org_id": envelope.get("tenant_id"),
        "flow_id": context.get("flow_id"),
        "request_id": context.get("request_id"),
        "author_run_id": envelope.get("message_id"),
        "base_plan_hash": context.get("base_plan_hash"),
    }
    if any(
        not isinstance(value, str) or not value.strip() or snapshot.get(key) != value
        for key, value in expected.items()
    ):
        raise AuthoringInputError("authoring_input_binding_mismatch")
    version = context.get("base_plan_version")
    if (
        type(version) is not int
        or version < 1
        or type(snapshot.get("base_plan_version")) is not int
        or snapshot["base_plan_version"] != version
    ):
        raise AuthoringInputError("authoring_input_base_stale")
    if not re.fullmatch(r"[0-9a-f]{64}", expected["base_plan_hash"]):
        raise AuthoringInputError("authoring_input_base_hash_invalid")
    document = snapshot.get("document")
    if not isinstance(document, dict):
        raise AuthoringInputError("authoring_input_base_invalid")
    try:
        content = json.dumps(
            document, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise AuthoringInputError("authoring_input_base_invalid") from None
    if len(content) > MAX_BASE_DOCUMENT_BYTES:
        raise AuthoringInputError("authoring_input_base_too_large")
    if hashlib.sha256(content).hexdigest() != snapshot.get("document_sha256"):
        raise AuthoringInputError("authoring_input_document_digest_mismatch")

    # Outside the checkout: cloning and branch changes cannot delete the input,
    # and repository-controlled symlinks cannot choose its destination.
    folder = None
    try:
        folder = Path(tempfile.mkdtemp(prefix="adp-amendment-input-", dir=directory))
        path = folder / "accepted-plan.json"
        with path.open("xb") as handle:
            os.chmod(path, 0o600)
            handle.write(content)
        os.chmod(path, 0o400)
    except OSError:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        raise AuthoringInputError("authoring_input_materialization_failed") from None
    return str(path)
