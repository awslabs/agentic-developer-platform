import copy
import errno
import hashlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runner_acceptance_verify", ROOT / "verify.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)
NOW = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)


def record_hashes(records):
    return [hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            for record in records]


@pytest.fixture
def sample():
    """Synthetic transport-independent counterexamples, never live evidence."""
    start = NOW - timedelta(minutes=40)
    at = lambda seconds: (start + timedelta(seconds=seconds)).isoformat()
    scope = {
        "schema_version": 1, "run_owned": True, "run_id": "12345678-1234-4234-8234-123456789012",
        "source_revision": "a" * 40, "account_id": "123456789012",
        "group_a": 10, "group_b": 20, "project_a": 101, "project_b": 202, "runner_a": 42,
        "project_namespace_a": 11, "project_namespace_b": 21,
        "vpc_id": "vpc-a", "private_subnet_ids": ["subnet-a"], "job_security_group_ids": ["sg-a"],
        "ami_id": "ami-a", "asg_names": ["owned-test-asg"],
        "network_targets": [{"name": name, "address": "fd00:ec2::254" if name == "metadata_ipv6" else
                             "169.254.169.254" if name == "metadata_ipv4" else "192.0.2.1",
                             "port": 80 if name in verifier.METADATA_TARGETS else 443, "kind": "https" if name in {"gitlab", "doh"} else
                             "dns" if name == "alternate_dns" else "tcp",
                             "expected": "allowed" if name in verifier.POSITIVE_TARGETS else "blocked",
                             **({"hostname": "example.test", "expected_status": 200} if name in {"gitlab", "doh"} else {})}
                            for name in sorted(verifier.NEGATIVE_TARGETS | verifier.POSITIVE_TARGETS)],
        "identity_role_arns": {name: f"arn:aws:iam::123456789012:role/test-{name}" for name in verifier.IDENTITY_CASES},
    }
    hosts = [{"instance_id": f"i-{number}", "vpc_id": "vpc-a", "subnet_id": "subnet-a",
              "security_group_ids": ["sg-a"], "public_ip": None, "public_ipv6": [],
              "ami_id": "ami-a", "group_id": 10, "manager_credentials_mounted": False,
              "instance_reused": False} for number in range(5)]
    cold = [{"instance_id": f"i-{number}", "denial_installed_at": at(1), "executor_ready_at": at(2),
             "first_instruction_at": at(3), "trusted_host_verified": True, "first_instruction_denied": True,
             "boot_id": "00000000-0000-0000-0000-000000000001", "stream_id": f"00000000-0000-0000-0000-00000000000{number+1}",
             "job_id": 111 + number, "pipeline_id": 300,
             "metadata_ipv4_denied": True, "metadata_ipv6_denied": True,
             "positive_controls_succeeded": True} for number in range(3)]
    cases = json.loads((ROOT / "executor-negative-cases.json").read_text())["cases"]
    evidence = {
        "run_id": scope["run_id"], "source_revision": scope["source_revision"], "account_id": scope["account_id"],
        "started_at": at(0), "completed_at": at(1800),
        "collection_sources": ["gitlab_api", "ec2_api", "autoscaling_api", "kubernetes_api", "trusted_host"],
        "identity_checks": [{"name": name, "result": result, "verified_caller_role_arn": scope["identity_role_arns"][name],
                             "aws_request_id": "synthetic-request", "transport_succeeded": True,
                             **({"action": "secretsmanager:GetSecretValue", "secret_id": "adp/dev/gitlab/ca-chain",
                                 "exit_code": 0 if name == "manager_ca" else 1,
                                 "error_code": None if name == "manager_ca" else "AccessDeniedException"}
                                if name in {"manager_ca", "gateway_ca", "agent_ca"} else {})}
                            for name, result in verifier.IDENTITY_CASES.items()],
        "ec01": {"registrations": [{"runner_id": 42, "group_id": 10,
                                     "token_secret_name": "adp/dev/gitlab-runner/10"}], "instance_runner_ids": []},
        "gitlab": {
            "a": {"group_id": 10, "project_id": 101, "job_id": 111, "pipeline_id": 300,
                  "runner_id": 42, "status": "success", "runner_trace_line": "Running on runner-example via host",
                  "duration_seconds": 10, "tag_list": ["adp-group-10"], "instance_id": "i-0"},
            "b": {"group_id": 20, "project_id": 202, "job_id": 222, "pipeline_id": 350,
                  "tag_list": ["adp-group-10"], "final_status": "canceled",
                  "pending_observations": [{"observed_at": at(seconds), "status": "pending", "runner_id": None,
                                            "job_id": 222, "pipeline_id": 350, "source_revision": "a" * 40,
                                            "tag_list": ["adp-group-10"]}
                                           for seconds in range(0, 301, 30)]},
            "runner": {"id": 42, "runner_type": "group_type", "group_ids": [10], "locked": True, "run_untagged": False},
        },
        "hosts": hosts,
        "probes": [{"name": name, "result": "blocked", "transport_connected": False,
                    "error_kind": "timeout", "elapsed_seconds": 3,
                    **({"connection_attempted": True} if name in verifier.METADATA_TARGETS else {})}
                   for name in sorted(verifier.NEGATIVE_TARGETS)]
                  + [{"name": name, "result": "allowed", "transport_connected": True, "elapsed_seconds": .1} for name in sorted(verifier.POSITIVE_TARGETS)],
        "network_job_id": 490,
        "independent_target_controls": [{"name": name, "reachable": True, "source": "trusted_reference_host",
                                         "address": "fd00:ec2::254" if name == "metadata_ipv6" else
                                         "169.254.169.254" if name == "metadata_ipv4" else "192.0.2.1",
                                         "port": 80 if name in verifier.METADATA_TARGETS else 443}
                                        for name in sorted(verifier.NEGATIVE_TARGETS)],
        "cache_roundtrip": {"writer_group_id": 10, "reader_group_id": 10, "upload_succeeded": True,
                            "download_succeeded": True, "expected_sha256": "b" * 64, "downloaded_sha256": "b" * 64},
        "executor_negatives": [{"name": case["name"], "gitlab_job_id": 500 + index, "executor": "docker-autoscaler",
                                "outcome": "constrained", "trusted_host_verified": True, "invalid_yaml": False,
                                "privileged": False, "uid": 10001, "added_capabilities": [], "host_namespaces": [],
                                "host_mounts": [], "devices": [], "credential_mounts": []} for index, case in enumerate(cases)],
        "cold_starts": cold, "daemon_restart": {**cold[0], "stream_id": "00000000-0000-0000-0000-000000000005",
            "instance_id": "i-4", "job_id": 114, "denial_installed_at": at(5.5),
            "executor_ready_at": at(7), "first_instruction_at": at(8)},
        "bootstrap_failure": {"instance_id": "i-3", "job_id": 199, "executor_ready": False, "accepted_jobs": 0,
                              "trusted_host_verified": True},
        "cleanup": [{"name": name, "elapsed_seconds": 600, "asg_desired": 0, "asg_instances": [],
                     "job_id": {"legal": 111, "failed": 901, "cancelled": 902, "manager_loss": 903}[name],
                     "pipeline_id": 300 if name == "legal" else 300 + index, "job_status": {"legal": "success", "failed": "failed",
                     "cancelled": "canceled", "manager_loss": "canceled"}[name],
                     "group_id": 10, "runner_id": 42, "instance_id": f"i-{index}",
                     "job_finished_at": at(0 if name == "legal" else 300),
                     "observed_at": at(600 if name == "legal" else 900),
                     "aws_request_ids": {"autoscaling": f"synthetic-asg-{index}",
                                         "ec2": [f"synthetic-ec2-{index}"]},
                     "manager_resource_version": f"synthetic-manager-{index}",
                     **({"manager_loss_receipt": {"unready_at": at(310),
                         "unready_ready_replicas": 0, "unready_resource_version": "synthetic-before-restart"}}
                        if name == "manager_loss" else {}),
                     "ec2_nonterminated_instances": [], "manager_ready": 1, "manager_desired": 1,
                     "observed_asg_names": ["owned-test-asg"]}
                    for index, name in enumerate(sorted(verifier.LIFECYCLE_CASES))],
        "instance_plan_exit_codes": {"absent": 0, "present": 0, "after_teardown": 0},
        "instance_plan_receipts": [{"stage": stage, "run_id": scope["run_id"],
                                    "account_id": scope["account_id"], "source_revision": scope["source_revision"],
                                    "working_directory": "modules/source-control/gitlab/infra",
                                    "started_at": at(seconds), "completed_at": at(seconds + 1), "exit_code": 0,
                                    "backend_sha256": "1" * 64, "variables_sha256": "2" * 64,
                                    "output_sha256": "3" * 64}
                                   for stage, seconds in (("absent", 100), ("present", 200), ("after_teardown", 1500))],
        "fixture_teardown": {"run_id": scope["run_id"], "account_id": scope["account_id"],
                             "source_revision": scope["source_revision"], "owner_parent_group_id": 106,
                             "backend_key": f'dev/qualification/{scope["run_id"]}/terraform.tfstate',
                             "backend_sha256": "4" * 64, "runner_backend_sha256": "5" * 64,
                             "owned_resource_ids_before": ["synthetic-owned-listener"],
                             "owned_resource_ids_after": [], "post_destroy_plan_exit_code": 0,
                             "provider_request_ids": ["synthetic-provider-request"], "completed_at": at(1200)},
        "teardown_complete": True, "token_revoked": True,
        "token_revocation": {"runner_id": 42, "baseline_http_status": 200, "after_revoke_http_status": 403,
                             "same_secret_version": True, "tls_verified": True},
        "secret_scan": {"run_id": scope["run_id"], "source_revision": scope["source_revision"],
                        "workflow_runs": [{"run_id": 600 + index, "workflow": workflow,
                                           "head_sha": scope["source_revision"], "conclusion": "success",
                                           "job_ids": [700 + index]}
                                          for index, workflow in enumerate(("gitlab-infra-apply.yml",
                                                                            "gitlab-runners-apply.yml"))],
                        "full_logs_scanned": True, "state_scanned": True, "artifacts_scanned": True,
                        "canary_hits": 0, "ca_pem_hits": 0, "log_job_ids": [700, 701],
                        "state_count": 1, "artifact_count": 1, "sha256s": ["6" * 64] * 4},
    }
    for row in evidence["executor_negatives"]:
        if row["name"] in {"arbitrary_image", "arbitrary_service", "root_user"}:
            row.update(outcome="refused_before_script", script_started=False, job_container_created=False,
                       refusal_reason={"arbitrary_image": "image_not_allowed", "arbitrary_service": "service_not_allowed",
                                       "root_user": "user_not_allowed"}[row["name"]])
            for field in ("uid", "privileged", "added_capabilities", "host_namespaces", "host_mounts", "devices", "credential_mounts"):
                del row[field]
        else:
            count = 2 if row["name"] == "host_device" else 4 if row["name"] == "manager_credentials" else 1
            row.update(instance_id=f'i-{20 + row["gitlab_job_id"] - 500}', pipeline_id=300,
                       probe={"name": row["name"], "trusted_host_verified": False,
                              "process": {"uid": 10001, "effective_capabilities": 0, "no_new_privileges": True},
                              "probe_result": "requires_independent_trace_scan" if row["name"] == "debug_trace" else "inaccessible",
                              "observations": [] if row["name"] == "debug_trace" else
                              [{"result": "inaccessible", "errno": errno.EPERM} for _ in range(count)],
                              **({"credential_environment_present": False} if row["name"] == "manager_credentials" else {})})
    def ns(seconds):
        return int((start + timedelta(seconds=seconds)).timestamp() * 10**9)

    def record(boot, stream, sequence, kind, at_seconds, data):
        return {"schema_version": 1, "boot_id": boot, "stream_id": stream, "sequence": sequence,
                "observer_sha256": "a" * 64, "observed_at_unix_ns": ns(at_seconds),
                "kind": kind, "data": data}

    histories = []
    for number, host_row in enumerate(hosts):
        rows = []
        attempts = [cold[number]] if number < 3 else []
        if number == 4:
            attempts.append(evidence["daemon_restart"])
            rows.append(record(cold[0]["boot_id"], "00000000-0000-0000-0000-000000000007",
                               1, "stream_started", 2, {"executor_ready": False,
                               "service_invocations": {"docker.service": "1" * 32, "adp-job-firewall.service": "2" * 32},
                               "host_enforcement": {"status": "verified", "executor_ready_at_unix_ns": None,
                                                    "denial_installed_at_unix_ns": ns(1)}}))
        for index, attempt in enumerate(attempts):
            stream = attempt["stream_id"]
            boot = attempt["boot_id"]
            offset = 5 if number == 4 else 0
            rows.append(record(boot, stream, 1, "stream_started", offset + (1 if number == 4 else 0),
                               {"executor_ready": False,
                                "service_invocations": {"docker.service": "3" * 32, "adp-job-firewall.service": "4" * 32},
                                "host_enforcement": {"status": "verified", "executor_ready_at_unix_ns": None,
                                                     "denial_installed_at_unix_ns": ns(5.5 if number == 4 else 0)}}))
            container = str(number) * 64
            rows.append(record(boot, stream, 2, "container_event", offset + 2.5,
                        {"action": "start", "container_id": container, "job_id": attempt["job_id"],
                         "project_id": scope["project_a"], "pipeline_id": attempt["pipeline_id"],
                         "source_revision": scope["source_revision"], "docker_time_unix_ns": ns(offset + 2.5)}))
            rows.append(record(boot, stream, 3, "container_observation", offset + 2.6,
                        {"container_id": container, "job_id": attempt["job_id"], "host_enforcement": {"status": "verified",
                         "denial_installed_at_unix_ns": ns(5.5 if number == 4 else 1),
                         "executor_ready_at_unix_ns": ns(offset + 2)}}))
        if number == 3:
            rows = [record(cold[0]["boot_id"], "00000000-0000-0000-0000-000000000006", 1,
                           "stream_started", 1, {"executor_ready": False}),
                    record(cold[0]["boot_id"], "00000000-0000-0000-0000-000000000006", 2,
                           "heartbeat", 12, {"executor_ready": False})]
        histories.append({"scope": {"run_id": scope["run_id"], "account_id": scope["account_id"],
                         "observer_sha256": "a" * 64,
                         "instance": {"instance_id": host_row["instance_id"], "ami_id": scope["ami_id"],
                          "vpc_id": scope["vpc_id"], "group_id": scope["group_a"],
                          "security_group_ids": scope["job_security_group_ids"],
                          "instance_profile": None, "public_ip": None}},
                         "history_status": "contiguous_so_far", "records": rows,
                         "snapshots": [{"aws_request_id": "synthetic-provider-request", "problems": [],
                                        "record_sha256s": record_hashes(rows),
                                        "provider_timestamp": at(20), "observed_at": at(21)}]})
    for index, row in enumerate(evidence["executor_negatives"]):
        if row["outcome"] != "constrained":
            continue
        instance_id = row["instance_id"]
        hosts.append({**hosts[0], "instance_id": instance_id})
        stream = f"00000000-0000-0000-0000-{index + 20:012x}"
        container = f"{index + 10:064x}"
        identity = {"job_id": row["gitlab_job_id"], "project_id": scope["project_a"],
                    "pipeline_id": row["pipeline_id"], "source_revision": scope["source_revision"]}
        observed = {**identity, "source": "trusted_host_docker_and_kernel", "kind": "build",
                    "container_id": container, "image_id": "sha256:" + "c" * 64,
                    "configured_user": "10001:10001", "privileged": False, "cap_add": [], "cap_drop": ["ALL"],
                    "security_options": ["no-new-privileges:true"], "network_mode": "private", "pid_mode": "",
                    "ipc_mode": "private", "devices": [], "device_requests_present": False,
                    "mounts": [{"Type": "volume", "Destination": "/builds"}],
                    "kernel": {"uids": [10001] * 4, "effective_capabilities_hex": "0" * 16,
                               "no_new_privileges": True,
                               "namespaces": {name: {"same_as_host": False} for name in ("net", "pid", "ipc", "mnt")}},
                    "host_enforcement": {"status": "verified"}}
        records = [record(stream, stream, 1, "stream_started", 1, {"executor_ready": False}),
                   record(stream, stream, 2, "container_event", 2,
                          {**identity, "action": "start", "container_id": container, "docker_time_unix_ns": ns(2)}),
                   record(stream, stream, 3, "container_observation", 3, observed)]
        histories.append({"scope": {"run_id": scope["run_id"], "account_id": scope["account_id"],
                                    "observer_sha256": "a" * 64,
                                    "instance": {"instance_id": instance_id, "ami_id": scope["ami_id"],
                                                 "vpc_id": scope["vpc_id"], "group_id": scope["group_a"],
                                                 "security_group_ids": scope["job_security_group_ids"],
                                                 "instance_profile": None, "public_ip": None}},
                          "history_status": "contiguous_so_far", "records": records,
                          "snapshots": [{"aws_request_id": "synthetic-provider-request", "problems": [],
                                         "record_sha256s": record_hashes(records),
                                         "provider_timestamp": at(20), "observed_at": at(21)}]})
    evidence["host_histories"] = histories
    return scope, evidence


