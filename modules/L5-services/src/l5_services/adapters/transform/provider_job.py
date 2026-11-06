"""Finite Transform CLI command for a separately qualified provider job."""

import json
import re
from hashlib import sha256

from l5_services.adapters.transform.qualification import ProviderQualification


NDJSON_INTERFACE = "atx-json-3.19.0-v1"
NDJSON_BINARY_SHA256 = "87117a33f1aab02e78e0ed216549edef49918329df55e756bebd84d8f79a10bf"


def finite_provider_job(intent, manifest, qualification, limits):
    verdict = ProviderQualification.model_validate(qualification)
    if (not verdict.ready_for_submission or verdict.interface not in {"atx-custom-def-exec-v1", NDJSON_INTERFACE}
            or not isinstance(intent, dict) or not isinstance(manifest, dict)
            or not isinstance(limits, dict)):
        raise PermissionError("Transform provider job is not qualified")
    definition = intent.get("definition_ref")
    if (not isinstance(definition, dict) or definition.get("record_type") != "transform_definition"
            or definition.get("tenant_id") != intent.get("tenant_id")
            or definition.get("project_id") != intent.get("project_id")
            or not isinstance(definition.get("record_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,99})?", definition["record_id"])
            or not isinstance(definition.get("record_revision"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", definition["record_revision"])
            or definition["record_revision"] != verdict.definition_revision):
        raise ValueError("Transform definition differs from pinned qualification")
    scope = manifest.get("scope")
    if (not isinstance(scope, dict)
            or not isinstance(intent.get("executor_name"), str)
            or not re.fullmatch(r"tf-[a-f0-9]{40}", intent["executor_name"])
            or intent.get("image_digest") != verdict.image_digest
            or intent.get("binary_sha256") != verdict.binary_sha256
            or intent.get("definition_revision") != verdict.definition_revision
            or not isinstance(intent.get("attempt_token"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", intent["attempt_token"])
            or not isinstance(intent.get("input_hash"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", intent["input_hash"])
            or manifest.get("run_id") != intent.get("run_id")
            or scope.get("tenant_id") != intent.get("tenant_id")
            or scope.get("project_id") != intent.get("project_id")
            or scope.get("product_id") != intent.get("product_id")
            or not isinstance(manifest.get("sources"), list) or not manifest["sources"]
            or not isinstance(manifest.get("source_snapshot_ids"), list)
            or sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            != intent.get("input_hash")
            or not manifest["source_snapshot_ids"]):
        raise ValueError("Transform job differs from sealed work identity")
    minutes = limits.get("agent_minutes")
    deadline_seconds = limits.get("deadline_seconds")
    if (type(minutes) is not int or not 1 <= minutes <= 60
            or type(deadline_seconds) is not int or not 60 <= deadline_seconds <= 86400
            or minutes * 60 > deadline_seconds):
        raise PermissionError("Transform agent-minute budget is not admitted")
    job = {"image_digest": verdict.image_digest,
            "binary_sha256": verdict.binary_sha256,
            "executor_name": intent["executor_name"],
            "attempt_token": intent["attempt_token"],
            "input_hash": intent["input_hash"],
            "argv": ["/usr/local/bin/atx", "custom", "def", "exec",
                     "--code-repository-path", "/work/source",
                     "--transformation-name", definition["record_id"],
                     "--transformation-version", definition["record_revision"],
                     "--non-interactive", "--do-not-learn", "--limit", str(minutes)],
            "active_deadline_seconds": deadline_seconds}
    if verdict.interface == NDJSON_INTERFACE:
        if verdict.cli_version != "3.19.0" or verdict.binary_sha256 != NDJSON_BINARY_SHA256:
            raise PermissionError("Transform NDJSON executable differs from the pinned candidate")
        job["argv"] = ["/usr/local/bin/atx", "json"]
        job["session_request"] = {
            "type": "start_session", "mode": "exec",
            "params": {"codeRepositoryPath": "/work/source", "trustAllTools": False,
                       "interactiveMode": True, "doNotLearn": True,
                       "useKnowledgeItems": False, "agentMinutesLimit": str(minutes),
                       "transformationName": definition["record_id"],
                       "transformationVersion": definition["record_revision"],
                       "buildCommand": "true"},
            "message": "Apply only the admitted transformation: " + json.dumps(intent["goal"], sort_keys=True),
        }
    return job
