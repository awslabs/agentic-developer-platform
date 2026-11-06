"""Fail-closed predicates for independent, authorized installed-release readback."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from l5_services.runtime_names import functions, queue_scope


class ReleaseProofError(ValueError):
    """A bounded release observation cannot establish the pinned candidate."""


def require(condition, reason):
    if not condition:
        raise ReleaseProofError(reason)


def digest(value, size=64):
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{size}}}", value) is not None


@dataclass(frozen=True)
class InstalledFixture:
    account_id: str
    role_arn: str
    source_revision: str
    package_version: str
    gateway_image_digest: str
    wheel_sha256: str
    schema_sha256: str
    cli_manifest_sha256: str
    frontend_asset_sha256: str
    worker_code_sha256: str
    manifest_version: str
    database_head: str
    queue_arn: str
    product_id: str
    probe_id: str
    nonce: str
    result_sha256: str
    deadline_seconds: int
    max_spend_usd: str
    max_attempts: int
    owned_resources: tuple[str, ...]

    @classmethod
    def from_manifest(cls, path: Path):
        manifest = json.loads(path.read_text())
        require(
            isinstance(manifest, dict) and type(manifest.get("schema_version")) is int and manifest["schema_version"] == 1, "fixture_version_invalid"
        )
        data = manifest.get("installed_release")
        require(isinstance(data, dict) and set(data) == set(cls.__dataclass_fields__), "fixture_fields_invalid")
        require(isinstance(data.get("owned_resources"), list), "fixture_resources_invalid")
        fixture = cls(**{**data, "owned_resources": tuple(data["owned_resources"])})
        require(isinstance(fixture.account_id, str) and re.fullmatch(r"[0-9]{12}", fixture.account_id) is not None, "fixture_account_invalid")
        require(
            isinstance(fixture.role_arn, str)
            and re.fullmatch(rf"arn:aws:iam::{fixture.account_id}:role/[A-Za-z0-9+=,.@_/-]+", fixture.role_arn) is not None,
            "fixture_role_invalid",
        )
        require(
            isinstance(fixture.package_version, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}(?:[a-z0-9.-]+)?", fixture.package_version) is not None,
            "fixture_package_invalid",
        )
        require(digest(fixture.source_revision, 40), "fixture_source_invalid")
        require(
            all(
                digest(getattr(fixture, field))
                for field in (
                    "gateway_image_digest",
                    "wheel_sha256",
                    "schema_sha256",
                    "cli_manifest_sha256",
                    "frontend_asset_sha256",
                    "result_sha256",
                )
            ),
            "fixture_digest_invalid",
        )
        require(
            isinstance(fixture.worker_code_sha256, str) and re.fullmatch(r"[A-Za-z0-9+/]{43}=", fixture.worker_code_sha256) is not None,
            "fixture_worker_digest_invalid",
        )
        require(digest(fixture.nonce), "fixture_nonce_invalid")
        require(
            all(
                isinstance(getattr(fixture, field), str) and 0 < len(getattr(fixture, field)) <= 256
                for field in (
                    "manifest_version",
                    "database_head",
                    "product_id",
                    "probe_id",
                )
            ),
            "fixture_identity_invalid",
        )
        try:
            _, queue_account, _, _ = queue_scope(fixture.queue_arn)
        except ValueError as error:
            raise ReleaseProofError("fixture_queue_invalid") from error
        require(queue_account == fixture.account_id, "fixture_queue_invalid")
        require(
            isinstance(fixture.max_spend_usd, str)
            and re.fullmatch(r"(?:0|[1-9][0-9]{0,3})\.[0-9]{2}", fixture.max_spend_usd) is not None
            and Decimal(fixture.max_spend_usd) > 0,
            "fixture_spend_invalid",
        )
        require(type(fixture.deadline_seconds) is int and 1 <= fixture.deadline_seconds < 60, "fixture_deadline_invalid")
        require(type(fixture.max_attempts) is int and 1 <= fixture.max_attempts <= 3, "fixture_attempts_invalid")
        require(
            bool(fixture.owned_resources)
            and all(isinstance(item, str) and 0 < len(item) <= 256 for item in fixture.owned_resources)
            and len(fixture.owned_resources) == len(set(fixture.owned_resources)),
            "fixture_resources_invalid",
        )
        return fixture


SOURCES = {
    "identity": "aws-sts",
    "artifact": "versioned-s3",
    "gateway": "kubernetes",
    "schema": "authenticated-runtime",
    "budget": "budget-meter",
    "worker": "aws-lambda",
    "repair": "aws-lambda",
    "cli": "installed-cli",
    "frontend": "deployed-frontend",
    "queue": "aws-sqs",
    "mapping": "aws-lambda",
    "result": "database",
    "profile": "profile-api",
    "cleanup": "native-cleanup",
}


def verify_installed_release(fixture: InstalledFixture, observed: Mapping, *, now=None):
    """Validate evidence supplied by distinct native readers; never call providers here."""
    now = datetime.now(UTC) if now is None else now
    require(isinstance(now, datetime) and now.utcoffset() is not None, "release_clock_invalid")
    require(isinstance(observed, Mapping), "release_observation_invalid")
    require(all(isinstance(observed.get(name), Mapping) for name in SOURCES), "release_readback_missing")
    references = []
    for name, source in SOURCES.items():
        part = observed[name]
        require(part.get("source") == source, f"release_{name}_source_invalid")
        reference = part.get("evidence_ref")
        try:
            observed_at = datetime.fromisoformat(part["observed_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise ReleaseProofError(f"release_{name}_time_invalid") from error
        require(observed_at.utcoffset() is not None and 0 <= (now - observed_at).total_seconds() <= 900, f"release_{name}_time_invalid")
        require(isinstance(reference, str) and 0 < len(reference) <= 1024, f"release_{name}_reference_missing")
        references.append(reference)
    require(len(set(references)) == len(references), "release_evidence_reused")

    identity, artifact, gateway, schema = (observed[name] for name in ("identity", "artifact", "gateway", "schema"))
    require(identity.get("account_id") == fixture.account_id and identity.get("role_arn") == fixture.role_arn, "release_target_changed")
    require(
        artifact.get("manifest_version") == fixture.manifest_version
        and artifact.get("source_revision") == fixture.source_revision
        and artifact.get("wheel_sha256") == fixture.wheel_sha256
        and artifact.get("worker_code_sha256") == fixture.worker_code_sha256,
        "release_artifact_changed",
    )
    require(
        gateway.get("source_revision") == fixture.source_revision
        and gateway.get("image_digest") == fixture.gateway_image_digest
        and isinstance(gateway.get("pod_uid"), str)
        and bool(gateway["pod_uid"]),
        "release_gateway_changed",
    )
    require(
        schema.get("pod_uid") == gateway["pod_uid"]
        and schema.get("nonce") == fixture.nonce
        and schema.get("l5_wheel_sha256") == fixture.wheel_sha256
        and schema.get("l5_package_version") == fixture.package_version
        and schema.get("l5_schema_sha256") == fixture.schema_sha256
        and schema.get("cli_manifest_sha256") == fixture.cli_manifest_sha256
        and schema.get("database_heads") == schema.get("image_heads") == [fixture.database_head],
        "release_schema_changed",
    )
    region, account, environment, service = queue_scope(fixture.queue_arn)
    require(account == fixture.account_id, "release_target_changed")
    for name, function in zip(("worker", "repair"), functions(environment, service), strict=True):
        part = observed[name]
        require(
            part.get("code_sha256") == fixture.worker_code_sha256
            and part.get("function_arn") == f"arn:aws:lambda:{region}:{fixture.account_id}:function:{function}"
            and part.get("role_arn") == f"arn:aws:iam::{fixture.account_id}:role/{function}"
            and part.get("state") == "Active"
            and part.get("last_update_status") == "Successful"
            and isinstance(part.get("revision_id"), str)
            and bool(part["revision_id"]),
            f"release_{name}_changed",
        )
    require(
        observed["cli"].get("manifest_sha256") == fixture.cli_manifest_sha256 and observed["cli"].get("source_revision") == fixture.source_revision,
        "release_cli_changed",
    )
    require(
        observed["frontend"].get("asset_sha256") == fixture.frontend_asset_sha256
        and observed["frontend"].get("source_revision") == fixture.source_revision,
        "release_frontend_changed",
    )
    queue, result = observed["queue"], observed["result"]
    require(
        queue.get("arn") == fixture.queue_arn and isinstance(queue.get("message_id"), str) and bool(queue["message_id"]),
        "release_queue_changed",
    )
    attempts = queue.get("attempts")
    require(isinstance(attempts, list) and 1 <= len(attempts) <= fixture.max_attempts, "release_dispatch_unverified")
    mapping = observed["mapping"]
    require(
        mapping.get("event_source_arn") == fixture.queue_arn
        and mapping.get("function_arn") == observed["worker"]["function_arn"]
        and mapping.get("state") == "Enabled"
        and isinstance(mapping.get("uuid"), str)
        and bool(mapping["uuid"]),
        "release_mapping_changed",
    )
    request_ids = []
    for attempt in attempts:
        require(isinstance(attempt, Mapping), "release_dispatch_unverified")
        request_id, reference = attempt.get("request_id"), attempt.get("evidence_ref")
        require(
            isinstance(request_id, str)
            and bool(request_id)
            and isinstance(reference, str)
            and bool(reference)
            and reference not in references
            and attempt.get("outcome") in ("retry", "completed"),
            "release_dispatch_unverified",
        )
        request_ids.append(request_id)
        references.append(reference)
    require(
        len(set(request_ids)) == len(request_ids)
        and all(attempt["outcome"] == "retry" for attempt in attempts[:-1])
        and attempts[-1]["outcome"] == "completed"
        and queue.get("worker_request_id") == request_ids[-1],
        "release_dispatch_unverified",
    )
    require(
        result.get("message_id") == queue["message_id"]
        and result.get("probe_id") == fixture.probe_id
        and result.get("product_id") == fixture.product_id
        and result.get("state") == "completed"
        and result.get("result_sha256") == fixture.result_sha256,
        "release_result_missing",
    )
    spent = observed["budget"].get("spent_usd")
    require(
        isinstance(spent, str) and re.fullmatch(r"(?:0|[1-9][0-9]{0,3})\.[0-9]{2}", spent) is not None,
        "release_spend_invalid",
    )
    require(Decimal(spent) <= Decimal(fixture.max_spend_usd), "release_spend_exceeded")
    try:
        sent = datetime.fromisoformat(queue["sent_at"])
        stored = datetime.fromisoformat(result["stored_at"])
    except (KeyError, TypeError, ValueError) as error:
        raise ReleaseProofError("release_deadline_invalid") from error
    require(sent.utcoffset() is not None and stored.utcoffset() is not None, "release_deadline_invalid")
    require(0 <= (stored - sent).total_seconds() <= fixture.deadline_seconds, "release_deadline_exceeded")
    require(0 <= (now - stored).total_seconds() <= 900, "release_result_stale")
    profile = observed["profile"]
    before_ref, after_ref = profile.get("before_ref"), profile.get("after_ref")
    require(
        isinstance(before_ref, str)
        and bool(before_ref)
        and isinstance(after_ref, str)
        and bool(after_ref)
        and before_ref != after_ref
        and before_ref not in references
        and after_ref not in references,
        "release_profile_readbacks_missing",
    )
    require(
        isinstance(profile.get("before"), Mapping)
        and profile["before"].get("product_id") == fixture.product_id
        and profile["before"] == profile.get("after"),
        "release_profile_changed",
    )
    cleanup = observed["cleanup"].get("resources")
    require(isinstance(cleanup, list) and len(cleanup) == len(fixture.owned_resources), "release_cleanup_incomplete")
    require(
        all(
            isinstance(item, Mapping)
            and item.get("id") in fixture.owned_resources
            and (item.get("state") == "absent" or (item.get("state") == "retained" and isinstance(item.get("reason"), str) and bool(item["reason"])))
            for item in cleanup
        ),
        "release_cleanup_unverified",
    )
    require({item["id"] for item in cleanup} == set(fixture.owned_resources), "release_cleanup_incomplete")