def test_complete_synthetic_evidence_passes_validator_only(sample):
    scope, evidence = sample
    assert verifier.verify(scope, evidence, now=NOW)["verdict"] == "pass"


@pytest.mark.parametrize("change,reason", [
    (lambda evidence: evidence.pop("instance_plan_receipts"), "instance_plan_receipts_missing"),
    (lambda evidence: evidence["instance_plan_receipts"].pop(), "instance_plan_receipts_missing"),
    (lambda evidence: evidence["instance_plan_receipts"][1].update(exit_code=2), "instance_plan_receipt_invalid"),
    (lambda evidence: evidence["instance_plan_receipts"][1].update(backend_sha256="a" * 64),
     "instance_plan_inputs_changed"),
    (lambda evidence: evidence["instance_plan_receipts"][2].update(started_at=evidence["instance_plan_receipts"][0]["started_at"]),
     "instance_plan_receipt_invalid"),
    (lambda evidence: evidence["fixture_teardown"].update(owner_parent_group_id=48), "fixture_teardown_unproven"),
    (lambda evidence: evidence["fixture_teardown"].update(backend_key="dev/modules/gitlab/terraform.tfstate"),
     "fixture_teardown_unproven"),
    (lambda evidence: evidence["fixture_teardown"].update(backend_sha256="1" * 64), "fixture_teardown_unproven"),
    (lambda evidence: evidence["fixture_teardown"].update(owned_resource_ids_after=["synthetic-orphan"]),
     "fixture_teardown_unproven"),
    (lambda evidence: evidence["fixture_teardown"].update(post_destroy_plan_exit_code=2), "fixture_teardown_unproven"),
    (lambda evidence: evidence["fixture_teardown"].update(completed_at=evidence["instance_plan_receipts"][2]["completed_at"]),
     "fixture_teardown_unproven"),
    (lambda evidence: evidence["secret_scan"].update(artifacts_scanned=False), "secret_safety_unproven"),
    (lambda evidence: evidence["secret_scan"].update(ca_pem_hits=1), "secret_safety_unproven"),
    (lambda evidence: evidence["secret_scan"]["workflow_runs"][0].update(head_sha="b" * 40), "secret_scan_run_wrong"),
    (lambda evidence: evidence["secret_scan"].update(log_job_ids=[700]), "secret_scan_coverage_missing"),
])
def test_ec04_plan_fixture_and_log_receipts_fail_closed(sample, change, reason):
    scope, evidence = sample
    change(evidence)
    with pytest.raises(verifier.IncompleteEvidence, match=reason):
        verifier.verify(scope, evidence, now=NOW)


def test_pre_teardown_verdict_never_requires_post_destroy_receipts(sample):
    scope, evidence = sample
    evidence["instance_plan_exit_codes"].pop("after_teardown")
    evidence["instance_plan_receipts"].pop()
    evidence.pop("fixture_teardown")
    assert verifier.verify(scope, evidence, now=NOW, final=False)["verdict"] == "pre_teardown_checks_passed"


