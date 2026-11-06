"""Fail-closed predicates for independently collected, authorized recovery evidence."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from l5_services.installed_release_probe import ReleaseProofError, digest, require


@dataclass(frozen=True)
class RecoveryFixture:
    account_id: str
    role_arn: str
    queue_arn: str
    source_revision: str
    wheel_sha256: str
    schema_sha256: str
    current_manifest_version: str
    compatible_manifest_version: str
    current_code_sha256: str
    compatible_code_sha256: str
    run_id: str
    work_id: str
    product_id: str
    source_snapshot_ids: tuple[str, ...]
    owned_resources: tuple[str, ...]
    deadline_seconds: int
    max_attempts: int
    max_spend_usd: str

    @classmethod
    def from_manifest(cls, path: Path):
        manifest = json.loads(path.read_text())
        require(
            isinstance(manifest, dict)
            and type(manifest.get("schema_version")) is int
            and manifest["schema_version"] == 1,
            "recovery_fixture_version_invalid",
        )
        data = manifest.get("recovery")
        require(
            isinstance(data, dict) and set(data) == set(cls.__dataclass_fields__),
            "recovery_fixture_fields_invalid",
        )
        for field in ("source_snapshot_ids", "owned_resources"):
            value = data[field]
            require(
                isinstance(value, list)
                and bool(value)
                and all(
                    isinstance(item, str) and 0 < len(item) <= 256 for item in value
                )
                and len(value) == len(set(value)),
                f"recovery_fixture_{field}_invalid",
            )
        fixture = cls(
            **{
                **data,
                "source_snapshot_ids": tuple(data["source_snapshot_ids"]),
                "owned_resources": tuple(data["owned_resources"]),
            }
        )
        require(
            isinstance(fixture.account_id, str)
            and re.fullmatch(r"[0-9]{12}", fixture.account_id) is not None,
            "recovery_fixture_account_invalid",
        )
        require(
            isinstance(fixture.role_arn, str)
            and re.fullmatch(
                rf"arn:aws:iam::{fixture.account_id}:role/[A-Za-z0-9+=,.@_/-]+",
                fixture.role_arn,
            )
            is not None,
            "recovery_fixture_role_invalid",
        )
        require(
            isinstance(fixture.queue_arn, str)
            and re.fullmatch(
                rf"arn:aws:sqs:[a-z0-9-]+:{fixture.account_id}:adp-(dev|test|prod)-l5-work",
                fixture.queue_arn,
            )
            is not None,
            "recovery_fixture_queue_invalid",
        )
        require(
            digest(fixture.source_revision, 40)
            and digest(fixture.wheel_sha256)
            and digest(fixture.schema_sha256),
            "recovery_fixture_digest_invalid",
        )
        require(
            all(
                isinstance(getattr(fixture, field), str)
                and re.fullmatch(r"[A-Za-z0-9+/]{43}=", getattr(fixture, field))
                is not None
                for field in ("current_code_sha256", "compatible_code_sha256")
            ),
            "recovery_fixture_code_invalid",
        )
        require(
            all(
                isinstance(getattr(fixture, field), str)
                and 0 < len(getattr(fixture, field)) <= 256
                for field in (
                    "current_manifest_version",
                    "compatible_manifest_version",
                    "run_id",
                    "work_id",
                    "product_id",
                )
            )
            and fixture.current_manifest_version != fixture.compatible_manifest_version,
            "recovery_fixture_pin_invalid",
        )
        require(
            type(fixture.deadline_seconds) is int
            and 60 <= fixture.deadline_seconds <= 900,
            "recovery_fixture_deadline_invalid",
        )
        require(
            type(fixture.max_attempts) is int and 1 <= fixture.max_attempts <= 3,
            "recovery_fixture_attempts_invalid",
        )
        require(
            isinstance(fixture.max_spend_usd, str)
            and re.fullmatch(r"(?:0|[1-9][0-9]{0,3})\.[0-9]{2}", fixture.max_spend_usd)
            is not None
            and Decimal(fixture.max_spend_usd) > 0,
            "recovery_fixture_spend_invalid",
        )
        return fixture


SOURCES = {
    "identity": "aws-sts",
    "artifacts": "versioned-s3",
    "before_worker": "aws-lambda",
    "after_worker": "aws-lambda",
    "before_repair": "aws-lambda",
    "after_repair": "aws-lambda",
    "mapping": "aws-lambda",
    "rule": "aws-events",
    "fault": "fault-injector",
    "queue": "aws-sqs",
    "repair": "aws-lambda",
    "before_work": "database",
    "after_work": "database",
    "before_run": "database",
    "after_run": "database",
    "snapshots": "native-storage",
    "provider": "native-provider",
    "admission": "authenticated-api",
    "budget": "budget-meter",
    "cleanup": "native-cleanup",
}


def timestamp(value, now, reason):
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ReleaseProofError(reason) from error
    require(
        moment.utcoffset() is not None and 0 <= (now - moment).total_seconds() <= 900,
        reason,
    )
    return moment


def verify_recovery(fixture: RecoveryFixture, observed: Mapping, *, now=None):
    """Check supplied native observations; this function does not collect live evidence."""
    now = datetime.now(UTC) if now is None else now
    require(
        isinstance(now, datetime) and now.utcoffset() is not None,
        "recovery_clock_invalid",
    )
    require(
        isinstance(observed, Mapping)
        and all(isinstance(observed.get(name), Mapping) for name in SOURCES),
        "recovery_readback_missing",
    )
    references = []
    for name, source in SOURCES.items():
        part = observed[name]
        require(part.get("source") == source, f"recovery_{name}_source_invalid")
        timestamp(part.get("observed_at"), now, f"recovery_{name}_time_invalid")
        reference = part.get("evidence_ref")
        require(
            isinstance(reference, str) and 0 < len(reference) <= 1024,
            f"recovery_{name}_reference_missing",
        )
        references.append(reference)
    require(len(set(references)) == len(references), "recovery_evidence_reused")

    identity, artifacts = observed["identity"], observed["artifacts"]
    require(
        identity.get("account_id") == fixture.account_id
        and identity.get("role_arn") == fixture.role_arn,
        "recovery_target_changed",
    )
    versions = artifacts.get("versions")
    require(
        isinstance(versions, list)
        and len(versions) == 2
        and all(isinstance(item, Mapping) for item in versions)
        and [
            (
                item.get("version"),
                item.get("source_revision"),
                item.get("wheel_sha256"),
                item.get("schema_sha256"),
                item.get("code_sha256"),
            )
            for item in versions
        ]
        == [
            (
                fixture.current_manifest_version,
                fixture.source_revision,
                fixture.wheel_sha256,
                fixture.schema_sha256,
                fixture.current_code_sha256,
            ),
            (
                fixture.compatible_manifest_version,
                fixture.source_revision,
                fixture.wheel_sha256,
                fixture.schema_sha256,
                fixture.compatible_code_sha256,
            ),
        ],
        "recovery_artifact_incompatible",
    )
    region = fixture.queue_arn.split(":")[3]
    environment = (
        fixture.queue_arn.rsplit(":", 1)[-1]
        .removeprefix("adp-")
        .removesuffix("-l5-work")
    )
    worker_arn = f"arn:aws:lambda:{region}:{fixture.account_id}:function:adp-{environment}-l5-worker"
    for label in ("worker", "repair"):
        before, after = observed[f"before_{label}"], observed[f"after_{label}"]
        function_arn = f"arn:aws:lambda:{region}:{fixture.account_id}:function:adp-{environment}-l5-{label}"
        require(
            before.get("function_arn") == after.get("function_arn") == function_arn
            and before.get("code_sha256") == fixture.current_code_sha256
            and after.get("code_sha256") == fixture.compatible_code_sha256
            and all(
                isinstance(item.get("revision_id"), str) and bool(item["revision_id"])
                for item in (before, after)
            )
            and before["revision_id"] != after["revision_id"]
            and after.get("state") == "Active"
            and after.get("last_update_status") == "Successful",
            "recovery_rollback_unverified",
        )
    require(
        observed["mapping"].get("function_arn") == worker_arn
        and observed["mapping"].get("queue_arn") == fixture.queue_arn
        and observed["mapping"].get("state") == "Enabled"
        and observed["rule"].get("state") == "ENABLED",
        "recovery_consumer_unavailable",
    )
    fault, queue, repair = (observed[name] for name in ("fault", "queue", "repair"))
    require(
        fault.get("kind") == "ack_lost_after_send"
        and fault.get("work_id") == fixture.work_id
        and isinstance(fault.get("injection_id"), str)
        and bool(fault["injection_id"]),
        "recovery_fault_unverified",
    )
    require(
        queue.get("queue_arn") == fixture.queue_arn
        and queue.get("work_id") == fixture.work_id
        and queue.get("sent_before_fault") is True
        and queue.get("original_ack_lost") is True
        and queue.get("redelivered") is True,
        "recovery_queue_unverified",
    )
    require(
        repair.get("work_id") == fixture.work_id
        and repair.get("trigger") == "minute-rule"
        and repair.get("re_enqueued") is True
        and repair.get("operator_command") is False,
        "recovery_automatic_repair_missing",
    )
    before_work, after_work = observed["before_work"], observed["after_work"]
    require(
        before_work.get("run_id") == after_work.get("run_id") == fixture.run_id
        and before_work.get("work_id") == after_work.get("work_id") == fixture.work_id
        and before_work.get("completed_at") is None
        and before_work.get("blocked_at") is None
        and after_work.get("completed_at") is not None
        and after_work.get("blocked_at") is None
        and type(after_work.get("attempts")) is int
        and 1 <= after_work["attempts"] <= fixture.max_attempts,
        "recovery_work_unverified",
    )
    due = timestamp(before_work.get("due_at"), now, "recovery_due_invalid")
    before_run, after_run = observed["before_run"], observed["after_run"]
    require(
        before_run.get("run_id") == after_run.get("run_id") == fixture.run_id
        and before_run.get("product_id")
        == after_run.get("product_id")
        == fixture.product_id
        and before_run.get("source_snapshot_ids")
        == after_run.get("source_snapshot_ids")
        == list(fixture.source_snapshot_ids)
        and isinstance(before_run.get("history"), list)
        and bool(before_run["history"])
        and isinstance(after_run.get("history"), list)
        and after_run["history"][: len(before_run["history"])] == before_run["history"]
        and after_run.get("state") == "completed",
        "recovery_history_changed",
    )
    snapshots = observed["snapshots"]
    require(
        isinstance(snapshots.get("before_ref"), str)
        and bool(snapshots["before_ref"])
        and isinstance(snapshots.get("after_ref"), str)
        and bool(snapshots["after_ref"])
        and snapshots["before_ref"] != snapshots["after_ref"]
        and snapshots["before_ref"] not in references
        and snapshots["after_ref"] not in references
        and isinstance(snapshots.get("before"), list)
        and isinstance(snapshots.get("after"), list)
        and snapshots["before"] == snapshots["after"]
        and [item.get("id") for item in snapshots["after"] if isinstance(item, Mapping)]
        == list(fixture.source_snapshot_ids)
        and len(snapshots["after"]) == len(fixture.source_snapshot_ids)
        and all(
            isinstance(item, Mapping)
            and digest(item.get("sha256"))
            and item.get("exists") is True
            for item in snapshots["after"]
        ),
        "recovery_snapshots_missing",
    )
    provider = observed["provider"]
    require(
        provider.get("run_id") == fixture.run_id
        and provider.get("product_id") == fixture.product_id
        and type(provider.get("effect_count")) is int
        and provider["effect_count"] == 1
        and isinstance(provider.get("native_job_id"), str)
        and bool(provider["native_job_id"])
        and before_run.get("native_job_id")
        == after_run.get("native_job_id")
        == provider["native_job_id"],
        "recovery_provider_duplicated",
    )
    polls = provider.get("poll_times")
    require(
        isinstance(polls, list)
        and bool(polls)
        and all(timestamp(item, now, "recovery_poll_invalid") >= due for item in polls),
        "recovery_early_poll",
    )
    require(
        observed["admission"].get("enabled") is False
        and observed["admission"].get("new_run_status") in (403, 409, 503)
        and observed["admission"].get("new_provider_effect_count") == 0,
        "recovery_admission_not_disabled",
    )
    spent = observed["budget"].get("spent_usd")
    require(
        isinstance(spent, str)
        and re.fullmatch(r"(?:0|[1-9][0-9]{0,3})\.[0-9]{2}", spent) is not None
        and Decimal(spent) <= Decimal(fixture.max_spend_usd),
        "recovery_budget_exceeded",
    )
    sent = timestamp(queue.get("sent_at"), now, "recovery_timeline_invalid")
    injected = timestamp(fault.get("injected_at"), now, "recovery_timeline_invalid")
    rolled_back = timestamp(after.get("updated_at"), now, "recovery_timeline_invalid")
    repair_rolled_back = timestamp(
        observed["after_repair"].get("updated_at"), now, "recovery_timeline_invalid"
    )
    repaired = timestamp(repair.get("enqueued_at"), now, "recovery_timeline_invalid")
    completed = timestamp(
        after_work.get("completed_at"), now, "recovery_timeline_invalid"
    )
    require(
        sent <= injected <= rolled_back <= repaired <= completed
        and injected <= repair_rolled_back <= repaired
        and due <= completed
        and (completed - sent).total_seconds() <= fixture.deadline_seconds,
        "recovery_deadline_exceeded",
    )
    cleanup = observed["cleanup"].get("resources")
    require(
        isinstance(cleanup, list)
        and len(cleanup) == len(fixture.owned_resources)
        and all(
            isinstance(item, Mapping)
            and item.get("id") in fixture.owned_resources
            and (
                item.get("state") == "absent"
                or (
                    item.get("state") == "retained"
                    and isinstance(item.get("reason"), str)
                    and bool(item["reason"])
                )
            )
            for item in cleanup
        )
        and {item["id"] for item in cleanup} == set(fixture.owned_resources),
        "recovery_cleanup_unverified",
    )
    require(
        observed["cleanup"].get("failed") is False
        or (
            observed["cleanup"].get("failed") is True
            and any(item["state"] == "retained" for item in cleanup)
        ),
        "recovery_cleanup_failure_hidden",
    )
