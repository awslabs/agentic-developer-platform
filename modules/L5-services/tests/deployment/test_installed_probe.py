"""SCN-01 offline contract checks; these observations are synthetic, not live proof."""

import copy
import json
from datetime import UTC, datetime

import pytest
from l5_services.installed_release_probe import InstalledFixture, ReleaseProofError
from l5_services.installed_release_probe import verify_installed_release as verify_release

ACCOUNT = "000000000000"
SOURCE = "a" * 40
WORKER = "A" * 43 + "="
QUEUE = f"arn:aws:sqs:us-east-1:{ACCOUNT}:adp-dev-l5-work"


SOURCE_TYPES = {
    "identity": "aws-sts",
    "artifact": "versioned-s3",
    "gateway": "kubernetes",
    "schema": "authenticated-runtime",
    "worker": "aws-lambda",
    "repair": "aws-lambda",
    "cli": "installed-cli",
    "frontend": "deployed-frontend",
    "queue": "aws-sqs",
    "mapping": "aws-lambda",
    "result": "database",
    "budget": "budget-meter",
    "profile": "profile-api",
    "cleanup": "native-cleanup",
}


def verify_installed_release(fixture, observed):
    return verify_release(fixture, observed, now=datetime(2026, 10, 26, 12, 1, tzinfo=UTC))


def fixture_data():
    return {
        "account_id": ACCOUNT,
        "role_arn": f"arn:aws:iam::{ACCOUNT}:role/reviewed-deployment",
        "source_revision": SOURCE,
        "package_version": "0.1.0",
        "gateway_image_digest": "b" * 64,
        "wheel_sha256": "c" * 64,
        "schema_sha256": "d" * 64,
        "cli_manifest_sha256": "e" * 64,
        "frontend_asset_sha256": "f" * 64,
        "worker_code_sha256": WORKER,
        "manifest_version": "immutable-version-one",
        "database_head": "l5_worker_repair",
        "queue_arn": QUEUE,
        "product_id": "fixture-product",
        "probe_id": "run-owned-probe",
        "nonce": "1" * 64,
        "result_sha256": "2" * 64,
        "deadline_seconds": 45,
        "max_spend_usd": "0.50",
        "max_attempts": 2,
        "owned_resources": ["run-owned-resource"],
    }


def fixture(tmp_path, data=None):
    manifest = tmp_path / "private-fixture.json"
    manifest.write_text(json.dumps({"schema_version": 1, "installed_release": data if data is not None else fixture_data()}))
    return InstalledFixture.from_manifest(manifest)


def observations():
    found = {
        name: {"source": source, "evidence_ref": f"private/{name}-observation", "observed_at": "2026-10-26T12:01:00+00:00"}
        for name, source in SOURCE_TYPES.items()
    }
    found["identity"].update(account_id=ACCOUNT, role_arn=f"arn:aws:iam::{ACCOUNT}:role/reviewed-deployment")
    found["artifact"].update(manifest_version="immutable-version-one", source_revision=SOURCE, wheel_sha256="c" * 64, worker_code_sha256=WORKER)
    found["gateway"].update(source_revision=SOURCE, image_digest="b" * 64, pod_uid="pod-one")
    found["schema"].update(
        pod_uid="pod-one",
        nonce="1" * 64,
        l5_package_version="0.1.0",
        l5_wheel_sha256="c" * 64,
        l5_schema_sha256="d" * 64,
        cli_manifest_sha256="e" * 64,
        database_heads=["l5_worker_repair"],
        image_heads=["l5_worker_repair"],
    )
    for label in ("worker", "repair"):
        found[label].update(
            code_sha256=WORKER,
            function_arn=f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:adp-dev-l5-{label}",
            role_arn=f"arn:aws:iam::{ACCOUNT}:role/adp-dev-l5-{label}",
            state="Active",
            last_update_status="Successful",
            revision_id=f"rev-{label}",
        )
    found["cli"].update(manifest_sha256="e" * 64, source_revision=SOURCE)
    found["frontend"].update(asset_sha256="f" * 64, source_revision=SOURCE)
    found["queue"].update(
        arn=QUEUE,
        message_id="message-one",
        worker_request_id="lambda-request-one",
        sent_at="2026-10-26T12:00:00+00:00",
        attempts=[{"request_id": "lambda-request-one", "evidence_ref": "private/lambda-attempt-one", "outcome": "completed"}],
    )
    found["mapping"].update(event_source_arn=QUEUE, function_arn=found["worker"]["function_arn"], state="Enabled", uuid="mapping-one")
    found["result"].update(
        message_id="message-one",
        probe_id="run-owned-probe",
        product_id="fixture-product",
        state="completed",
        result_sha256="2" * 64,
        stored_at="2026-10-26T12:00:30+00:00",
    )
    found["budget"].update(spent_usd="0.01")
    profile = {"product_id": "fixture-product", "name": "unchanged"}
    found["profile"].update(before=profile, after=copy.deepcopy(profile), before_ref="private/profile-before", after_ref="private/profile-after")
    found["cleanup"].update(resources=[{"id": "run-owned-resource", "state": "absent"}])
    return found