@pytest.mark.parametrize("change,reason", [
    (lambda evidence: evidence["ec01"]["registrations"][0].update(group_id=20), "registration_inventory_wrong"),
    (lambda evidence: evidence["ec01"].update(registrations=[]), "registration_inventory_missing"),
    (lambda evidence: evidence["ec01"].update(instance_runner_ids=[42]), "instance_runner_scope_unproven"),
    (lambda evidence: evidence["identity_checks"][0].update(secret_id="unrelated"), "ca_identity_probe_unproven"),
    (lambda evidence: evidence["identity_checks"][2].update(exit_code=0), "ca_identity_probe_unproven"),
])
def test_ec01_receipts_fail_closed(sample, change, reason):
    scope, evidence = sample
    change(evidence)
    with pytest.raises(verifier.IncompleteEvidence, match=reason):
        verifier.verify(scope, evidence, now=NOW)


@pytest.mark.parametrize("mutation,reason", [
    ("none", None), ("unrelated_product", None), ("extra_secret", "live_token_inventory_wrong"),
    ("wrong_group", "live_runner_scope_wrong"), ("instance_runner", "live_instance_runner_scope_wrong"),
])
def test_ec01_live_provider_inventory_matches_receipts(sample, mutation, reason):
    scope, evidence = sample
    secrets = [{"Name": "adp/dev/gitlab-runner/10"}]
    runner = {"id": 42, "runner_type": "group_type", "locked": True, "run_untagged": False,
              "groups": [{"id": 10}]}
    instance_runners = []
    if mutation == "unrelated_product":
        secrets.append({"Name": "adp/dev/gitlab-runner/37"})
    if mutation == "extra_secret":
        secrets.append({"Name": "adp/dev/gitlab-runner/20"})
    if mutation == "wrong_group":
        runner["groups"] = [{"id": 20}]
    if mutation == "instance_runner":
        instance_runners.append({"id": 42})
    aws = Mock()
    aws.client.return_value.get_paginator.return_value.paginate.return_value = [{"SecretList": secrets}]
    gitlab = lambda path: runner if path == "runners/42" else instance_runners
    if reason:
        with pytest.raises(verifier.IncompleteEvidence, match=reason):
            verifier.live_ec01_readback(scope, evidence, aws=aws, gitlab=gitlab)
    else:
        verifier.live_ec01_readback(scope, evidence, aws=aws, gitlab=gitlab)
    aws.client.return_value.get_paginator.assert_called_once_with("list_secrets")


def test_qualification_binds_exact_private_release_lock(sample, tmp_path):
    _, evidence = sample
    lock = tmp_path / "reviewed-lock.json"
    lock.write_text('{"synthetic":"reviewed"}')
    lock.chmod(0o600)
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    evidence["release_lock_sha256"] = digest
    verifier.verify_release_binding(evidence, private_lock=lock, approved_sha256=digest)
    with pytest.raises(verifier.IncompleteEvidence, match="release_lock_binding_missing"):
        verifier.verify_release_binding(evidence)
    with pytest.raises(verifier.IncompleteEvidence, match="release_lock_binding_missing"):
        verifier.verify_release_binding(evidence, private_lock=lock)
    with pytest.raises(verifier.IncompleteEvidence, match="release_lock_mismatch"):
        verifier.verify_release_binding(evidence, private_lock=lock, approved_sha256="a" * 64)
    lock.chmod(0o644)
    with pytest.raises(verifier.IncompleteEvidence, match="private_operator_file"):
        verifier.verify_release_binding(evidence, private_lock=lock, approved_sha256=digest)


@pytest.mark.parametrize("mutation,reason", [
    (lambda evidence: evidence.pop("host_histories"), "host_histories_missing"),
    (lambda evidence: evidence["host_histories"][0]["scope"].update(run_id="other"), "host_history_scope_wrong"),
    (lambda evidence: evidence["host_histories"][0]["snapshots"][0].update(aws_request_id=""), "host_provider_receipt_missing"),
    (lambda evidence: evidence["host_histories"][0]["records"].pop(0), "host_stream_not_contiguous"),
    (lambda evidence: evidence["host_histories"][0]["records"][1]["data"].update(job_id=999), "one_job_per_host_required"),
    (lambda evidence: evidence["host_histories"][0]["records"][2]["data"]["host_enforcement"].update(status="unavailable"), "first_job_guard_unverified"),
    (lambda evidence: evidence["host_histories"][0]["records"][2]["data"]["host_enforcement"].update(denial_installed_at_unix_ns=0), "early_denial_host_time_wrong"),
    (lambda evidence: evidence["daemon_restart"].update(instance_id="i-0"), "daemon_restart_requires_fresh_host"),
    (lambda evidence: evidence["host_histories"][3]["records"][1]["data"].update(executor_ready=True), "failed_bootstrap_host_unproven"),
    (lambda evidence: evidence["bootstrap_failure"].update(instance_id="i-0"), "failed_bootstrap_host_missing"),
])
def test_host_lifecycle_counterexamples_fail_closed(sample, mutation, reason):
    fixtures, evidence = sample
    mutation(evidence)
    # These cases describe the provider actually observing an invalid lifecycle.
    # Receipt tampering and missing coverage are tested independently below.
    for history in evidence.get("host_histories", []):
        history["snapshots"][0]["record_sha256s"] = record_hashes(history["records"])
    with pytest.raises(verifier.IncompleteEvidence, match=reason):
        verifier.verify(fixtures, evidence, now=NOW)


def test_multiple_provider_polls_cover_only_the_records_they_observed(sample):
    fixtures, evidence = sample
    history = evidence["host_histories"][4]
    earlier = copy.deepcopy(history["snapshots"][0])
    start = datetime.fromisoformat(evidence["started_at"])
    earlier.update(aws_request_id="synthetic-earlier-poll",
                   provider_timestamp=(start + timedelta(seconds=2)).isoformat(),
                   observed_at=(start + timedelta(seconds=3)).isoformat(),
                   record_sha256s=record_hashes(history["records"][:1]))
    # The first snapshot predates the restart and has only second precision.
    # The later console response can have rolled off already retained records.
    history["snapshots"][0]["record_sha256s"] = record_hashes(history["records"][1:])
    history["snapshots"].insert(0, earlier)
    assert verifier.verify(fixtures, evidence, now=NOW)["verdict"] == "pass"


@pytest.mark.parametrize("mutation,reason", [
    (lambda rows: rows[1]["data"]["service_invocations"].update({"docker.service": "1" * 32}),
     "daemon_restart_not_observed"),
    (lambda rows: rows[1]["data"]["service_invocations"].update({"adp-job-firewall.service": "2" * 32}),
     "daemon_restart_not_observed"),
    (lambda rows: rows[0]["data"].pop("service_invocations"), "restart_service_identity_missing"),
    (lambda rows: rows[0]["data"]["host_enforcement"].update(status="unavailable"), "restart_denial_not_observed"),
    (lambda rows: rows[0]["data"].update(executor_ready=True), "restart_host_already_used"),
    (lambda rows: rows[1]["data"]["host_enforcement"].update(executor_ready_at_unix_ns=1),
     "restart_denial_not_observed"),
    (lambda rows: rows[0].update(boot_id="00000000-0000-0000-0000-000000000099"), "daemon_restart_not_observed"),
    (lambda rows: rows[0].update(kind="container_event", data={"job_id": 999}), "host_stream_not_contiguous"),
])
def test_restart_cannot_be_faked_by_restarting_only_the_observer(sample, mutation, reason):
    fixtures, evidence = sample
    history = evidence["host_histories"][4]
    mutation(history["records"])
    history["snapshots"][0]["record_sha256s"] = record_hashes(history["records"])
    with pytest.raises(verifier.IncompleteEvidence, match=reason):
        verifier.verify(fixtures, evidence, now=NOW)


def test_restart_refuses_an_earlier_job_even_when_host_claims_no_reuse(sample):
    fixtures, evidence = sample
    history = evidence["host_histories"][4]
    earlier = copy.deepcopy(history["records"][2])
    earlier.update(stream_id=history["records"][0]["stream_id"], sequence=2,
                   observed_at_unix_ns=history["records"][0]["observed_at_unix_ns"] + 1)
    earlier["data"]["job_id"] = 998
    history["records"].insert(1, earlier)
    history["snapshots"][0]["record_sha256s"] = record_hashes(history["records"])
    with pytest.raises(verifier.IncompleteEvidence, match="one_job_per_host_required"):
        verifier.verify(fixtures, evidence, now=NOW)


def test_failed_bootstrap_cannot_borrow_the_restart_host(sample):
    fixtures, evidence = sample
    evidence["bootstrap_failure"]["instance_id"] = evidence["daemon_restart"]["instance_id"]
    with pytest.raises(verifier.IncompleteEvidence, match="failed_bootstrap_host_missing"):
        verifier.verify(fixtures, evidence, now=NOW)


@pytest.mark.parametrize("mutation,reason", [
    (lambda history: history["snapshots"][0].pop("record_sha256s"), "host_provider_receipt_missing"),
    (lambda history: history["snapshots"][0]["record_sha256s"].pop(), "host_provider_record_uncovered"),
    (lambda history: history["records"][2]["data"].update(unobserved=True), "host_provider_receipt_missing"),
    (lambda history: history["snapshots"][0].update(provider_timestamp="2026-01-01T00:20:01+00:00"),
     "host_provider_receipt_missing"),
])
def test_missing_changed_and_future_host_records_are_not_covered(sample, mutation, reason):
    fixtures, evidence = sample
    mutation(evidence["host_histories"][0])
    with pytest.raises(verifier.IncompleteEvidence, match=reason):
        verifier.verify(fixtures, evidence, now=NOW)


@pytest.mark.parametrize("submicroseconds", [123, 999])
def test_real_host_nanoseconds_match_iso_microsecond_precision(sample, submicroseconds):
    fixtures, evidence = sample
    history = evidence["host_histories"][0]
    for record in history["records"]:
        if record["kind"] == "container_observation":
            record["data"]["host_enforcement"]["denial_installed_at_unix_ns"] += submicroseconds
    history["snapshots"][0]["record_sha256s"] = record_hashes(history["records"])
    assert verifier.verify(fixtures, evidence, now=NOW)["verdict"] == "pass"
    history["records"][2]["data"]["host_enforcement"]["denial_installed_at_unix_ns"] += 1000
    history["snapshots"][0]["record_sha256s"] = record_hashes(history["records"])
    with pytest.raises(verifier.IncompleteEvidence, match="early_denial_host_time_wrong"):
        verifier.verify(fixtures, evidence, now=NOW)


