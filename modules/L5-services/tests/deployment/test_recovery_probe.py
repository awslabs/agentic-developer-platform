"""SCN-02 synthetic observations validate a contract, not deployed recovery."""

import copy
import json
from datetime import UTC, datetime

import pytest
from l5_services.installed_release_probe import ReleaseProofError
from l5_services.recovery_probe import SOURCES, RecoveryFixture, verify_recovery

ACCOUNT = "000000000000"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/reviewed-deployment"
QUEUE = f"arn:aws:sqs:us-east-1:{ACCOUNT}:adp-dev-l5-work"
WORKER = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:adp-dev-l5-worker"
NOW = datetime(2026, 10, 26, 12, 2, tzinfo=UTC)


def data():
    return {
        "account_id": ACCOUNT,
        "role_arn": ROLE,
        "queue_arn": QUEUE,
        "source_revision": "c" * 40,
        "wheel_sha256": "a" * 64,
        "schema_sha256": "b" * 64,
        "current_manifest_version": "version-current",
        "compatible_manifest_version": "version-compatible",
        "current_code_sha256": "A" * 43 + "=",
        "compatible_code_sha256": "B" * 43 + "=",
        "run_id": "run-owned",
        "work_id": "work-owned",
        "product_id": "product-owned",
        "source_snapshot_ids": ["snapshot-one"],
        "owned_resources": ["resource-owned"],
        "deadline_seconds": 180,
        "max_attempts": 2,
        "max_spend_usd": "0.50",
    }


def fixture(tmp_path, payload=None):
    path = tmp_path / "private-recovery-fixture.json"
    path.write_text(
        json.dumps(
            {"schema_version": 1, "recovery": data() if payload is None else payload}
        )
    )
    return RecoveryFixture.from_manifest(path)


def observations():
    result = {
        name: {
            "source": source,
            "evidence_ref": f"private/{name}",
            "observed_at": "2026-10-26T12:02:00+00:00",
        }
        for name, source in SOURCES.items()
    }
    result["identity"].update(account_id=ACCOUNT, role_arn=ROLE)
    result["artifacts"]["versions"] = [
        {
            "version": version,
            "source_revision": "c" * 40,
            "wheel_sha256": "a" * 64,
            "schema_sha256": "b" * 64,
            "code_sha256": code,
        }
        for version, code in (
            ("version-current", "A" * 43 + "="),
            ("version-compatible", "B" * 43 + "="),
        )
    ]
    result["before_worker"].update(
        function_arn=WORKER, code_sha256="A" * 43 + "=", revision_id="before"
    )
    result["after_worker"].update(
        function_arn=WORKER,
        code_sha256="B" * 43 + "=",
        revision_id="after",
        state="Active",
        last_update_status="Successful",
        updated_at="2026-10-26T12:00:50+00:00",
    )
    result["before_repair"].update(
        function_arn=WORKER.replace("worker", "repair"),
        code_sha256="A" * 43 + "=",
        revision_id="repair-before",
    )
    result["after_repair"].update(
        function_arn=WORKER.replace("worker", "repair"),
        code_sha256="B" * 43 + "=",
        revision_id="repair-after",
        state="Active",
        last_update_status="Successful",
        updated_at="2026-10-26T12:00:51+00:00",
    )
    result["mapping"].update(function_arn=WORKER, queue_arn=QUEUE, state="Enabled")
    result["rule"]["state"] = "ENABLED"
    result["fault"].update(
        kind="ack_lost_after_send",
        work_id="work-owned",
        injection_id="injection-one",
        injected_at="2026-10-26T12:00:40+00:00",
    )
    result["queue"].update(
        queue_arn=QUEUE,
        work_id="work-owned",
        sent_before_fault=True,
        original_ack_lost=True,
        redelivered=True,
        sent_at="2026-10-26T12:00:30+00:00",
    )
    result["repair"].update(
        work_id="work-owned",
        trigger="minute-rule",
        re_enqueued=True,
        operator_command=False,
        enqueued_at="2026-10-26T12:01:00+00:00",
    )
    result["before_work"].update(
        due_at="2026-10-26T12:00:20+00:00",
        run_id="run-owned",
        work_id="work-owned",
        completed_at=None,
        blocked_at=None,
    )
    result["after_work"].update(
        run_id="run-owned",
        work_id="work-owned",
        completed_at="2026-10-26T12:01:30+00:00",
        blocked_at=None,
        attempts=2,
    )
    for label in ("before_run", "after_run"):
        result[label].update(
            run_id="run-owned",
            product_id="product-owned",
            source_snapshot_ids=["snapshot-one"],
            history=["admitted"],
            native_job_id="native-job",
        )
    result["after_run"].update(state="completed", history=["admitted", "repaired"])
    snapshots = [{"id": "snapshot-one", "sha256": "d" * 64, "exists": True}]
    result["snapshots"].update(
        before_ref="private/snapshots-before",
        after_ref="private/snapshots-after",
        before=snapshots,
        after=copy.deepcopy(snapshots),
    )
    result["provider"].update(
        run_id="run-owned",
        product_id="product-owned",
        effect_count=1,
        native_job_id="native-job",
        poll_times=["2026-10-26T12:01:20+00:00"],
    )
    result["admission"].update(
        enabled=False, new_run_status=503, new_provider_effect_count=0
    )
    result["budget"]["spent_usd"] = "0.25"
    result["cleanup"].update(
        failed=False, resources=[{"id": "resource-owned", "state": "absent"}]
    )
    return result