def test_pinned_installed_release_accepts_independent_bounded_observations(tmp_path):
    verify_installed_release(fixture(tmp_path), observations())


@pytest.mark.parametrize("service", ["devops", "transform", "well-architected", "finops"])
def test_modular_release_checks_owned_workers_not_legacy_shared_workers(tmp_path, service):
    data = fixture_data()
    data["queue_arn"] = QUEUE.replace("-l5-work", f"-l5-{service}-work")
    observed = observations()
    observed["queue"]["arn"] = data["queue_arn"]
    observed["mapping"]["event_source_arn"] = data["queue_arn"]
    for label in ("worker", "repair"):
        for key in ("function_arn", "role_arn"):
            observed[label][key] = observed[label][key].replace("-l5-", f"-l5-{service}-")
    observed["mapping"]["function_arn"] = observed["worker"]["function_arn"]
    selected = fixture(tmp_path, data)
    verify_installed_release(selected, observed)
    observed["worker"]["function_arn"] = observations()["worker"]["function_arn"]
    with pytest.raises(ReleaseProofError, match="release_worker_changed"):
        verify_installed_release(selected, observed)


@pytest.mark.parametrize(
    ("part", "field", "value", "reason"),
    [
        ("identity", "role_arn", "wrong-role", "release_target_changed"),
        ("artifact", "source_revision", "0" * 40, "release_artifact_changed"),
        ("schema", "l5_package_version", "0.2.0", "release_schema_changed"),
        ("gateway", "image_digest", "0" * 64, "release_gateway_changed"),
        ("schema", "l5_wheel_sha256", "0" * 64, "release_schema_changed"),
        ("schema", "observed_at", "2026-10-25T12:01:00+00:00", "release_schema_time_invalid"),
        ("schema", "database_heads", ["old-head"], "release_schema_changed"),
        ("repair", "code_sha256", "0" * 44, "release_repair_changed"),
        ("cli", "manifest_sha256", "0" * 64, "release_cli_changed"),
        ("frontend", "asset_sha256", "0" * 64, "release_frontend_changed"),
        ("queue", "attempts", [], "release_dispatch_unverified"),
        ("budget", "spent_usd", "0.51", "release_spend_exceeded"),
        ("result", "result_sha256", "0" * 64, "release_result_missing"),
        ("result", "stored_at", "2026-10-26T12:03:00+00:00", "release_deadline_exceeded"),
        ("result", "stored_at", "2026-10-25T12:00:30+00:00", "release_deadline_exceeded"),
        ("profile", "after", {"product_id": "other"}, "release_profile_changed"),
        ("cleanup", "resources", [], "release_cleanup_incomplete"),
    ],
)
def test_independent_readback_refuses_mismatch(tmp_path, part, field, value, reason):
    found = observations()
    found[part][field] = value
    with pytest.raises(ReleaseProofError, match=f"^{reason}$"):
        verify_installed_release(fixture(tmp_path), found)