def test_iso_timestamp_conversion_does_not_round_through_float():
    assert verifier.timestamp_ns("2026-01-01T00:20:01.123456+00:00") == 1767226801123456000


def test_host_order_cannot_be_hidden_inside_one_iso_microsecond(sample):
    fixtures, evidence = sample
    history = evidence["host_histories"][0]
    guard = history["records"][2]["data"]["host_enforcement"]
    evidence["cold_starts"][0]["denial_installed_at"] = evidence["cold_starts"][0]["executor_ready_at"]
    guard["denial_installed_at_unix_ns"] = guard["executor_ready_at_unix_ns"] + 1
    history["snapshots"][0]["record_sha256s"] = record_hashes(history["records"])
    with pytest.raises(verifier.IncompleteEvidence, match="early_denial_host_time_wrong"):
        verifier.verify(fixtures, evidence, now=NOW)


@pytest.mark.parametrize('fixture_name', ['sample', 'ipv4_only_sample'])
def test_lifecycle_job_and_failed_instance_are_checked_from_provider(request, fixture_name):
    fixtures, evidence = request.getfixturevalue(fixture_name)
    start, end = evidence["started_at"], evidence["completed_at"]
    attempts = evidence["cold_starts"] + [evidence["daemon_restart"]]
    jobs = {row["job_id"]: {"id": row["job_id"], "name": "network-isolation", "status": "success",
            "commit": {"id": fixtures["source_revision"]}, "runner": {"id": fixtures["runner_a"]},
            "pipeline": {"id": row["pipeline_id"]}, "created_at": start, "finished_at": end}
            for row in attempts}
    artifacts = {row["job_id"]: {"run_id": fixtures["run_id"], "probes": copy.deepcopy(evidence["probes"])}
                 for row in attempts}
    failure = evidence["bootstrap_failure"]
    jobs[failure["job_id"]] = {"id": failure["job_id"], "status": "canceled", "runner": None,
            "commit": {"id": fixtures["source_revision"]}, "created_at": start, "finished_at": end}
    def gitlab(path):
        if path.endswith("/variables"):
            return [{"key": "ADP_QUALIFICATION_RUN_ID", "value": fixtures["run_id"]},
                    {"key": "ADP_QUALIFICATION_TARGETS_FILE", "variable_type": "file",
                     "value": json.dumps({"run_id": fixtures["run_id"], "targets": fixtures["network_targets"]})}]
        if path.endswith("/artifacts/network-result.json"):
            return artifacts[int(path.split("/")[3])]
        if "/pipelines/" in path:
            return {"source": "api", "sha": fixtures["source_revision"],
                    "ref": "qualification-" + fixtures["run_id"]}
        return jobs[int(path.rsplit("/", 1)[1])]

    aws = Mock()
    aws.client.return_value.describe_instances.return_value = {"Reservations": [{"Instances": [
        {"InstanceId": failure["instance_id"], "State": {"Name": "terminated"}}]}]}
    verifier.live_lifecycle_jobs(fixtures, evidence, aws, gitlab)
    jobs[attempts[1]["job_id"]]["runner"] = {"id": 999}
    with pytest.raises(verifier.IncompleteEvidence, match="lifecycle_provider_job_wrong"):
        verifier.live_lifecycle_jobs(fixtures, evidence, aws, gitlab)
    jobs[attempts[1]["job_id"]]["runner"] = {"id": fixtures["runner_a"]}
    artifacts[attempts[1]["job_id"]]["probes"][0]["result"] = "allowed"
    with pytest.raises(verifier.IncompleteEvidence, match="first_instruction_denial_wrong"):
        verifier.live_lifecycle_jobs(fixtures, evidence, aws, gitlab)
    artifacts[attempts[1]["job_id"]]["probes"][0]["result"] = "blocked"
    for attempt in attempts:
        probes = artifacts[attempt["job_id"]]["probes"]
        metadata = next(row for row in probes if row["name"] == "metadata_ipv4")
        metadata["connection_attempted"] = False
        with pytest.raises(verifier.IncompleteEvidence, match="first_instruction_denial_wrong"):
            verifier.live_lifecycle_jobs(fixtures, evidence, aws, gitlab)
        metadata["connection_attempted"] = True
        positive = next(row for row in probes if row["name"] == "gitlab")
        positive["transport_connected"] = False
        with pytest.raises(verifier.IncompleteEvidence, match="first_instruction_denial_wrong"):
            verifier.live_lifecycle_jobs(fixtures, evidence, aws, gitlab)
        positive["transport_connected"] = True
    jobs[failure["job_id"]]["runner"] = {"id": fixtures["runner_a"]}
    with pytest.raises(verifier.IncompleteEvidence, match="failed_bootstrap_provider_job_wrong"):
        verifier.live_lifecycle_jobs(fixtures, evidence, aws, gitlab)
    jobs[failure["job_id"]]["runner"] = None
    aws.client.return_value.describe_instances.return_value["Reservations"][0]["Instances"][0]["State"]["Name"] = "running"
    with pytest.raises(verifier.IncompleteEvidence, match="failed_bootstrap_provider_instance_wrong"):
        verifier.live_lifecycle_jobs(fixtures, evidence, aws, gitlab)


@pytest.mark.parametrize("path,value,reason", [
    (("gitlab", "a", "group_id"), 20, "wrong_group"),
    (("gitlab", "a", "runner_id"), 99, "wrong_runner"),
    (("hosts", 0, "vpc_id"), "vpc-b", "wrong_vpc"),
    (("hosts", 0, "public_ip"), "192.0.2.1", "not_private"),
    (("hosts", 0, "instance_reused"), True, "reuse_violation"),
    (("cleanup", 0, "ec2_nonterminated_instances"), ["i-orphan"], "orphan_instance"),
    (("cleanup", 0, "asg_instances"), [{"LifecycleState": "Terminating"}], "orphan_instance"),
    (("cleanup", 0, "observed_asg_names"), [], "incomplete_asg_inventory"),
    (("cleanup", 0, "manager_ready"), 0, "manager_not_healthy"),
    (("cleanup", 0, "elapsed_seconds"), 901, "deadline_exceeded"),
    (("cold_starts", 0, "denial_installed_at"), "2026-01-01T00:20:04+00:00", "early_denial_evidence_missing"),
    (("cold_starts", 0, "metadata_ipv6_denied"), False, "early_denial_not_proven"),
    (("daemon_restart", "first_instruction_denied"), False, "early_denial_not_proven"),
    (("bootstrap_failure", "accepted_jobs"), 1, "failed_bootstrap"),
    (("executor_negatives", 0, "invalid_yaml"), True, "container_not_confined"),
    (("executor_negatives", 0, "uid"), 0, "container_not_confined"),
    (("executor_negatives", 0, "host_mounts"), ["/var/run/docker.sock"], "container_not_confined"),
    (("probes", 0, "result"), "allowed", "destination_reachable"),
    (("independent_target_controls", 0, "reachable"), False, "destination_not_listening"),
    (("independent_target_controls", 0, "address"), "192.0.2.88", "negative_control_target_wrong"),
    (("secret_scan", "canary_hits"), 1, "secret_safety"),
    (("instance_plan_exit_codes", "after_teardown"), 2, "independence"),
    (("token_revoked",), False, "revocation"),
    (("identity_checks", 0, "transport_succeeded"), False, "identity_boundary"),
    (("identity_checks", 0, "verified_caller_role_arn"), "arn:aws:iam::123456789012:role/admin", "identity_boundary"),
    (("cache_roundtrip", "downloaded_sha256"), "c" * 64, "cache_roundtrip"),
    (("token_revocation", "baseline_http_status"), 401, "token_rejection"),
])
def test_counterexample_must_fail(sample, path, value, reason):
    scope, evidence = sample
    target = evidence
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(verifier.IncompleteEvidence, match=reason):
        verifier.verify(scope, evidence, now=NOW)


@pytest.mark.parametrize("field,reason", [("probes", "missing_negative_cases"),
                                           ("executor_negatives", "executor_negative_cases_missing"),
                                           ("cold_starts", "three_fresh_starts_required")])
def test_missing_required_case_never_passes(sample, field, reason):
    scope, evidence = sample
    evidence[field].pop()
    with pytest.raises(verifier.IncompleteEvidence, match=reason):
        verifier.verify(scope, evidence, now=NOW)


def test_pending_five_minutes_requires_continuous_observation(sample):
    scope, evidence = sample
    rows = evidence["gitlab"]["b"]["pending_observations"]
    evidence["gitlab"]["b"]["pending_observations"] = [rows[0], rows[-1]]
    with pytest.raises(verifier.IncompleteEvidence, match="interval_unproven"):
        verifier.verify(scope, evidence, now=NOW)


@pytest.mark.parametrize("change,reason", [
    (lambda evidence: evidence["gitlab"]["a"].update(runner_trace_line="no runner execution"), "product_runner_trace_missing"),
    (lambda evidence: evidence["gitlab"]["b"]["pending_observations"][0].update(job_id=999), "group_b_pending_scope_wrong"),
    (lambda evidence: evidence["gitlab"]["b"]["pending_observations"][0].update(tag_list=["adp-group-20"]),
     "group_b_pending_scope_wrong"),
    (lambda evidence: evidence["cleanup"][0].update(job_id=0), "cleanup_job_scope_wrong"),
    (lambda evidence: evidence["cleanup"][0].update(observed_at=evidence["completed_at"]), "cleanup_deadline_exceeded"),
    (lambda evidence: evidence["cleanup"][0]["aws_request_ids"].update(ec2=[]), "cleanup_provider_receipts_missing"),
    (lambda evidence: evidence["cleanup"][0].update(instance_id="i-other"), "cleanup_job_scope_wrong"),
    (lambda evidence: next(row for row in evidence["cleanup"] if row["name"] == "manager_loss")
     .pop("manager_loss_receipt"), "manager_loss_receipt_missing"),
])
def test_ec02_receipts_reject_missing_or_mismatched_observations(sample, change, reason):
    fixtures, evidence = sample
    change(evidence)
    with pytest.raises(verifier.IncompleteEvidence, match=reason):
        verifier.verify(fixtures, evidence, now=NOW)