def verify(tmp_path, observed):
    verify_recovery(fixture(tmp_path), observed, now=NOW)


def test_recovery_requires_pinned_rollback_and_independent_readback(tmp_path):
    verify(tmp_path, observations())
    found = observations()
    found["cleanup"].update(
        failed=True,
        resources=[
            {
                "id": "resource-owned",
                "state": "retained",
                "reason": "cleanup retry owed",
            }
        ],
    )
    verify(tmp_path, found)


@pytest.mark.parametrize(
    ("part", "field", "value", "reason"),
    [
        ("artifacts", "versions", [], "recovery_artifact_incompatible"),
        ("artifacts", "source", "untrusted", "recovery_artifacts_source_invalid"),
        ("after_worker", "revision_id", "before", "recovery_rollback_unverified"),
        ("after_repair", "code_sha256", "A" * 43 + "=", "recovery_rollback_unverified"),
        ("mapping", "state", "Disabled", "recovery_consumer_unavailable"),
        ("fault", "kind", "uncontrolled", "recovery_fault_unverified"),
        ("queue", "redelivered", False, "recovery_queue_unverified"),
        ("repair", "operator_command", True, "recovery_automatic_repair_missing"),
        (
            "before_work",
            "completed_at",
            "2026-10-26T12:00:00+00:00",
            "recovery_work_unverified",
        ),
        ("after_work", "attempts", 4, "recovery_work_unverified"),
        ("after_run", "source_snapshot_ids", [], "recovery_history_changed"),
        ("after_run", "history", ["replaced"], "recovery_history_changed"),
        (
            "snapshots",
            "after_ref",
            "private/snapshots-before",
            "recovery_snapshots_missing",
        ),
        (
            "snapshots",
            "after",
            [{"id": "snapshot-one", "sha256": "e" * 64, "exists": True}],
            "recovery_snapshots_missing",
        ),
        ("provider", "effect_count", 2, "recovery_provider_duplicated"),
        (
            "provider",
            "poll_times",
            ["2026-10-26T12:00:10+00:00"],
            "recovery_early_poll",
        ),
        (
            "admission",
            "new_provider_effect_count",
            1,
            "recovery_admission_not_disabled",
        ),
        ("budget", "spent_usd", "0.51", "recovery_budget_exceeded"),
        (
            "repair",
            "enqueued_at",
            "2026-10-26T12:05:00+00:00",
            "recovery_timeline_invalid",
        ),
        ("cleanup", "resources", [], "recovery_cleanup_unverified"),
        ("cleanup", "failed", True, "recovery_cleanup_failure_hidden"),
    ],
)
def test_missing_or_conflicting_recovery_evidence_is_rejected(
    tmp_path, part, field, value, reason
):
    found = observations()
    found[part][field] = value
    with pytest.raises(ReleaseProofError, match=f"^{reason}$"):
        verify(tmp_path, found)


def test_slow_recovery_and_reused_evidence_cannot_pass(tmp_path):
    found = observations()
    found["after_work"]["completed_at"] = "2026-10-26T12:04:00+00:00"
    with pytest.raises(ReleaseProofError, match="recovery_deadline_exceeded"):
        verify_recovery(
            fixture(tmp_path), found, now=datetime(2026, 10, 26, 12, 5, tzinfo=UTC)
        )
    found = observations()
    found["repair"]["evidence_ref"] = found["queue"]["evidence_ref"]
    with pytest.raises(ReleaseProofError, match="recovery_evidence_reused"):
        verify(tmp_path, found)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "role_arn",
            "arn:aws:iam::111111111111:role/foreign",
            "recovery_fixture_role_invalid",
        ),
        (
            "queue_arn",
            "arn:aws:sqs:us-east-1:111111111111:foreign",
            "recovery_fixture_queue_invalid",
        ),
        ("wheel_sha256", "latest", "recovery_fixture_digest_invalid"),
        (
            "compatible_manifest_version",
            "version-current",
            "recovery_fixture_pin_invalid",
        ),
        ("deadline_seconds", 3600, "recovery_fixture_deadline_invalid"),
        ("max_attempts", 0, "recovery_fixture_attempts_invalid"),
        ("max_spend_usd", "0.00", "recovery_fixture_spend_invalid"),
        ("source_snapshot_ids", [], "recovery_fixture_source_snapshot_ids_invalid"),
    ],
)
def test_unbounded_or_unpinned_recovery_fixture_is_rejected(
    tmp_path, field, value, reason
):
    payload = copy.deepcopy(data())
    payload[field] = value
    with pytest.raises(ReleaseProofError, match=f"^{reason}$"):
        fixture(tmp_path, payload)