def test_reused_reference_or_unexplained_retention_is_not_proof(tmp_path):
    found = observations()
    found["result"]["evidence_ref"] = found["queue"]["evidence_ref"]
    with pytest.raises(ReleaseProofError, match="release_evidence_reused"):
        verify_installed_release(fixture(tmp_path), found)
    found["result"]["evidence_ref"] = "private/result-observation"
    found["cleanup"]["resources"] = [{"id": "run-owned-resource", "state": "retained"}]
    with pytest.raises(ReleaseProofError, match="release_cleanup_unverified"):
        verify_installed_release(fixture(tmp_path), found)
    found["cleanup"]["resources"][0]["reason"] = "reviewed drain pending"
    verify_installed_release(fixture(tmp_path), found)


def test_profile_requires_two_distinct_authenticated_reads(tmp_path):
    found = observations()
    found["profile"]["after_ref"] = found["profile"]["before_ref"]
    with pytest.raises(ReleaseProofError, match="release_profile_readbacks_missing"):
        verify_installed_release(fixture(tmp_path), found)


def test_unlinked_or_unattributed_attempt_cannot_claim_dispatch(tmp_path):
    found = observations()
    found["queue"]["attempts"].append(dict(found["queue"]["attempts"][0]))
    found["queue"]["attempts"][0]["outcome"] = "retry"
    found["queue"]["attempts"][1]["evidence_ref"] = "private/lambda-attempt-two"
    with pytest.raises(ReleaseProofError, match="release_dispatch_unverified"):
        verify_installed_release(fixture(tmp_path), found)
    found["queue"]["attempts"] = [{"request_id": "request", "evidence_ref": found["schema"]["evidence_ref"], "outcome": "completed"}]
    with pytest.raises(ReleaseProofError, match="release_dispatch_unverified"):
        verify_installed_release(fixture(tmp_path), found)


def test_replayed_result_and_queue_timestamps_cannot_satisfy_fresh_readback(tmp_path):
    found = observations()
    found["queue"]["sent_at"] = "2026-10-25T12:00:00+00:00"
    found["result"]["stored_at"] = "2026-10-25T12:00:30+00:00"
    with pytest.raises(ReleaseProofError, match="release_result_stale"):
        verify_installed_release(fixture(tmp_path), found)


def test_source_and_mapping_cannot_be_self_attested(tmp_path):
    found = observations()
    found["schema"]["source"] = "api-only"
    with pytest.raises(ReleaseProofError, match="release_schema_source_invalid"):
        verify_installed_release(fixture(tmp_path), found)
    found = observations()
    found["mapping"]["state"] = "Disabled"
    with pytest.raises(ReleaseProofError, match="release_mapping_changed"):
        verify_installed_release(fixture(tmp_path), found)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("account_id", "111", "fixture_account_invalid"),
        ("role_arn", "arn:aws:iam::111111111111:role/foreign", "fixture_role_invalid"),
        ("source_revision", "main", "fixture_source_invalid"),
        ("wheel_sha256", "mutable", "fixture_digest_invalid"),
        ("package_version", "latest", "fixture_package_invalid"),
        ("max_spend_usd", "0.00", "fixture_spend_invalid"),
        ("queue_arn", "arn:aws:sqs:us-east-1:111111111111:foreign", "fixture_queue_invalid"),
        ("deadline_seconds", 3600, "fixture_deadline_invalid"),
        ("max_attempts", 0, "fixture_attempts_invalid"),
        ("owned_resources", [], "fixture_resources_invalid"),
    ],
)
def test_unbounded_or_unpinned_private_fixture_is_rejected(tmp_path, field, value, reason):
    data = fixture_data()
    data[field] = value
    with pytest.raises(ReleaseProofError, match=f"^{reason}$"):
        fixture(tmp_path, data)