@pytest.mark.parametrize("change,reason", [
    (None, None), ("trace", "live_product_runner_trace_missing"),
    ("group_b_runner", "live_product_or_pending_job_wrong"),
    ("wrong_pipeline", "live_product_pipeline_wrong"),
    ("wrong_pipeline_status", "live_product_pipeline_wrong"),
    ("failed_cleanup", "live_cleanup_job_wrong"),
])
def test_ec02_live_readback_checks_actual_jobs_and_runner_trace(sample, change, reason):
    fixtures, evidence = sample
    start = evidence["started_at"]
    jobs = {}
    for label, status in (("a", "success"), ("b", "canceled")):
        row = evidence["gitlab"][label]
        jobs[row["job_id"]] = {"id": row["job_id"], "status": status,
                               "commit": {"id": fixtures["source_revision"]},
                               "pipeline": {"id": row["pipeline_id"]},
                               "tag_list": ["adp-group-10"],
                               "runner": {"id": 42} if label == "a" else None,
                               "created_at": start,
                               "finished_at": row.get("job_finished_at", evidence["completed_at"])}
    for row in evidence["cleanup"]:
        if row["job_id"] not in jobs:
            jobs[row["job_id"]] = {"id": row["job_id"], "status": row["job_status"],
                                   "commit": {"id": fixtures["source_revision"]},
                                   "pipeline": {"id": row["pipeline_id"]},
                                   "runner": {"id": 42}, "finished_at": row["job_finished_at"]}
    jobs[111]["finished_at"] = next(row["job_finished_at"] for row in evidence["cleanup"] if row["name"] == "legal")
    pipeline = {"source": "api", "sha": fixtures["source_revision"],
                "ref": "qualification-" + fixtures["run_id"]}
    if change == "group_b_runner":
        jobs[222]["runner"] = {"id": 42}
    if change == "wrong_pipeline":
        pipeline["sha"] = "b" * 40
    if change == "failed_cleanup":
        jobs[901]["status"] = "success"

    def gitlab(path):
        if path.endswith("/trace"):
            return "missing runner line" if change == "trace" else evidence["gitlab"]["a"]["runner_trace_line"]
        if path.endswith("/variables"):
            return [{"key": "ADP_QUALIFICATION_RUN_ID", "value": fixtures["run_id"]}]
        if "/pipelines/" in path:
            status = "canceled" if "/pipelines/350" in path else "success"
            return {**pipeline, "status": "failed" if change == "wrong_pipeline_status" else status}
        return jobs[int(path.rsplit("/", 1)[1])]

    if reason:
        with pytest.raises(verifier.IncompleteEvidence, match=reason):
            verifier.live_ec02_readback(fixtures, evidence, gitlab)
    else:
        verifier.live_ec02_readback(fixtures, evidence, gitlab)


def test_live_oracle_defeats_forged_successful_job_report(sample):
    scope, evidence = sample
    assert verifier.verify(scope, evidence, now=NOW)["verdict"] == "pass"
    aws = Mock()
    aws.client.return_value.get_caller_identity.return_value = {"Account": scope["account_id"]}
    # The job report claims A's group; the provider says B owns this project.
    gitlab = Mock(return_value={"namespace": {"id": 20, "kind": "group"}})
    with pytest.raises(verifier.IncompleteEvidence, match="live_wrong_group"):
        verifier.live_readback(scope, evidence, aws=aws, gitlab=gitlab, manager=Mock())


@pytest.mark.parametrize("instance_profile", [None, {"Arn": "arn:aws:iam::123456789012:instance-profile/unexpected"}])
def test_live_oracle_finds_detached_orphan_or_instance_credentials(sample, instance_profile):
    scope, evidence = sample
    clients = {name: Mock() for name in ("sts", "ec2", "autoscaling")}
    aws = Mock()
    aws.client.side_effect = clients.__getitem__
    clients["sts"].get_caller_identity.return_value = {"Account": scope["account_id"]}
    clients["ec2"].describe_instances.return_value = {"Reservations": [{"Instances": [
        {"InstanceId": row["instance_id"], "VpcId": "vpc-a", "SubnetId": "subnet-a", "ImageId": "ami-a",
         "SecurityGroups": [{"GroupId": "sg-a"}]} for row in evidence["hosts"]
    ]}]}
    if instance_profile:
        clients["ec2"].describe_instances.return_value["Reservations"][0]["Instances"][0]["IamInstanceProfile"] = instance_profile
    clients["autoscaling"].describe_auto_scaling_groups.return_value = {"AutoScalingGroups": []}
    orphan_tags = {"Component": "gitlab-job", "Environment": "dev", "GroupId": "10"}

    def pages(*, Filters):
        # AWS's reserved ASG tag can be absent after detachment. Common owned
        # tags still identify the instance after its ASG has disappeared.
        matched = all((orphan_tags.get(item["Name"][4:]) if item["Name"].startswith("tag:") else "running")
                      in item["Values"] for item in Filters)
        return [{"Reservations": [{"Instances": [{"InstanceId": "i-detached"}]}] if matched else []}]

    clients["ec2"].get_paginator.return_value.paginate.side_effect = pages

    def gitlab(path):
        if path.startswith("groups/"):
            group_id = int(path.split("/")[1])
            return {"id": group_id, "parent_id": 10 if group_id == 11 else 20}
        if path.startswith("runners/"):
            return None
        if path.endswith("/variables"):
            return network_variables(scope)
        if "/pipelines/" in path:
            return pipeline(scope)
        if path.endswith("/trace"):
            return refusal_trace(path)
        project = int(path.split("/")[1])
        if "/jobs/" not in path:
            return {"id": project, "namespace": {"id": 11 if project == 101 else 21, "kind": "group"}}
        if path.endswith("/artifacts/network-result.json"):
            return {"run_id": scope["run_id"], "probes": evidence["probes"]}
        if path.endswith("/artifacts/executor-result.json"):
            return executor_artifact(evidence, path)
        job_id = int(path.rsplit("/", 1)[1])
        if job_id == evidence["network_job_id"]:
            return network_job(scope, evidence)
        if job_id >= 500:
            return executor_job(scope, evidence, evidence["executor_negatives"][job_id - 500])
        return {"commit": {"id": scope["source_revision"]},
                "status": "success" if project == 101 else "canceled",
                "runner": {"id": 42} if project == 101 else None}

    reason = "live_job_instance_profile_forbidden" if instance_profile else "live_orphan_instance_remaining"
    with pytest.raises(verifier.IncompleteEvidence, match=reason):
        verifier.live_readback(scope, evidence, aws=aws, gitlab=gitlab, manager=lambda: None)


@pytest.mark.parametrize("label,namespace,parents", [
    ("a", 10, {}), ("a", 11, {11: 10}), ("a", 12, {12: 11, 11: 10}),
    ("b", 20, {}), ("b", 21, {21: 20}),
])
def test_project_scope_accepts_root_or_descendant_namespace(sample, label, namespace, parents):
    scope, _ = sample
    scope[f"project_namespace_{label}"] = namespace
    project = {"id": scope[f"project_{label}"], "namespace": {"id": namespace, "kind": "group"}}
    gitlab = Mock(side_effect=lambda path: {"id": int(path.split("/")[1]),
                                          "parent_id": parents[int(path.split("/")[1])]})
    verifier.project_scope(scope, label, project, gitlab, "scope_wrong")
    assert gitlab.call_count == len(parents)


@pytest.mark.parametrize("change", ["other_root", "unrelated", "cycle", "wrong_namespace", "wrong_project", "missing_binding"])
def test_project_scope_refuses_unrelated_or_unbound_namespace(sample, change):
    scope, _ = sample
    project = {"id": 101, "namespace": {"id": 11, "kind": "group"}}
    parent = {"other_root": 20, "unrelated": None, "cycle": 11}.get(change, 10)
    if change == "wrong_namespace":
        project["namespace"]["id"] = 12
    if change == "wrong_project":
        project["id"] = 999
    if change == "missing_binding":
        del scope["project_namespace_a"]
    gitlab = Mock(return_value={"id": 11, "parent_id": parent})
    with pytest.raises(verifier.IncompleteEvidence, match="scope_wrong"):
        verifier.project_scope(scope, "a", project, gitlab, "scope_wrong")


def executor_job(scope, evidence, row):
    return {"id": row["gitlab_job_id"], "name": f"executor-{row['name']}",
            "commit": {"id": scope["source_revision"]}, "runner": {"id": scope["runner_a"]},
            "status": "failed" if row["outcome"] == "refused_before_script" else "success", "pipeline": {"id": 300},
            "failure_reason": "runner_system_failure" if row["outcome"] == "refused_before_script" else None,
            "created_at": evidence["started_at"], "finished_at": evidence["completed_at"]}


def pipeline(scope):
    return {"source": "api", "sha": scope["source_revision"], "ref": "qualification-" + scope["run_id"]}


def network_job(scope, evidence):
    return {"id": evidence["network_job_id"], "name": "network-isolation",
            "commit": {"id": scope["source_revision"]}, "runner": {"id": scope["runner_a"]},
            "status": "success", "pipeline": {"id": 300},
            "created_at": evidence["started_at"], "finished_at": evidence["completed_at"]}


def executor_variables(scope):
    return [{"key": "ADP_QUALIFICATION_RUN_ID", "value": scope["run_id"]},
            {"key": "ADP_QUALIFICATION_HELPER_IMAGE", "value": "registry.example.test/unlisted@sha256:abc"}]


def network_variables(scope):
    return executor_variables(scope) + [
            {"key": "ADP_QUALIFICATION_TARGETS_FILE", "variable_type": "file",
             "value": json.dumps({"run_id": scope["run_id"], "targets": scope["network_targets"]})}]


def refusal_trace(path):
    name = json.loads((ROOT / "executor-negative-cases.json").read_text())["cases"][int(path.split("/")[3]) - 500]["name"]
    return {
        "arbitrary_image": 'The "registry.example.test/unlisted@sha256:abc" image is not present on list of allowed images:',
        "arbitrary_service": "too many services requested: 1, only 0 allowed",
        "root_user": 'user "0" is not an allowed user: [10001:10001]',
        "debug_trace": "Executor attempt recorded; independent host and provider verification required",
    }[name]


def executor_artifact(evidence, path):
    job_id = int(path.split("/")[3])
    return next(row["probe"] for row in evidence["executor_negatives"] if row["gitlab_job_id"] == job_id)


def test_all_executor_attempts_read_back_from_the_owned_pipeline(sample):
    scope, evidence = sample
    jobs = {row["gitlab_job_id"]: executor_job(scope, evidence, row) for row in evidence["executor_negatives"]}
    def read(path):
        if path.endswith("/variables"):
            return executor_variables(scope)
        if "/pipelines/" in path:
            return pipeline(scope)
        if path.endswith("/trace"):
            return refusal_trace(path)
        if path.endswith("/artifacts/executor-result.json"):
            return executor_artifact(evidence, path)
        assert path.startswith(f"projects/{scope['project_a']}/jobs/")
        return jobs[int(path.rsplit("/", 1)[1])]
    api = Mock(side_effect=read)
    verifier.live_executor_jobs(scope, evidence, api)
    assert api.call_count == len(jobs) + 6 + sum(row["outcome"] == "constrained" for row in evidence["executor_negatives"])


@pytest.mark.parametrize("change,reason", [
    (lambda row: row.pop("probe"), "executor_probe_not_executed"),
    (lambda row: row["probe"].update(probe_result="unexpected_access"), "executor_probe_not_executed"),
    (lambda row: row["probe"]["observations"][0].update(errno=errno.EIO), "executor_probe_not_executed"),
    (lambda row: row["probe"]["process"].update(effective_capabilities=1), "executor_probe_not_executed"),
    (lambda row: row["probe"].update(trusted_host_verified=True), "executor_probe_not_executed"),
    (lambda row: row.update(instance_id="i-0"), "executor_host_scope_wrong"),
    (lambda row: row.update(instance_id=None), "executor_host_scope_wrong"),
    (lambda row: row.update(pipeline_id=999), "executor_host_job_unverified"),
])
def test_executed_negative_probe_and_distinct_host_required(sample, change, reason):
    scope, evidence = sample
    change(evidence["executor_negatives"][0])
    with pytest.raises(verifier.IncompleteEvidence, match=reason):
        verifier.verify(scope, evidence, now=NOW)


@pytest.mark.parametrize("change,reason", [
    (lambda record: record.update(privileged=True), "executor_host_confinement_unverified"),
    (lambda record: record["kernel"]["namespaces"]["net"].update(same_as_host=True), "executor_host_confinement_unverified"),
    (lambda record: record["kernel"].update(uids=[0] * 4), "executor_host_confinement_unverified"),
    (lambda record: record.update(mounts=[{"Type": "bind", "Destination": "/var/run/docker.sock"}]),
     "executor_host_confinement_unverified"),
    (lambda record: record["host_enforcement"].update(status="unavailable"), "executor_host_confinement_unverified"),
    (lambda record: record.update(pipeline_id=999), "executor_host_job_unverified"),
])
def test_provider_retained_executor_host_inspection_rejects_unsafe_claims(sample, change, reason):
    scope, evidence = sample
    history = next(history for history in evidence["host_histories"] if history["scope"]["instance"]["instance_id"] ==
                   evidence["executor_negatives"][0]["instance_id"])
    change(history["records"][2]["data"])
    history["snapshots"][0]["record_sha256s"] = record_hashes(history["records"])
    with pytest.raises(verifier.IncompleteEvidence, match=reason):
        verifier.verify(scope, evidence, now=NOW)


def test_changed_host_observation_without_provider_digest_is_rejected(sample):
    scope, evidence = sample
    evidence["host_histories"][5]["records"][2]["data"]["privileged"] = True
    with pytest.raises(verifier.IncompleteEvidence, match="host_provider_receipt_missing"):
        verifier.verify(scope, evidence, now=NOW)


def test_negative_jobs_cannot_share_one_temporary_instance(sample):
    scope, evidence = sample
    constrained = [row for row in evidence["executor_negatives"] if row["outcome"] == "constrained"]
    constrained[1]["instance_id"] = constrained[0]["instance_id"]
    with pytest.raises(verifier.IncompleteEvidence, match="executor_host_scope_wrong"):
        verifier.verify(scope, evidence, now=NOW)


@pytest.mark.parametrize("case,change", [
    ("metadata_ipv4", {"connection_attempted": False}),
    ("metadata_ipv6", {"error_kind": "OSError"}),
    ("gateway", {"error_kind": "OSError"}),
    ("other_tenant", {"result": "skipped"}),
])
def test_network_receipt_requires_executed_packet_denial(sample, case, change):
    scope, evidence = sample
    next(row for row in evidence["probes"] if row["name"] == case).update(change)
    with pytest.raises(verifier.IncompleteEvidence, match="denied_destination_reachable"):
        verifier.verify(scope, evidence, now=NOW)


@pytest.mark.parametrize("mutation,reason", [
    ("missing", "live_executor_probe_artifact_wrong"),
    ("different", "live_executor_probe_artifact_wrong"),
    ("wrong-pipeline", "live_executor_host_pipeline_wrong"),
    ("trace-enabled", "live_debug_trace_not_disabled"),
])
def test_executor_provider_artifact_and_disabled_trace_are_required(sample, mutation, reason):
    scope, evidence = sample
    jobs = {row["gitlab_job_id"]: executor_job(scope, evidence, row) for row in evidence["executor_negatives"]}
    target = next(row for row in evidence["executor_negatives"] if row["name"] == "debug_trace")
    if mutation == "wrong-pipeline":
        target["pipeline_id"] += 1

    def read(path):
        if path.endswith("/variables"):
            return executor_variables(scope)
        if "/pipelines/" in path:
            return pipeline(scope)
        if path.endswith("/artifacts/executor-result.json"):
            if mutation in {"missing", "different"} and path.split("/")[3] == str(target["gitlab_job_id"]):
                return None if mutation == "missing" else {"name": "other-case"}
            return executor_artifact(evidence, path)
        if path.endswith("/trace"):
            return "+ python3 executor_probe.py\n" if mutation == "trace-enabled" and path.split("/")[3] == str(target["gitlab_job_id"]) else refusal_trace(path)
        return jobs[int(path.rsplit("/", 1)[1])]

    with pytest.raises(verifier.IncompleteEvidence, match=reason):
        verifier.live_executor_jobs(scope, evidence, read)

def test_refusal_evidence_is_independent_of_job_order(sample):
    scope, evidence = sample
    evidence["executor_negatives"].sort(key=lambda row: row["name"] != "arbitrary_image")
    jobs = {row["gitlab_job_id"]: executor_job(scope, evidence, row) for row in evidence["executor_negatives"]}
    def read(path):
        if path.endswith("/variables"):
            return executor_variables(scope)
        if "/pipelines/" in path:
            return pipeline(scope)
        if path.endswith("/trace"):
            return refusal_trace(path)
        if path.endswith("/artifacts/executor-result.json"):
            return executor_artifact(evidence, path)
        return jobs[int(path.rsplit("/", 1)[1])]
    verifier.live_executor_jobs(scope, evidence, read)


@pytest.mark.parametrize("case", ["arbitrary_image", "arbitrary_service", "root_user"])
@pytest.mark.parametrize("bad_trace", [
    "image not allowed", "service not allowed", 'user "0" not allowed',
    "ERROR: Job failed: image pull failure", "ERROR: job failed: TLS certificate invalid",
    "ERROR: Job failed: invalid YAML", "ERROR: connection refused",
    "too many services requested: 2, only 0 allowed",
])
def test_only_pinned_runner_refusal_for_its_case_is_accepted(sample, case, bad_trace):
    scope, evidence = sample
    jobs = {row["gitlab_job_id"]: executor_job(scope, evidence, row) for row in evidence["executor_negatives"]}
    rejected = next(row for row in evidence["executor_negatives"] if row["name"] == case)
    def read(path):
        if path.endswith("/variables"):
            return executor_variables(scope)
        if "/pipelines/" in path:
            return pipeline(scope)
        if path.endswith("/trace"):
            return bad_trace if path.split("/")[3] == str(rejected["gitlab_job_id"]) else refusal_trace(path)
        if path.endswith("/artifacts/executor-result.json"):
            return executor_artifact(evidence, path)
        return jobs[int(path.rsplit("/", 1)[1])]
    with pytest.raises(verifier.IncompleteEvidence, match="live_executor_refusal_trace_wrong"):
        verifier.live_executor_jobs(scope, evidence, read)


@pytest.mark.parametrize("image", ["substituted:latest", "", None])
def test_refusal_image_must_match_the_owned_pipeline_image(sample, image):
    scope, evidence = sample
    jobs = {row["gitlab_job_id"]: executor_job(scope, evidence, row) for row in evidence["executor_negatives"]}
    def read(path):
        if path.endswith("/variables"):
            variables = executor_variables(scope)
            variables[1]["value"] = image
            return variables
        if "/pipelines/" in path:
            return pipeline(scope)
        if path.endswith("/trace"):
            return refusal_trace(path)
        if path.endswith("/artifacts/executor-result.json"):
            return executor_artifact(evidence, path)
        return jobs[int(path.rsplit("/", 1)[1])]
    with pytest.raises(verifier.IncompleteEvidence, match="live_executor_(image_binding|refusal_trace_wrong)"):
        verifier.live_executor_jobs(scope, evidence, read)


@pytest.mark.parametrize("case", ["arbitrary_image", "arbitrary_service", "root_user"])
def test_each_refusal_needs_provider_system_failure(sample, case):
    scope, evidence = sample
    jobs = {row["gitlab_job_id"]: executor_job(scope, evidence, row) for row in evidence["executor_negatives"]}
    rejected = next(row for row in evidence["executor_negatives"] if row["name"] == case)
    jobs[rejected["gitlab_job_id"]]["failure_reason"] = "script_failure"
    def read(path):
        if path.endswith("/variables"):
            return executor_variables(scope)
        if "/pipelines/" in path:
            return pipeline(scope)
        if path.endswith("/trace"):
            return refusal_trace(path)
        if path.endswith("/artifacts/executor-result.json"):
            return executor_artifact(evidence, path)
        return jobs[int(path.rsplit("/", 1)[1])]
    with pytest.raises(verifier.IncompleteEvidence, match="live_executor_refusal_trace_wrong"):
        verifier.live_executor_jobs(scope, evidence, read)


@pytest.mark.parametrize("field,value", [("source", "push"), ("sha", "b" * 40), ("ref", "other-branch")])
def test_executor_jobs_from_an_unreviewed_pipeline_are_rejected(sample, field, value):
    scope, evidence = sample
    jobs = {row["gitlab_job_id"]: executor_job(scope, evidence, row) for row in evidence["executor_negatives"]}
    metadata = pipeline(scope)
    metadata[field] = value
    def read(path):
        if path.endswith("/variables"):
            return executor_variables(scope)
        if "/pipelines/" in path:
            return metadata
        if path.endswith("/trace"):
            return refusal_trace(path)
        if path.endswith("/artifacts/executor-result.json"):
            return executor_artifact(evidence, path)
        return jobs[int(path.rsplit("/", 1)[1])]
    with pytest.raises(verifier.IncompleteEvidence, match="live_executor_pipeline_scope_wrong"):
        verifier.live_executor_jobs(scope, evidence, read)


@pytest.mark.parametrize("mutation", ["wrong-reason", "wrong-trace", "script-started", "missing-trace", "other-pipeline"])
def test_independent_executor_refusal_or_pipeline_mismatch_fails(sample, mutation):
    scope, evidence = sample
    jobs = {row["gitlab_job_id"]: executor_job(scope, evidence, row) for row in evidence["executor_negatives"]}
    refused = next(row for row in evidence["executor_negatives"] if row["name"] == "arbitrary_image")
    if mutation == "wrong-reason":
        jobs[refused["gitlab_job_id"]]["failure_reason"] = "script_failure"
    if mutation == "other-pipeline":
        jobs[refused["gitlab_job_id"]]["pipeline"]["id"] = 301
    def read(path):
        if path.endswith("/variables"):
            return executor_variables(scope)
        if "/pipelines/" in path:
            return pipeline(scope)
        if path.endswith("/trace"):
            if path.split("/")[3] == str(refused["gitlab_job_id"]):
                return {"wrong-trace": "connection refused", "script-started":
                        refusal_trace(path) + '; Executing "step_script" stage', "missing-trace": ""}.get(
                            mutation, refusal_trace(path))
            return refusal_trace(path)
        if path.endswith("/artifacts/executor-result.json"):
            return executor_artifact(evidence, path)
        return jobs[int(path.rsplit("/", 1)[1])]
    with pytest.raises(verifier.IncompleteEvidence, match=("pipelines_not_unique" if mutation == "other-pipeline"
                                                         else "refusal_trace_wrong")):
        verifier.live_executor_jobs(scope, evidence, read)


def test_network_job_requires_matching_provider_job_file_variable_and_artifact(sample):
    scope, evidence = sample
    def read(path):
        if path.endswith("/variables"):
            return network_variables(scope)
        if path.endswith("/artifacts/network-result.json"):
            return {"run_id": scope["run_id"], "probes": evidence["probes"]}
        return network_job(scope, evidence)
    api = Mock(side_effect=read)
    verifier.live_network_job(scope, evidence, api, 300)
    assert api.call_count == 3


@pytest.mark.parametrize("mutation,reason", [
    ("wrong-runner", "job_scope"), ("wrong-source", "job_scope"),
    ("wrong-pipeline", "job_scope"), ("failed", "job_scope"),
    ("wrong-file-type", "target_binding"), ("wrong-target", "target_binding"),
    ("missing-target", "target_binding"), ("wrong-artifact", "artifact_wrong"),
])
def test_network_provider_counterexamples_refused(sample, mutation, reason):
    scope, evidence = sample
    job = network_job(scope, evidence)
    variables = network_variables(scope)
    artifact = {"run_id": scope["run_id"], "probes": evidence["probes"]}
    if mutation == "wrong-runner":
        job["runner"]["id"] += 1
    elif mutation == "wrong-source":
        job["commit"]["id"] = "b" * 40
    elif mutation == "wrong-pipeline":
        job["pipeline"]["id"] = 301
    elif mutation == "failed":
        job["status"] = "failed"
    elif mutation == "wrong-file-type":
        next(row for row in variables if row["key"] == "ADP_QUALIFICATION_TARGETS_FILE")["variable_type"] = "env_var"
    elif mutation == "wrong-target":
        target = next(row for row in variables if row["key"] == "ADP_QUALIFICATION_TARGETS_FILE")
        payload = json.loads(target["value"])
        payload["targets"][0]["address"] = "198.51.100.99"
        target["value"] = json.dumps(payload)
    elif mutation == "missing-target":
        variables[:] = [row for row in variables if row["key"] != "ADP_QUALIFICATION_TARGETS_FILE"]
    elif mutation == "wrong-artifact":
        artifact["probes"] = evidence["probes"][:-1]
    api = Mock(side_effect=lambda path: variables if path.endswith("/variables") else
               artifact if path.endswith("/artifacts/network-result.json") else job)
    with pytest.raises(verifier.IncompleteEvidence, match=reason):
        verifier.live_network_job(scope, evidence, api, 300)


@pytest.mark.parametrize("mutation", ["wrong-run", "duplicate", "invalid-address", "wrong-ipv6", "wrong-ipv4",
                                     "wrong-ipv4-port", "wrong-ipv6-port", "reused-job"])
def test_unreviewed_or_reused_network_scope_refused(sample, mutation):
    scope, evidence = sample
    if mutation == "wrong-run":
        scope["network_targets"][0]["expected"] = "allowed"
    elif mutation == "duplicate":
        scope["network_targets"][1] = scope["network_targets"][0]
    elif mutation == "invalid-address":
        scope["network_targets"][0]["address"] = "private.example.test"
    elif mutation in {"wrong-ipv6", "wrong-ipv4", "wrong-ipv4-port", "wrong-ipv6-port"}:
        name = "metadata_ipv6" if "ipv6" in mutation else "metadata_ipv4"
        target = next(row for row in scope["network_targets"] if row["name"] == name)
        if mutation.endswith("port"):
            target["port"] = 443
        else:
            target["address"] = "fd00:ec2::123" if name == "metadata_ipv6" else "169.254.169.253"
    elif mutation == "reused-job":
        evidence["network_job_id"] = evidence["executor_negatives"][0]["gitlab_job_id"]
    with pytest.raises(verifier.IncompleteEvidence):
        verifier.verify(scope, evidence, now=NOW)


@pytest.mark.parametrize("mutation,reason", [
    ("wrong-id", "job_scope"), ("wrong-case", "job_scope"),
    ("wrong-runner", "job_scope"), ("wrong-source", "job_scope"),
    ("pending", "job_outcome"), ("successful-refusal", "job_outcome"),
    ("old-job", "outside_run"), ("wrong-run", "run_binding"),
    ("missing-run", "run_binding"), ("missing-pipeline", "pipeline_missing"),
])
def test_forged_executor_reports_fail_independent_job_readback(sample, mutation, reason):
    scope, evidence = sample
    row = evidence["executor_negatives"][0]
    job = executor_job(scope, evidence, row)
    variables = executor_variables(scope)
    if mutation == "wrong-id": job["id"] += 1
    elif mutation == "wrong-case": job["name"] = "network-probe"
    elif mutation == "wrong-runner": job["runner"]["id"] += 1
    elif mutation == "wrong-source": job["commit"]["id"] = "b" * 40
    elif mutation == "pending": job["status"] = "pending"
    elif mutation == "successful-refusal": row["outcome"] = "refused_before_script"
    elif mutation == "old-job": job["created_at"] = "2025-12-31T00:00:00Z"
    elif mutation == "wrong-run": variables[0]["value"] = "different-run"
    elif mutation == "missing-run": variables = []
    elif mutation == "missing-pipeline": job["pipeline"] = {}
    api = Mock(side_effect=lambda path: executor_artifact(evidence, path) if path.endswith("/artifacts/executor-result.json") else
               variables if path.endswith("/variables") else
               pipeline(scope) if "/pipelines/" in path else job)
    with pytest.raises(verifier.IncompleteEvidence, match=reason):
        verifier.live_executor_jobs(scope, evidence, api)


def test_a_single_job_cannot_replace_two_executor_attempts(sample):
    scope, evidence = sample
    evidence["executor_negatives"][1]["gitlab_job_id"] = evidence["executor_negatives"][0]["gitlab_job_id"]
    with pytest.raises(verifier.IncompleteEvidence, match="executor_job_evidence_reused"):
        verifier.verify(scope, evidence, now=NOW)


@pytest.mark.parametrize("change,reason", [
    ({"outcome": "constrained"}, "disallowed_executor_input_admitted"),
    ({"refusal_reason": "network_error"}, "executor_refusal_not_proven"),
    ({"script_started": True}, "executor_refusal_not_proven"),
    ({"job_container_created": True}, "executor_refusal_not_proven"),
])
def test_failed_job_is_not_proof_of_executor_refusal(sample, change, reason):
    scope, evidence = sample
    row = next(row for row in evidence["executor_negatives"] if row["name"] == "arbitrary_image")
    row.update(change)
    with pytest.raises(verifier.IncompleteEvidence, match=reason):
        verifier.verify(scope, evidence, now=NOW)


@pytest.mark.parametrize("wrong", ["url", "secret"])
def test_live_target_scope_checked_before_admin_credential_is_read(sample, wrong):
    scope, _ = sample
    clients = {name: Mock() for name in ("sts", "ssm", "secretsmanager")}
    session = Mock()
    session.client.side_effect = clients.__getitem__
    clients["sts"].get_caller_identity.return_value = {
        "Account": scope["account_id"], "Arn": "arn:aws:sts::123456789012:assumed-role/operator/session",
    }
    clients["ssm"].get_parameter.return_value = {"Parameter": {"Value": "https://gitlab.dev.adp.internal/gitlab"}}
    args = SimpleNamespace(aws_profile="explicit-test-profile", region="us-east-1",
        expected_role="arn:aws:iam::123456789012:role/operator",
        gitlab_url="https://unexpected.example.test" if wrong == "url" else "https://gitlab.dev.adp.internal/gitlab",
        admin_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:unrelated-Ab12Cd")
    with patch("boto3.Session", return_value=session), pytest.raises(verifier.IncompleteEvidence):
        verifier.live_clients(args, scope)
    clients["secretsmanager"].get_secret_value.assert_not_called()


def test_fixture_schema_refuses_boolean_instead_of_numeric_group(sample):
    scope, evidence = sample
    scope["group_a"] = True
    with pytest.raises(verifier.IncompleteEvidence, match="fixture_schema_invalid"):
        verifier.verify(scope, evidence, now=NOW)


def test_private_files_cannot_be_symlinks_or_group_readable(tmp_path):
    data = tmp_path / "evidence.json"
    data.write_text("{}")
    data.chmod(0o640)
    with pytest.raises(verifier.IncompleteEvidence, match="private_operator_file"):
        verifier.private_json(data)
    data.chmod(0o600)
    assert verifier.private_json(data) == {}
    link = tmp_path / "link.json"
    link.symlink_to(data)
    with pytest.raises(verifier.IncompleteEvidence, match="private_operator_file"):
        verifier.private_json(link)


@pytest.mark.parametrize("result,connected", [("blocked", True), ("blocked", None), ("reachable_protocol_error", True), ("unreachable", False), ("probe_error", False)])
def test_protocol_and_local_failures_cannot_pass_network_denial(sample, result, connected):
    scope, evidence = sample
    evidence["probes"][0].update(result=result, transport_connected=connected)
    with pytest.raises(verifier.IncompleteEvidence, match="denied_destination_reachable"):
        verifier.verify(scope, evidence, now=NOW)


@pytest.fixture
def ipv4_only_sample(sample):
    scope, evidence = sample
    next(row for row in scope['network_targets'] if row['name'] == 'metadata_ipv6')['expected'] = 'disabled_by_provider'
    next(row for row in evidence['probes'] if row['name'] == 'metadata_ipv6').update(
        result='unreachable', errno=101, error_kind='OSError', connection_attempted=True)
    next(row for row in evidence['independent_target_controls'] if row['name'] == 'metadata_ipv6').update(
        reachable=False, source='ec2_and_autoscaling_api', result='disabled_by_provider')
    for row in evidence['cold_starts'] + [evidence['daemon_restart']]:
        row['metadata_ipv6_result'] = 'disabled_by_provider'
    # The network suite has its own instance and independently retained console.
    network = copy.deepcopy(evidence['host_histories'][0])
    network['scope']['instance']['instance_id'] = 'i-5'
    for record in network['records']:
        if 'job_id' in record['data']:
            record['data']['job_id'] = evidence['network_job_id']
    evidence['host_histories'].append(network)
    evidence['hosts'].append({**evidence['hosts'][0], 'instance_id': 'i-5'})
    guard_sha = hashlib.sha256((ROOT.parents[1] / 'runners/templates/job-user-data.sh.tftpl').read_bytes()).hexdigest()
    for history in evidence['host_histories']:
        for record in history['records']:
            if 'host_enforcement' in record['data']:
                record['data']['host_enforcement'].update(guard_sha256=guard_sha, policy_sha256='b' * 64)
        for snapshot in history['snapshots']:
            snapshot['record_sha256s'] = record_hashes(history['records'])
            snapshot['ipv6_metadata'] = {
                'source': 'ec2_and_autoscaling_api', 'result': 'disabled_by_provider',
                **{key: scope[key] for key in ('run_id', 'source_revision', 'account_id')},
                'started_at': snapshot['provider_timestamp'], 'observed_at': snapshot['observed_at'],
                'aws_request_ids': {key: 'synthetic-request' for key in ('ec2_instance', 'asg_membership', 'ec2_launch_template')},
                'instance': {'instance_id': history['scope']['instance']['instance_id'], 'ami_id': scope['ami_id'],
                             'vpc_id': scope['vpc_id'], 'subnet_id': scope['private_subnet_ids'][0],
                             'metadata_ipv6': 'disabled', 'metadata_state': 'applied', 'http_tokens': 'required',
                             'instance_profile': None, 'interfaces': [{'id': 'eni-a', 'ipv6_addresses': []}]},
                'asg_name': scope['asg_names'][0],
                'launch_template': {'id': 'lt-a', 'version': 1, 'ami_id': scope['ami_id'],
                                    'metadata_ipv6': 'disabled', 'http_tokens': 'required', 'instance_profile': None},
            }
    return scope, evidence


def test_ipv4_only_mode_requires_distinct_executed_provider_result(ipv4_only_sample):
    scope, evidence = ipv4_only_sample
    result = verifier.verify(scope, evidence, now=NOW)
    assert result['verdict'] == 'pass' and result['metadata_ipv6'] == 'disabled_by_provider'
    assert result['network_cases'] == 15
    assert next(row for row in evidence['probes'] if row['name'] == 'metadata_ipv6')['result'] == 'unreachable'


@pytest.mark.parametrize('mutation,reason', [
    (lambda e: e['host_histories'][0]['snapshots'][0].pop('ipv6_metadata'), 'ipv6_provider_receipt_missing'),
    (lambda e: e['host_histories'][0]['snapshots'][0]['ipv6_metadata']['instance'].update(metadata_ipv6='enabled'), 'ipv6_provider_receipt_invalid'),
    (lambda e: e['host_histories'][0]['snapshots'][0]['ipv6_metadata']['instance'].update(metadata_state='pending'), 'ipv6_provider_receipt_invalid'),
    (lambda e: e['host_histories'][0]['snapshots'][0]['ipv6_metadata']['instance'].update(instance_id='i-b'), 'ipv6_provider_receipt_invalid'),
    (lambda e: e['host_histories'][0]['snapshots'][0]['ipv6_metadata']['instance'].update(instance_profile='unexpected'), 'ipv6_provider_receipt_invalid'),
    (lambda e: e['host_histories'][0]['snapshots'][0]['ipv6_metadata']['instance']['interfaces'][0].update(ipv6_addresses=['2001:db8::1']), 'ipv6_provider_receipt_invalid'),
    (lambda e: e['host_histories'][0]['snapshots'][0]['ipv6_metadata']['launch_template'].update(metadata_ipv6='enabled'), 'ipv6_provider_receipt_invalid'),
    (lambda e: e['host_histories'][0]['snapshots'][0]['ipv6_metadata']['launch_template'].update(version='$Latest'), 'ipv6_provider_receipt_invalid'),
    (lambda e: e['host_histories'][0]['snapshots'][0]['ipv6_metadata'].update(run_id='other'), 'ipv6_provider_receipt_invalid'),
    (lambda e: e['host_histories'][0]['snapshots'][0]['ipv6_metadata'].update(started_at='2025-01-01T00:00:00Z'), 'ipv6_provider_receipt_invalid'),
    (lambda e: e['host_histories'][0]['snapshots'][0]['ipv6_metadata']['aws_request_ids'].pop('ec2_launch_template'), 'ipv6_provider_receipt_invalid'),
    (lambda e: e['host_histories'][0]['records'][0]['data']['host_enforcement'].update(guard_sha256='f'*64), 'ipv6_host_enforcement_unverified'),
    (lambda e: e['daemon_restart'].pop('metadata_ipv6_result'), 'ipv6_lifecycle_result_missing'),
    (lambda e: e['host_histories'].pop(), 'host_histories_missing'),
])
def test_ipv4_only_metadata_counterexamples_fail_closed(ipv4_only_sample, mutation, reason):
    scope, evidence = ipv4_only_sample
    mutation(evidence)
    for history in evidence['host_histories']:
        history['snapshots'][0]['record_sha256s'] = record_hashes(history['records'])
    with pytest.raises(verifier.IncompleteEvidence, match=reason):
        verifier.verify(scope, evidence, now=NOW)


@pytest.mark.parametrize('change', [
    {'connection_attempted': False}, {'transport_connected': True}, {'result': 'allowed'},
    {'result': 'skipped'}, {'result': 'blocked', 'error_kind': 'OSError'}, {'errno': 111},
])
def test_ipv4_only_probe_still_must_execute_without_connection(ipv4_only_sample, change):
    scope, evidence = ipv4_only_sample
    next(row for row in evidence['probes'] if row['name'] == 'metadata_ipv6').update(change)
    with pytest.raises(verifier.IncompleteEvidence, match='denied_destination_reachable'):
        verifier.verify(scope, evidence, now=NOW)


@pytest.mark.parametrize('name', ['metadata_ipv4', 'cluster_api', 'internet'])
def test_ipv4_only_exception_cannot_hide_other_missing_controls(ipv4_only_sample, name):
    scope, evidence = ipv4_only_sample
    next(row for row in evidence['independent_target_controls'] if row['name'] == name)['reachable'] = False
    with pytest.raises(verifier.IncompleteEvidence, match='negative_destination_not_listening'):
        verifier.verify(scope, evidence, now=NOW)
    next(row for row in scope['network_targets'] if row['name'] == name)['expected'] = 'disabled_by_provider'
    with pytest.raises(verifier.IncompleteEvidence, match='fixture_schema_invalid'):
        verifier.verify(scope, evidence, now=NOW)


def test_ec04_native_workflow_revisions_require_exact_retained_context_bindings(sample):
    scope, evidence = sample
    sources = {}
    for row in evidence['secret_scan']['workflow_runs']:
        expected = {'run_id': row['run_id'], 'run_attempt': 1, 'repository_id': 456, 'source_revision': 'b' * 40,
                    'workflow_revision': 'c' * 40, 'correlation': 'orch:reviewed', 'contract_digest': 'd' * 64,
                    'context_sha256': 'e' * 64, 'context_reference': 's3://adp-dev-deployment-evidence-' + scope['account_id']
                    + '/deployment-evidence/v1/456/' + str(row['run_id']) + '/1/context/' + row['workflow'] + '.json?versionId=version1',
                    'region': 'us-east-1'}
        sources[row['workflow']] = expected
        row.update({k: v for k, v in expected.items() if k not in ('repository_id', 'region')}, head_sha=expected['workflow_revision'])
    scope['workflow_sources'] = sources
    assert verifier.verify(scope, evidence, now=NOW)['verdict'] == 'pass'
    evidence['secret_scan']['workflow_runs'][0]['context_sha256'] = 'f' * 64
    with pytest.raises(verifier.IncompleteEvidence, match='secret_scan_run_wrong'):
        verifier.verify(scope, evidence, now=NOW)
