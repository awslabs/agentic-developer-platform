#!/usr/bin/env python3
"""Evaluate private operator-collected GitLab, EC2/ASG and trusted host evidence.

Synthetic fixtures exercise the verifier only. Live acceptance requires the
delivery owner to collect these records from the named independent APIs and
trusted host/controller logs; a job's self-report is never an inventory oracle.
"""
import argparse
import errno
import hashlib
import http.client
import importlib.util
import ipaddress
import json
import os
import re
import stat
import ssl
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent


def support_module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ipv6_metadata = support_module("ipv6_metadata")
network_probe = support_module("probe")
ec04_audit = support_module("ec04")
NEGATIVE_TARGETS = {
    "gateway", "devbox", "rds", "redis", "other_tenant", "metadata_ipv4",
    "metadata_ipv6", "cluster_api", "sts", "secretsmanager", "internet",
    "alternate_dns", "doh",
}
POSITIVE_TARGETS = {"gitlab", "cache"}
METADATA_TARGETS = {"metadata_ipv4": ("169.254.169.254", 80, "tcp"),
                    "metadata_ipv6": ("fd00:ec2::254", 80, "tcp")}
LIFECYCLE_CASES = {"legal", "failed", "cancelled", "manager_loss"}
IDENTITY_CASES = {
    "manager_ca": "allowed", "manager_own_token": "allowed",
    "gateway_ca": "AccessDenied", "agent_ca": "AccessDenied",
    "manager_other_token": "AccessDenied", "exporter_admin": "AccessDenied",
    "exporter_group_token": "AccessDenied",
}
REFUSAL_TRACES = {
    "arbitrary_image": re.compile(r'The "(?P<image>[^"\r\n]{1,256})" image is not present on list of allowed images:'),
    "arbitrary_service": re.compile(r"too many services requested: 1, only 0 allowed\b"),
    "root_user": re.compile(r'user "0" is not an allowed user: \[10001:10001\]'),
}


class IncompleteEvidence(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise IncompleteEvidence(code)


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "timestamp_timezone_missing")
    return parsed


def timestamp_ns(value):
    """Convert the timestamp's microseconds without a lossy float round trip."""
    delta = timestamp(value) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return ((delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds) * 1000


def by_name(rows):
    require(isinstance(rows, list), "evidence_rows_missing")
    mapped = {row["name"]: row for row in rows}
    require(len(mapped) == len(rows), "duplicate_evidence")
    return mapped


def expected_outcomes(name):
    if name == "metadata_ipv6":
        return {"blocked", "disabled_by_provider"}
    return {"allowed" if name in POSITIVE_TARGETS else "blocked"}


def negative_probe_matches(name, row, targets):
    if name == "metadata_ipv6" and targets[name]["expected"] == "disabled_by_provider":
        return network_probe.expected_result(targets[name], row)
    return (row.get("name") == name and row.get("result") == "blocked"
            and row.get("transport_connected") is False and row.get("error_kind") == "timeout"
            and (name not in METADATA_TARGETS or row.get("connection_attempted") is True))


def verify_ipv6_metadata(fixtures, evidence, start, end):
    """Provider-disabled is a separate executed case, never a firewall-drop claim."""
    if not ipv6_metadata.enabled(fixtures):
        return
    jobs = {row["job_id"] for row in evidence["cold_starts"] + [evidence["daemon_restart"]]}
    jobs.add(evidence["network_job_id"])
    observed = set()
    guard_sha = hashlib.sha256((ROOT.parents[1] / "runners/templates/job-user-data.sh.tftpl").read_bytes()).hexdigest()
    for history in evidence["host_histories"]:
        instance_id = history["scope"]["instance"]["instance_id"]
        observations = [record for record in history["records"] if record["kind"] == "container_observation"
                        and record["data"].get("job_id") in jobs]
        if not observations:
            continue
        receipts = [snapshot.get("ipv6_metadata") for snapshot in history["snapshots"]]
        require(receipts and all(isinstance(receipt, dict) for receipt in receipts), "ipv6_provider_receipt_missing")
        for receipt in receipts:
            try:
                ipv6_metadata.validate(receipt, fixtures, instance_id, start, end)
            except (KeyError, TypeError, ValueError) as error:
                raise IncompleteEvidence("ipv6_provider_receipt_invalid") from error
        require(all(receipt["instance"] == receipts[0]["instance"]
                    and receipt["launch_template"] == receipts[0]["launch_template"] for receipt in receipts),
                "ipv6_provider_binding_changed")
        for record in history["records"]:
            if record["kind"] not in {"stream_started", "container_observation"}:
                continue
            guard = record["data"].get("host_enforcement", {})
            require(guard.get("status") == "verified" and guard.get("guard_sha256") == guard_sha
                    and re.fullmatch(r"[0-9a-f]{64}", guard.get("policy_sha256", "")),
                    "ipv6_host_enforcement_unverified")
        for record in observations:
            require(any(timestamp_ns(receipt["started_at"]) <= record["observed_at_unix_ns"]
                        <= timestamp_ns(receipt["observed_at"]) + 60 * 10**9
                        or record["observed_at_unix_ns"] <= timestamp_ns(receipt["started_at"])
                        <= record["observed_at_unix_ns"] + 60 * 10**9 for receipt in receipts),
                    "ipv6_provider_receipt_not_contemporaneous")
            observed.add(record["data"]["job_id"])
    require(observed == jobs, "ipv6_job_host_correlation_missing")
    require(all(row.get("metadata_ipv6_result") == "disabled_by_provider"
                for row in evidence["cold_starts"] + [evidence["daemon_restart"]]),
            "ipv6_lifecycle_result_missing")


def verify(fixtures, evidence, *, now=None, final=True):
    """Reject partial evidence, including superficially successful job logs."""
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(json.loads((ROOT / "fixtures.schema.json").read_text()),
                                     format_checker=Draft202012Validator.FORMAT_CHECKER)
    require(validator.is_valid(fixtures), "fixture_schema_invalid")
    now = now or datetime.now(timezone.utc)
    require(fixtures["schema_version"] == 1 and fixtures["run_owned"] is True, "run_ownership_missing")
    require(evidence["run_id"] == fixtures["run_id"], "wrong_run")
    require(evidence["source_revision"] == fixtures["source_revision"]
            and re.fullmatch(r"[0-9a-f]{40}", fixtures["source_revision"]), "wrong_source_revision")
    start, end = timestamp(evidence["started_at"]), timestamp(evidence["completed_at"])
    require(start <= end <= now and (now - end).total_seconds() <= 3600, "evidence_not_fresh")
    require(evidence["account_id"] == fixtures["account_id"], "wrong_account")
    require(fixtures["group_a"] != fixtures["group_b"], "groups_not_distinct")
    targets = by_name(fixtures["network_targets"])
    require(set(targets) == NEGATIVE_TARGETS | POSITIVE_TARGETS, "qualification_targets_missing")
    for name, target in targets.items():
        try:
            address = ipaddress.ip_address(target["address"])
        except ValueError as error:
            raise IncompleteEvidence("qualification_target_address_invalid") from error
        require(target["expected"] in expected_outcomes(name)
                and (name not in METADATA_TARGETS or
                     (str(address), target["port"], target["kind"]) == METADATA_TARGETS[name]),
                "qualification_target_scope_wrong")
        require((name not in {"gitlab", "doh"} or (target["kind"] == "https" and target.get("hostname")
                 and target.get("expected_status") == 200))
                and (name != "alternate_dns" or target["kind"] == "dns"), "qualification_target_protocol_wrong")
    require(type(evidence["network_job_id"]) is int and evidence["network_job_id"] > 0,
            "network_job_missing")
    require(evidence["collection_sources"] == ["gitlab_api", "ec2_api", "autoscaling_api", "kubernetes_api", "trusted_host"],
            "independent_oracles_missing")
    identities = by_name(evidence["identity_checks"])
    require(set(identities) == set(IDENTITY_CASES), "identity_checks_missing")
    for name, expected in IDENTITY_CASES.items():
        row = identities[name]
        require(row["result"] == expected and row["verified_caller_role_arn"] == fixtures["identity_role_arns"][name]
                and row["aws_request_id"] and row["transport_succeeded"] is True, "identity_boundary_unproven")
    for name in ("manager_ca", "gateway_ca", "agent_ca"):
        row = identities[name]
        require(row.get("action") == "secretsmanager:GetSecretValue"
                and row.get("secret_id") == "adp/dev/gitlab/ca-chain"
                and (type(row.get("exit_code")) is int and row["exit_code"] == 0
                     and row.get("error_code") is None if name == "manager_ca"
                     else type(row.get("exit_code")) is int and row["exit_code"] != 0
                     and row.get("error_code") == "AccessDeniedException"), "ca_identity_probe_unproven")

    ec01 = evidence.get("ec01")
    require(isinstance(ec01, dict), "registration_inventory_missing")
    registrations = ec01.get("registrations")
    require(isinstance(registrations, list) and registrations, "registration_inventory_missing")
    require(all(isinstance(row, dict) and type(row.get("runner_id")) is int and row["runner_id"] > 0
                and type(row.get("group_id")) is int and row["group_id"] in (fixtures["group_a"], fixtures["group_b"])
                and row.get("token_secret_name") == f'adp/dev/gitlab-runner/{row["group_id"]}'
                for row in registrations), "registration_inventory_wrong")
    require(len({row["runner_id"] for row in registrations}) == len(registrations)
            and len({row["group_id"] for row in registrations}) == len(registrations)
            and any(row["runner_id"] == fixtures["runner_a"] and row["group_id"] == fixtures["group_a"]
                    for row in registrations), "registration_inventory_wrong")
    instance_ids = ec01.get("instance_runner_ids")
    require(isinstance(instance_ids, list) and all(type(runner_id) is int and runner_id > 0 for runner_id in instance_ids)
            and len(instance_ids) == len(set(instance_ids))
            and not set(instance_ids) & {row["runner_id"] for row in registrations}, "instance_runner_scope_unproven")

    # These are provider readbacks, collected outside the tenant job.
    a, b = evidence["gitlab"]["a"], evidence["gitlab"]["b"]
    require(a["group_id"] == fixtures["group_a"] and b["group_id"] == fixtures["group_b"], "wrong_group")
    require(a["project_id"] == fixtures["project_a"] and b["project_id"] == fixtures["project_b"], "wrong_project")
    require(a["status"] == "success" and a["runner_id"] == fixtures["runner_a"]
            and type(a.get("pipeline_id")) is int and a["pipeline_id"] > 0
            and type(a.get("job_id")) is int and a["job_id"] > 0,
            "wrong_runner_assignment")
    require(isinstance(a.get("runner_trace_line"), str)
            and re.fullmatch(r"Running on runner-[A-Za-z0-9_-]+(?:-project-[0-9]+-concurrent-[0-9]+)? via [^\r\n]{1,128}",
                             a["runner_trace_line"]) is not None, "product_runner_trace_missing")
    require(0 <= a["duration_seconds"] <= 600 and a["tag_list"] == b["tag_list"], "pipeline_bound_or_tags_wrong")
    runner = evidence["gitlab"]["runner"]
    require(runner["id"] == fixtures["runner_a"] and runner["runner_type"] == "group_type"
            and runner["group_ids"] == [fixtures["group_a"]] and runner["locked"] is True
            and runner["run_untagged"] is False, "runner_scope_not_verified")
    pending = b["pending_observations"]
    require(type(b.get("pipeline_id")) is int and b["pipeline_id"] > 0
            and b["final_status"] == "canceled", "group_b_not_observed_and_cancelled")
    require(len(pending) >= 11, "group_b_pending_interval_unproven")
    times = [timestamp(row["observed_at"]) for row in pending]
    require(all(row.get("status") == "pending" and row.get("runner_id") is None
                and row.get("job_id") == b["job_id"] and row.get("pipeline_id") == b["pipeline_id"]
                and row.get("source_revision") == fixtures["source_revision"]
                and row.get("tag_list") == [f'adp-group-{fixtures["group_a"]}'] for row in pending),
            "group_b_pending_scope_wrong")
    require(all(start <= moment <= end for moment in times)
            and all(0 < (right - left).total_seconds() <= 60 for left, right in zip(times, times[1:]))
            and (times[-1] - times[0]).total_seconds() >= 300, "group_b_pending_interval_unproven")

    hosts = evidence["hosts"]
    require(isinstance(hosts, list) and hosts, "host_inventory_missing")
    require(len({host["instance_id"] for host in hosts}) == len(hosts), "duplicate_host")
    require(a["instance_id"] in {host["instance_id"] for host in hosts}, "job_instance_correlation_missing")
    for host in hosts:
        require(host["vpc_id"] == fixtures["vpc_id"], "wrong_vpc")
        require(host["subnet_id"] in fixtures["private_subnet_ids"]
                and host["security_group_ids"] == fixtures["job_security_group_ids"]
                and host["public_ip"] is None and host["public_ipv6"] == [], "host_not_private_and_scoped")
        require(host["ami_id"] == fixtures["ami_id"] and host["group_id"] == fixtures["group_a"], "wrong_host_provenance")
        require(host["manager_credentials_mounted"] is False and host["instance_reused"] is False, "host_identity_or_reuse_violation")

    probes = by_name(evidence["probes"])
    require(set(probes) == NEGATIVE_TARGETS | POSITIVE_TARGETS, "missing_negative_cases")
    require(set(probes) == set(targets) and evidence["network_job_id"] != a["job_id"]
            and evidence["network_job_id"] not in [row["gitlab_job_id"] for row in evidence["executor_negatives"]],
            "network_job_evidence_reused")
    controls = by_name(evidence["independent_target_controls"])
    require(set(controls) == NEGATIVE_TARGETS, "negative_target_controls_missing")
    for name in NEGATIVE_TARGETS:
        if name == "metadata_ipv6" and ipv6_metadata.enabled(fixtures):
            require(controls[name].get("reachable") is False
                    and controls[name].get("source") == "ec2_and_autoscaling_api"
                    and controls[name].get("result") == "disabled_by_provider", "ipv6_provider_control_missing")
        else:
            require(controls[name]["reachable"] is True and controls[name]["source"] == "trusted_reference_host",
                    "negative_destination_not_listening")
        require(controls[name]["address"] == targets[name]["address"]
                and controls[name]["port"] == targets[name]["port"], "negative_control_target_wrong")
        require(negative_probe_matches(name, probes[name], targets), "denied_destination_reachable")
    for name in POSITIVE_TARGETS:
        require(probes[name]["result"] == "allowed"
                and probes[name].get("transport_connected") is True, "positive_control_failed")
    cache = evidence["cache_roundtrip"]
    require(cache["writer_group_id"] == fixtures["group_a"] and cache["reader_group_id"] == fixtures["group_a"]
            and cache["upload_succeeded"] is True and cache["download_succeeded"] is True
            and cache["expected_sha256"] == cache["downloaded_sha256"]
            and re.fullmatch(r"[0-9a-f]{64}", cache["expected_sha256"]), "cache_roundtrip_unproven")
    require(all(0 <= row["elapsed_seconds"] <= 3.5 for row in probes.values()), "connection_timeout_exceeded")

    cases = json.loads((ROOT / "executor-negative-cases.json").read_text())["cases"]
    negatives = by_name(evidence["executor_negatives"])
    require(set(negatives) == {case["name"] for case in cases}, "executor_negative_cases_missing")
    negative_job_ids = [row["gitlab_job_id"] for row in negatives.values()]
    require(len(set(negative_job_ids)) == len(negative_job_ids)
            and a["job_id"] not in negative_job_ids, "executor_job_evidence_reused")
    for result in negatives.values():
        require(type(result["gitlab_job_id"]) is int and result["gitlab_job_id"] > 0
                and result["executor"] == "docker-autoscaler"
                and result["outcome"] in {"refused_before_script", "constrained"}
                and result["trusted_host_verified"] is True, "executor_denial_not_proven")
        require(result["invalid_yaml"] is False, "executor_container_not_confined")
        refusal_reasons = {"arbitrary_image": "image_not_allowed", "arbitrary_service": "service_not_allowed",
                           "root_user": "user_not_allowed"}
        if result["name"] in refusal_reasons:
            require(result["outcome"] == "refused_before_script", "disallowed_executor_input_admitted")
        if result["outcome"] == "refused_before_script":
            require(result["name"] in refusal_reasons
                    and result.get("refusal_reason") == refusal_reasons[result["name"]]
                    and result.get("script_started") is False and result.get("job_container_created") is False,
                    "executor_refusal_not_proven")
        else:
            require(result["privileged"] is False and type(result["uid"]) is int and result["uid"] > 0
                    and result["added_capabilities"] == [] and result["host_namespaces"] == [] and result["host_mounts"] == []
                    and result["devices"] == [] and result["credential_mounts"] == [], "executor_container_not_confined")
            probe = result.get("probe")
            require(isinstance(probe, dict) and probe.get("name") == result["name"]
                    and probe.get("trusted_host_verified") is False
                    and probe.get("process") == {"uid": result["uid"], "effective_capabilities": 0,
                                                 "no_new_privileges": True}, "executor_probe_not_executed")
            observations = probe.get("observations")
            if result["name"] == "debug_trace":
                require(probe.get("probe_result") == "requires_independent_trace_scan" and observations == [],
                        "executor_probe_not_executed")
            else:
                require(probe.get("probe_result") == "inaccessible"
                        and isinstance(observations, list) and len(observations) == (2 if result["name"] == "host_device" else
                            4 if result["name"] == "manager_credentials" else 1)
                        and all(item.get("result") == "inaccessible"
                                and item.get("errno") in {errno.EACCES, errno.EPERM, errno.ENOENT}
                                for item in observations)
                        and (result["name"] != "manager_credentials"
                             or probe.get("credential_environment_present") is False), "executor_probe_not_executed")

    starts = evidence["cold_starts"]
    require(len(starts) == 3 and len({row["instance_id"] for row in starts}) == 3, "three_fresh_starts_required")
    for row in starts + [evidence["daemon_restart"]]:
        require(row["instance_id"] in {host["instance_id"] for host in hosts}, "early_probe_unknown_instance")
        deny, ready, first = (timestamp(row[key]) for key in ("denial_installed_at", "executor_ready_at", "first_instruction_at"))
        require(start <= deny <= ready <= first <= end, "early_denial_evidence_missing")
        require(row["trusted_host_verified"] is True and row["first_instruction_denied"] is True
                and row["metadata_ipv4_denied"] is True and row["metadata_ipv6_denied"] is True
                and row["positive_controls_succeeded"] is True, "early_denial_not_proven")
    verify_host_histories(fixtures, evidence, hosts, start, end)
    verify_executor_host_histories(fixtures, evidence, hosts, negatives)
    verify_ipv6_metadata(fixtures, evidence, start, end)
    require(evidence["bootstrap_failure"]["executor_ready"] is False
            and evidence["bootstrap_failure"]["accepted_jobs"] == 0
            and evidence["bootstrap_failure"]["trusted_host_verified"] is True, "failed_bootstrap_admitted_job")

    lifecycle = by_name(evidence["cleanup"])
    require(set(lifecycle) == LIFECYCLE_CASES, "cleanup_cases_missing")
    cleanup_jobs = set()
    for name, row in lifecycle.items():
        require(type(row.get("job_id")) is int and row["job_id"] > 0 and row["job_id"] not in cleanup_jobs
                and row.get("group_id") == fixtures["group_a"] and row.get("runner_id") == fixtures["runner_a"]
                and row.get("instance_id") in {host["instance_id"] for host in hosts}
                and type(row.get("pipeline_id")) is int and row["pipeline_id"] > 0
                and row.get("job_status") == {"legal": "success", "failed": "failed",
                                               "cancelled": "canceled", "manager_loss": "canceled"}[name],
                "cleanup_job_scope_wrong")
        cleanup_jobs.add(row["job_id"])
        finished, observed = timestamp(row["job_finished_at"]), timestamp(row["observed_at"])
        require(start <= finished <= observed <= end and 0 <= (observed - finished).total_seconds() <= 900
                and row.get("elapsed_seconds") == (observed - finished).total_seconds(),
                "cleanup_deadline_exceeded")
        receipts = row.get("aws_request_ids")
        require(isinstance(receipts, dict) and set(receipts) == {"autoscaling", "ec2"}
                and isinstance(receipts["autoscaling"], str)
                and re.fullmatch(r"[A-Za-z0-9-]{1,128}", receipts["autoscaling"])
                and isinstance(receipts["ec2"], list) and receipts["ec2"]
                and all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9-]{1,128}", value)
                        for value in receipts["ec2"])
                and isinstance(row.get("manager_resource_version"), str) and row["manager_resource_version"],
                "cleanup_provider_receipts_missing")
        if name == "manager_loss":
            loss = row.get("manager_loss_receipt")
            require(isinstance(loss, dict) and isinstance(loss.get("unready_resource_version"), str)
                    and loss["unready_resource_version"] != row["manager_resource_version"]
                    and loss.get("unready_ready_replicas") == 0
                    and finished <= timestamp(loss["unready_at"]) < observed,
                    "manager_loss_receipt_missing")
        require(row["asg_desired"] == 0 and row["asg_instances"] == []
                and row["ec2_nonterminated_instances"] == [], "orphan_instance_remaining")
        require(row["manager_ready"] == 1 and row["manager_desired"] == 1, "manager_not_healthy")
        require(set(row["observed_asg_names"]) == set(fixtures["asg_names"]), "incomplete_asg_inventory")
    expected_plans = {"absent": 0, "present": 0}
    if final:
        expected_plans["after_teardown"] = 0
        require(evidence["teardown_complete"] is True and evidence["token_revoked"] is True, "teardown_or_revocation_unproven")
        revoked = evidence["token_revocation"]
        require(revoked["runner_id"] == fixtures["runner_a"] and revoked["baseline_http_status"] == 200
                and revoked["after_revoke_http_status"] in (401, 403)
                and revoked["same_secret_version"] is True and revoked["tls_verified"] is True,
                "token_rejection_not_proven")
    require(evidence["instance_plan_exit_codes"] == expected_plans, "instance_independence_unproven")
    verify_ec04(fixtures, evidence, start, end, final=final)
    return {"verdict": "pass" if final else "pre_teardown_checks_passed", "scope": "operator-collected qualification evidence",
            "network_cases": len(probes), "executor_cases": len(negatives), "cleanup_cases": len(lifecycle),
            "metadata_ipv6": "disabled_by_provider" if ipv6_metadata.enabled(fixtures) else "packet_denial"}


def verify_host_histories(fixtures, evidence, hosts, start, end):
    histories = evidence.get("host_histories")
    constrained = sum(row["outcome"] == "constrained" for row in evidence["executor_negatives"])
    require(isinstance(histories, list) and len(histories) == constrained + (6 if ipv6_metadata.enabled(fixtures) else 5),
            "host_histories_missing")
    indexed = {}
    for history in histories:
        require(isinstance(history, dict) and isinstance(history.get("scope"), dict)
                and isinstance(history.get("records"), list) and history["records"]
                and history.get("history_status") == "contiguous_so_far"
                and isinstance(history.get("snapshots"), list) and history["snapshots"], "host_history_incomplete")
        scope = history["scope"]
        instance = scope.get("instance")
        require(scope.get("run_id") == fixtures["run_id"]
                and scope.get("account_id") == fixtures["account_id"]
                and isinstance(instance, dict) and instance.get("instance_id") in {row["instance_id"] for row in hosts}
                and instance["instance_id"] not in indexed
                and instance["ami_id"] == fixtures["ami_id"]
                and instance["vpc_id"] == fixtures["vpc_id"]
                and instance["group_id"] == fixtures["group_a"]
                and instance["security_group_ids"] == fixtures["job_security_group_ids"]
                and instance["instance_profile"] is None and instance["public_ip"] is None,
                "host_history_scope_wrong")
        records = history["records"]
        digests = {hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest():
                   record for record in records}
        covered = set()
        for snapshot in history["snapshots"]:
            hashes = snapshot.get("record_sha256s")
            require(snapshot.get("aws_request_id") and not snapshot.get("problems")
                    and start <= timestamp(snapshot["provider_timestamp"]) <= timestamp(snapshot["observed_at"])
                    and isinstance(hashes, list) and hashes
                    and all(isinstance(digest, str) and digest in digests for digest in hashes),
                    "host_provider_receipt_missing")
            # EC2 console timestamps have whole-second precision. Each receipt
            # covers only its own poll, not records first seen by later polls.
            require(all(timestamp_ns(snapshot["provider_timestamp"]) // 10**9
                        >= digests[digest]["observed_at_unix_ns"] // 10**9
                        and timestamp_ns(snapshot["observed_at"]) // 1000
                        >= digests[digest]["observed_at_unix_ns"] // 1000 for digest in hashes),
                    "host_provider_receipt_missing")
            covered.update(hashes)
        require(covered == set(digests), "host_provider_record_uncovered")
        streams = {}
        for record in records:
            require(record.get("schema_version") == 1
                    and record.get("observer_sha256") == scope.get("observer_sha256")
                    and re.fullmatch(r"[0-9a-f]{64}", scope.get("observer_sha256", ""))
                    and record.get("boot_id") and record.get("stream_id")
                    and type(record.get("sequence")) is int, "host_record_untrusted")
            streams.setdefault((record["boot_id"], record["stream_id"]), []).append(record)
        for rows in streams.values():
            require(rows[0]["kind"] == "stream_started"
                    and [row["sequence"] for row in rows] == list(range(1, len(rows) + 1))
                    and all(left["observed_at_unix_ns"] <= right["observed_at_unix_ns"]
                            for left, right in zip(rows, rows[1:]))
                    and all(row["kind"] not in {"observation_incomplete", "stream_incomplete"} for row in rows),
                    "host_stream_not_contiguous")
        require(all(left["observed_at_unix_ns"] <= right["observed_at_unix_ns"]
                    for left, right in zip(records, records[1:])), "host_stream_order_wrong")
        indexed[instance["instance_id"]] = history

    require({row["instance_id"] for row in evidence["cold_starts"]} <= set(indexed), "cold_start_history_missing")
    restart = evidence["daemon_restart"]
    cold_instances = {row["instance_id"] for row in evidence["cold_starts"]}
    require(restart["instance_id"] in indexed and restart["instance_id"] not in cold_instances,
            "daemon_restart_requires_fresh_host")
    for row in evidence["cold_starts"] + [evidence["daemon_restart"]]:
        records = indexed[row["instance_id"]]["records"]
        require(all(record["data"].get("job_id") == row["job_id"]
                    for record in records if record["kind"] in {"container_event", "container_observation"}),
                "one_job_per_host_required")
        streams = {(record["boot_id"], record["stream_id"])
                   for record in records if record["kind"] == "stream_started"}
        key = (row["boot_id"], row["stream_id"])
        require(key in streams, "early_denial_stream_missing")
        events = [record for record in records if (record["boot_id"], record["stream_id"]) == key
                  and record["kind"] == "container_event" and record["data"].get("action") == "start"]
        require(events and events[0]["data"].get("job_id") == row["job_id"]
                and events[0]["data"].get("project_id") == fixtures["project_a"]
                and events[0]["data"].get("source_revision") == fixtures["source_revision"]
                and events[0]["data"].get("pipeline_id") == row["pipeline_id"], "first_job_host_event_missing")
        observations = [record["data"] for record in records if record["kind"] == "container_observation"
                        and (record["boot_id"], record["stream_id"]) == key
                        and record["data"].get("container_id") == events[0]["data"].get("container_id")]
        require(len(observations) == 1 and observations[0].get("host_enforcement", {}).get("status") == "verified",
                "first_job_guard_unverified")
        guard = observations[0]["host_enforcement"]
        deny_ns, ready_ns = (guard.get(key) for key in ("denial_installed_at_unix_ns", "executor_ready_at_unix_ns"))
        docker_ns = events[0]["data"].get("docker_time_unix_ns")
        # ISO fields describe microseconds; host stat/Docker observations retain
        # nanoseconds. Compare their stated precision, keeping raw host order.
        require(all(type(value) is int and value > 0 for value in (deny_ns, ready_ns, docker_ns))
                and deny_ns // 1000 == timestamp_ns(row["denial_installed_at"]) // 1000
                and ready_ns // 1000 == timestamp_ns(row["executor_ready_at"]) // 1000
                and deny_ns <= ready_ns <= docker_ns
                and docker_ns // 1000 <= timestamp_ns(row["first_instruction_at"]) // 1000,
                "early_denial_host_time_wrong")
    records = indexed[restart["instance_id"]]["records"]
    starts = [record for record in records if record["kind"] == "stream_started"]
    require(len(starts) == 2 and starts[0]["boot_id"] == starts[1]["boot_id"] == restart["boot_id"]
            and starts[0]["stream_id"] != starts[1]["stream_id"] == restart["stream_id"]
            and starts[0]["observed_at_unix_ns"] < starts[1]["observed_at_unix_ns"],
            "daemon_restart_not_observed")
    before_restart = [record for record in records if record["stream_id"] == starts[0]["stream_id"]]
    require(0 <= starts[1]["observed_at_unix_ns"] - before_restart[-1]["observed_at_unix_ns"] <= 60 * 10**9,
            "daemon_restart_observation_gap")
    require(not any(record["kind"] in {"container_event", "container_observation"}
                    or record["data"].get("executor_ready") is True for record in records
                    if record["stream_id"] == starts[0]["stream_id"]), "restart_host_already_used")
    for row in starts:
        guard = row["data"].get("host_enforcement", {})
        require(row["data"].get("executor_ready") is False and guard.get("status") == "verified"
                and guard.get("executor_ready_at_unix_ns") is None
                and type(guard.get("denial_installed_at_unix_ns")) is int
                and timestamp_ns(evidence["started_at"]) <= guard["denial_installed_at_unix_ns"]
                <= row["observed_at_unix_ns"], "restart_denial_not_observed")
        services = row["data"].get("service_invocations", {})
        require(all(re.fullmatch(r"[0-9a-f]{32}", services.get(service, ""))
                    for service in ("docker.service", "adp-job-firewall.service")),
                "restart_service_identity_missing")
    require(all(starts[0]["data"]["service_invocations"][service]
                != starts[1]["data"]["service_invocations"][service]
                for service in ("docker.service", "adp-job-firewall.service"))
            and starts[0]["observed_at_unix_ns"]
            < starts[1]["data"]["host_enforcement"]["denial_installed_at_unix_ns"]
            and starts[1]["data"]["host_enforcement"]["denial_installed_at_unix_ns"] // 1000
            == timestamp_ns(restart["denial_installed_at"]) // 1000, "daemon_restart_not_observed")
    failed = evidence["bootstrap_failure"]
    require(failed.get("instance_id") in indexed and failed["instance_id"] not in cold_instances | {restart["instance_id"]},
            "failed_bootstrap_host_missing")
    records = indexed[failed["instance_id"]]["records"]
    require(any(record["kind"] == "heartbeat" and record["data"].get("executor_ready") is False
                and start <= datetime.fromtimestamp(record["observed_at_unix_ns"] / 10**9, timezone.utc) <= end
                for record in records)
            and not any(record["kind"] in {"container_event", "container_observation"}
                        or record["data"].get("executor_ready") is True for record in records),
            "failed_bootstrap_host_unproven")


def verify_executor_host_histories(fixtures, evidence, hosts, negatives):
    histories = {history["scope"]["instance"]["instance_id"]: history
                 for history in evidence["host_histories"]}
    reserved = {row["instance_id"] for row in evidence["cold_starts"] + [evidence["daemon_restart"],
                                                                           evidence["bootstrap_failure"]]}
    reserved.add(evidence["gitlab"]["a"]["instance_id"])
    seen = set()
    for name, row in negatives.items():
        if row["outcome"] == "refused_before_script":
            continue
        instance_id = row.get("instance_id")
        require(instance_id in histories and instance_id not in reserved | seen
                and instance_id in {host["instance_id"] for host in hosts}
                and type(row.get("pipeline_id")) is int and row["pipeline_id"] > 0,
                "executor_host_scope_wrong")
        seen.add(instance_id)
        records = histories[instance_id]["records"]
        events = [record for record in records if record["kind"] == "container_event"
                  and record["data"].get("action") == "start"]
        observations = [record for record in records if record["kind"] == "container_observation"]
        require(len(events) == len(observations) == 1
                and all(record["data"].get("job_id") == row["gitlab_job_id"]
                        for record in records if record["kind"] in {"container_event", "container_observation"})
                and all(record["data"].get("project_id") == fixtures["project_a"]
                        and record["data"].get("pipeline_id") == row["pipeline_id"]
                        and record["data"].get("source_revision") == fixtures["source_revision"]
                        for record in events + observations)
                and events[0]["data"].get("container_id") == observations[0]["data"].get("container_id")
                and events[0]["observed_at_unix_ns"] <= observations[0]["observed_at_unix_ns"],
                "executor_host_job_unverified")
        host = observations[0]["data"]
        kernel = host.get("kernel", {})
        namespaces = kernel.get("namespaces", {})
        require(host.get("source") == "trusted_host_docker_and_kernel"
                and host.get("kind") == "build"
                and re.fullmatch(r"sha256:[0-9a-f]{64}", host.get("image_id", ""))
                and row["uid"] == 10001 and host.get("configured_user") == "10001:10001"
                and host.get("privileged") is False and host.get("cap_add") == []
                and "ALL" in host.get("cap_drop", [])
                and "no-new-privileges:true" in host.get("security_options", [])
                and host.get("network_mode") not in {None, "host"}
                and host.get("pid_mode") not in {"host", None}
                and host.get("ipc_mode") not in {"host", None}
                and host.get("devices") == [] and host.get("device_requests_present") is False
                and isinstance(host.get("mounts"), list)
                and all(mount.get("Type") == "volume" and mount.get("Destination") in {"/builds", "/cache"}
                        for mount in host["mounts"])
                and kernel.get("uids") == [row["uid"]] * 4
                and kernel.get("effective_capabilities_hex") == "0" * 16
                and kernel.get("no_new_privileges") is True
                and all(namespaces.get(kind, {}).get("same_as_host") is False
                        for kind in ("net", "pid", "ipc", "mnt"))
                and host.get("host_enforcement", {}).get("status") == "verified",
                "executor_host_confinement_unverified")


def verify_ec04(fixtures, evidence, start, end, *, final):
    stages = ("absent", "present", "after_teardown") if final else ("absent", "present")
    plans = evidence.get("instance_plan_receipts")
    require(isinstance(plans, list) and [row.get("stage") for row in plans] == list(stages),
            "instance_plan_receipts_missing")
    previous = start
    for row in plans:
        began, finished = timestamp(row["started_at"]), timestamp(row["completed_at"])
        require(row.get("run_id") == fixtures["run_id"] and row.get("account_id") == fixtures["account_id"]
                and row.get("source_revision") == fixtures["source_revision"]
                and row.get("working_directory") == "modules/source-control/gitlab/infra"
                and row.get("exit_code") == 0
                and all(isinstance(row.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", row[key])
                        for key in ("backend_sha256", "variables_sha256", "output_sha256"))
                and previous <= began <= finished <= end, "instance_plan_receipt_invalid")
        previous = finished
    require(len({row["backend_sha256"] for row in plans}) == 1
            and len({row["variables_sha256"] for row in plans}) == 1,
            "instance_plan_inputs_changed")
    if final:
        teardown = evidence.get("fixture_teardown")
        require(isinstance(teardown, dict)
                and teardown.get("run_id") == fixtures["run_id"]
                and teardown.get("account_id") == fixtures["account_id"]
                and teardown.get("source_revision") == fixtures["source_revision"]
                and teardown.get("owner_parent_group_id") == 106
                and 106 not in (fixtures["group_a"], fixtures["group_b"])
                and isinstance(teardown.get("backend_key"), str)
                and re.fullmatch(r"dev/[a-z0-9/-]+/terraform\.tfstate", teardown["backend_key"])
                and fixtures["run_id"] in teardown["backend_key"].split("/")
                and ".." not in teardown["backend_key"].split("/")
                and isinstance(teardown.get("backend_sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", teardown["backend_sha256"])
                and teardown["backend_sha256"] != plans[0]["backend_sha256"]
                and isinstance(teardown.get("runner_backend_sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", teardown["runner_backend_sha256"])
                and teardown["backend_sha256"] != teardown["runner_backend_sha256"]
                and isinstance(teardown.get("owned_resource_ids_before"), list)
                and teardown["owned_resource_ids_before"]
                and len(set(teardown["owned_resource_ids_before"])) == len(teardown["owned_resource_ids_before"])
                and teardown.get("owned_resource_ids_after") == []
                and teardown.get("post_destroy_plan_exit_code") == 0
                and isinstance(teardown.get("provider_request_ids"), list)
                and teardown["provider_request_ids"]
                and all(re.fullmatch(r"[A-Za-z0-9-]{1,128}", value)
                        for value in teardown["provider_request_ids"])
                and timestamp(plans[1]["completed_at"]) <= timestamp(teardown["completed_at"])
                <= timestamp(plans[2]["started_at"]),
                "fixture_teardown_unproven")
    scan = evidence.get("secret_scan")
    require(isinstance(scan, dict) and scan.get("run_id") == fixtures["run_id"]
            and scan.get("source_revision") == fixtures["source_revision"]
            and all(scan.get(key) is True for key in ("full_logs_scanned", "state_scanned", "artifacts_scanned"))
            and scan.get("canary_hits") == scan.get("ca_pem_hits") == 0
            and type(scan.get("state_count")) is int and scan["state_count"] > 0
            and type(scan.get("artifact_count")) is int and scan["artifact_count"] > 0,
            "secret_safety_unproven")
    runs = scan.get("workflow_runs")
    require(isinstance(runs, list) and {row.get("workflow") for row in runs} ==
            {"gitlab-infra-apply.yml", "gitlab-runners-apply.yml"}
            and all(type(row.get("run_id")) is int and row["run_id"] > 0
                    and ec04_audit.workflow_run_matches(fixtures, row)
                    and row.get("conclusion") in {"success", "failure", "cancelled"}
                    and isinstance(row.get("job_ids"), list) and row["job_ids"]
                    and all(type(job) is int and job > 0 for job in row["job_ids"]) for row in runs)
            and len({row["run_id"] for row in runs}) == len(runs), "secret_scan_run_wrong")
    job_ids = [job for row in runs for job in row["job_ids"]]
    digests = scan.get("sha256s")
    require(scan.get("log_job_ids") == job_ids and len(set(job_ids)) == len(job_ids)
            and isinstance(digests, list) and len(digests) == len(job_ids) + scan["state_count"] + scan["artifact_count"]
            and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in digests),
            "secret_scan_coverage_missing")


def private_json(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600, "private_operator_file_required")
    return json.loads(path.read_text())


def live_executor_jobs(fixtures, evidence, gitlab):
    """Each claimed executor attempt must be an actual job in this owned run.

    The qualification pipeline uses fixed executor-<case> job names from the
    reviewed source and an ADP_QUALIFICATION_RUN_ID pipeline variable. Provider
    readbacks bind reports to that pipeline; one successful job cannot stand in
    for missing overrides. Trusted host inspection is still separately required.
    """
    project = fixtures["project_a"]
    start, end = timestamp(evidence["started_at"]), timestamp(evidence["completed_at"])
    pipelines = set()
    helper_image = None
    for row in evidence["executor_negatives"]:
        job = gitlab(f"projects/{project}/jobs/{row['gitlab_job_id']}")
        require(isinstance(job, dict) and job.get("id") == row["gitlab_job_id"]
                and job.get("name") == f"executor-{row['name']}"
                and job.get("commit", {}).get("id") == fixtures["source_revision"]
                and (job.get("runner") or {}).get("id") == fixtures["runner_a"],
                "live_executor_job_scope_wrong")
        expected = "success" if row["outcome"] == "constrained" else "failed"
        require(job.get("status") == expected, "live_executor_job_outcome_wrong")
        if row["outcome"] == "refused_before_script":
            trace = gitlab(f"projects/{project}/jobs/{row['gitlab_job_id']}/trace")
            reason = REFUSAL_TRACES.get(row["name"])
            require(reason is not None and job.get("failure_reason") == "runner_system_failure"
                    and isinstance(trace, str) and len(trace) <= 131072
                    and (match := reason.search(trace))
                    and 'Executing "step_script" stage' not in trace, "live_executor_refusal_trace_wrong")
        require(start <= timestamp(job["created_at"]) <= timestamp(job["finished_at"]) <= end,
                "live_executor_job_outside_run")
        pipeline_id = job.get("pipeline", {}).get("id")
        require(type(pipeline_id) is int and pipeline_id > 0, "live_executor_pipeline_missing")
        if row["outcome"] == "constrained":
            require(row.get("pipeline_id") == pipeline_id, "live_executor_host_pipeline_wrong")
            artifact = gitlab(f"projects/{project}/jobs/{row['gitlab_job_id']}/artifacts/executor-result.json")
            require(isinstance(artifact, dict) and artifact == row.get("probe"),
                    "live_executor_probe_artifact_wrong")
            if row["name"] == "debug_trace":
                trace = gitlab(f"projects/{project}/jobs/{row['gitlab_job_id']}/trace")
                require(isinstance(trace, str) and len(trace) <= 131072
                        and "Executor attempt recorded; independent host and provider verification required" in trace
                        and not re.search(r"(?m)^\+{1,2} ", trace), "live_debug_trace_not_disabled")
        if pipeline_id not in pipelines:
            pipeline = gitlab(f"projects/{project}/pipelines/{pipeline_id}")
            require(isinstance(pipeline, dict) and pipeline.get("source") == "api"
                    and pipeline.get("sha") == fixtures["source_revision"]
                    and pipeline.get("ref") == "qualification-" + fixtures["run_id"], "live_executor_pipeline_scope_wrong")
            variables = gitlab(f"projects/{project}/pipelines/{pipeline_id}/variables")
            require(isinstance(variables, list), "live_executor_run_binding_missing")
            bindings = [item.get("value") for item in variables if item.get("key") == "ADP_QUALIFICATION_RUN_ID"]
            require(bindings == [fixtures["run_id"]], "live_executor_run_binding_wrong")
            images = [item.get("value") for item in variables if item.get("key") == "ADP_QUALIFICATION_HELPER_IMAGE"]
            require(len(images) == 1 and isinstance(images[0], str) and images[0], "live_executor_image_binding_wrong")
            helper_image = images[0]
            pipelines.add(pipeline_id)
        if row["outcome"] == "refused_before_script" and row["name"] == "arbitrary_image":
            require(helper_image == match.group("image"), "live_executor_refusal_trace_wrong")
    require(len(pipelines) == 1, "live_executor_pipelines_not_unique")
    return next(iter(pipelines))


def live_network_job(fixtures, evidence, gitlab, pipeline_id):
    project, job_id = fixtures["project_a"], evidence["network_job_id"]
    start, end = timestamp(evidence["started_at"]), timestamp(evidence["completed_at"])
    job = gitlab(f"projects/{project}/jobs/{job_id}")
    require(isinstance(job, dict) and job.get("id") == job_id and job.get("name") == "network-isolation"
            and job.get("commit", {}).get("id") == fixtures["source_revision"]
            and (job.get("runner") or {}).get("id") == fixtures["runner_a"]
            and job.get("pipeline", {}).get("id") == pipeline_id and job.get("status") == "success",
            "live_network_job_scope_wrong")
    require(start <= timestamp(job["created_at"]) <= timestamp(job["finished_at"]) <= end,
            "live_network_job_outside_run")
    if ipv6_metadata.enabled(fixtures):
        observations = [record["data"] for history in evidence["host_histories"] for record in history["records"]
                        if record["kind"] == "container_observation" and record["data"].get("job_id") == job_id]
        require(observations and all(row.get("pipeline_id") == pipeline_id and row.get("project_id") == project
                                    and row.get("source_revision") == fixtures["source_revision"] for row in observations),
                "live_ipv6_network_host_job_wrong")
    variables = gitlab(f"projects/{project}/pipelines/{pipeline_id}/variables")
    require(isinstance(variables, list), "live_network_target_binding_wrong")
    targets = [row for row in variables if row.get("key") == "ADP_QUALIFICATION_TARGETS_FILE"]
    require(len(targets) == 1 and targets[0].get("variable_type") == "file", "live_network_target_binding_wrong")
    try:
        payload = json.loads(targets[0]["value"])
    except (ValueError, KeyError, TypeError) as error:
        raise IncompleteEvidence("live_network_target_binding_wrong") from error
    require(payload == {"run_id": fixtures["run_id"], "targets": fixtures["network_targets"]},
            "live_network_target_binding_wrong")
    artifact = gitlab(f"projects/{project}/jobs/{job_id}/artifacts/network-result.json")
    require(isinstance(artifact, dict) and artifact == {"run_id": fixtures["run_id"], "probes": evidence["probes"]},
            "live_network_artifact_wrong")


def live_lifecycle_jobs(fixtures, evidence, aws, gitlab):
    start, end = timestamp(evidence["started_at"]), timestamp(evidence["completed_at"])
    attempts = evidence["cold_starts"] + [evidence["daemon_restart"]]
    job_ids = [row["job_id"] for row in attempts]
    failed = evidence["bootstrap_failure"]
    require(type(failed.get("job_id")) is int and failed["job_id"] not in job_ids
            and len(set(job_ids)) == len(job_ids), "lifecycle_job_reused")
    for row in attempts:
        job = gitlab(f"projects/{fixtures['project_a']}/jobs/{row['job_id']}")
        require(isinstance(job, dict) and job.get("id") == row["job_id"]
                and job.get("commit", {}).get("id") == fixtures["source_revision"]
                and (job.get("runner") or {}).get("id") == fixtures["runner_a"]
                and job.get("pipeline", {}).get("id") == row["pipeline_id"]
                and job.get("status") == "success" and job.get("name") == "network-isolation"
                and start <= timestamp(job["created_at"]) <= timestamp(job["finished_at"]) <= end,
                "lifecycle_provider_job_wrong")
        require(timestamp(job["created_at"]) <= timestamp(row["first_instruction_at"]) <= timestamp(job["finished_at"]),
                "lifecycle_provider_instruction_time_wrong")
        pipeline = gitlab(f"projects/{fixtures['project_a']}/pipelines/{row['pipeline_id']}")
        require(isinstance(pipeline, dict) and pipeline.get("source") == "api"
                and pipeline.get("sha") == fixtures["source_revision"]
                and pipeline.get("ref") == "qualification-" + fixtures["run_id"], "lifecycle_pipeline_scope_wrong")
        variables = gitlab(f"projects/{fixtures['project_a']}/pipelines/{row['pipeline_id']}/variables")
        require(isinstance(variables, list)
                and [item.get("value") for item in variables if item.get("key") == "ADP_QUALIFICATION_RUN_ID"]
                == [fixtures["run_id"]], "lifecycle_run_binding_wrong")
        target_files = [item for item in variables if item.get("key") == "ADP_QUALIFICATION_TARGETS_FILE"]
        require(len(target_files) == 1 and target_files[0].get("variable_type") == "file",
                "lifecycle_targets_not_bound")
        try:
            target_payload = json.loads(target_files[0]["value"])
        except (ValueError, KeyError, TypeError) as error:
            raise IncompleteEvidence("lifecycle_targets_not_bound") from error
        require(target_payload == {"run_id": fixtures["run_id"], "targets": fixtures["network_targets"]},
                "lifecycle_targets_not_bound")
        artifact = gitlab(f"projects/{fixtures['project_a']}/jobs/{row['job_id']}/artifacts/network-result.json")
        require(isinstance(artifact, dict) and artifact.get("run_id") == fixtures["run_id"],
                "first_instruction_artifact_wrong")
        results = by_name(artifact.get("probes"))
        require(set(results) == NEGATIVE_TARGETS | POSITIVE_TARGETS
                and all(negative_probe_matches(name, results[name], by_name(fixtures["network_targets"]))
                        for name in NEGATIVE_TARGETS)
                and all(results[name].get("result") == "allowed" and results[name].get("transport_connected") is True
                        for name in POSITIVE_TARGETS), "first_instruction_denial_wrong")
    job = gitlab(f"projects/{fixtures['project_a']}/jobs/{failed['job_id']}")
    require(isinstance(job, dict) and job.get("id") == failed["job_id"]
            and job.get("commit", {}).get("id") == fixtures["source_revision"]
            and job.get("runner") is None and job.get("status") == "canceled"
            and start <= timestamp(job["created_at"]) <= timestamp(job["finished_at"]) <= end,
            "failed_bootstrap_provider_job_wrong")
    response = aws.client("ec2").describe_instances(InstanceIds=[failed["instance_id"]])
    instances = [instance for reservation in response["Reservations"] for instance in reservation["Instances"]]
    require(len(instances) == 1 and instances[0]["InstanceId"] == failed["instance_id"]
            and instances[0]["State"]["Name"] == "terminated", "failed_bootstrap_provider_instance_wrong")


def live_ec01_readback(fixtures, evidence, *, aws, gitlab):
    """Compare pre-teardown registration receipts with independent provider inventory."""
    registrations = evidence["ec01"]["registrations"]
    secrets = aws.client("secretsmanager").get_paginator("list_secrets").paginate(
        Filters=[{"Key": "name", "Values": ["adp/dev/gitlab-runner/"]}])
    # The independent fixture stack must coexist with production registrations.
    # Still inspect the complete provider inventory and reject an unexpected
    # token in either owned fixture group; unrelated groups are outside teardown.
    owned_names = {f"adp/dev/gitlab-runner/{fixtures[key]}" for key in ("group_a", "group_b")}
    names = [secret["Name"] for page in secrets for secret in page["SecretList"]
             if secret["Name"] in owned_names]
    require(len(names) == len(registrations)
            and set(names) == {row["token_secret_name"] for row in registrations}, "live_token_inventory_wrong")
    for row in registrations:
        runner = gitlab(f'runners/{row["runner_id"]}')
        require(runner and runner.get("id") == row["runner_id"] and runner.get("runner_type") == "group_type"
                and runner.get("locked") is True and runner.get("run_untagged") is False
                and [group.get("id") for group in runner.get("groups", [])] == [row["group_id"]],
                "live_runner_scope_wrong")
    instance_runners = []
    for page in range(1, 21):
        batch = gitlab(f"runners/all?type=instance_type&per_page=100&page={page}")
        require(isinstance(batch, list) and len(batch) <= 100, "live_instance_runner_inventory_invalid")
        instance_runners.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise IncompleteEvidence("live_instance_runner_inventory_incomplete")
    require(all(isinstance(runner, dict) and type(runner.get("id")) is int for runner in instance_runners),
            "live_instance_runner_inventory_invalid")
    require({runner["id"] for runner in instance_runners} == set(evidence["ec01"]["instance_runner_ids"]),
            "live_instance_runner_scope_wrong")


def live_ec02_readback(fixtures, evidence, gitlab):
    """Read actual product trace, pending control and cleanup jobs from GitLab."""
    start, end = timestamp(evidence["started_at"]), timestamp(evidence["completed_at"])
    for label, status in (("a", "success"), ("b", "canceled")):
        row = evidence["gitlab"][label]
        project, pipeline_id = row["project_id"], row["pipeline_id"]
        job = gitlab(f"projects/{project}/jobs/{row['job_id']}")
        require(isinstance(job, dict) and job.get("id") == row["job_id"]
                and job.get("status") == status and job.get("commit", {}).get("id") == fixtures["source_revision"]
                and job.get("pipeline", {}).get("id") == pipeline_id
                and job.get("tag_list") == [f'adp-group-{fixtures["group_a"]}']
                and (job.get("runner") or {}).get("id") == (fixtures["runner_a"] if label == "a" else None),
                "live_product_or_pending_job_wrong")
        pipeline = gitlab(f"projects/{project}/pipelines/{pipeline_id}")
        require(isinstance(pipeline, dict) and pipeline.get("source") == "api"
                and pipeline.get("sha") == fixtures["source_revision"]
                and pipeline.get("ref") == "qualification-" + fixtures["run_id"]
                and pipeline.get("status") == status, "live_product_pipeline_wrong")
        variables = gitlab(f"projects/{project}/pipelines/{pipeline_id}/variables")
        require(isinstance(variables, list) and
                [item.get("value") for item in variables if item.get("key") == "ADP_QUALIFICATION_RUN_ID"]
                == [fixtures["run_id"]], "live_product_pipeline_binding_wrong")
        require(start <= timestamp(job["created_at"]) <= timestamp(job["finished_at"]) <= end,
                "live_product_job_outside_run")
        if label == "a":
            trace = gitlab(f"projects/{project}/jobs/{row['job_id']}/trace")
            require(isinstance(trace, str) and row["runner_trace_line"] in trace.splitlines(),
                    "live_product_runner_trace_missing")
    for row in evidence["cleanup"]:
        job = gitlab(f"projects/{fixtures['project_a']}/jobs/{row['job_id']}")
        require(isinstance(job, dict) and job.get("id") == row["job_id"]
                and job.get("status") == row["job_status"]
                and job.get("commit", {}).get("id") == fixtures["source_revision"]
                and job.get("pipeline", {}).get("id") == row["pipeline_id"]
                and (job.get("runner") or {}).get("id") == fixtures["runner_a"]
                and timestamp(job["finished_at"]) == timestamp(row["job_finished_at"]),
                "live_cleanup_job_wrong")


def project_scope(fixtures, label, project, gitlab, reason):
    namespace = project.get("namespace", {}) if isinstance(project, dict) else {}
    namespace_id = fixtures.get(f"project_namespace_{label}")
    root_id = fixtures[f"group_{label}"]
    other_root = fixtures["group_b" if label == "a" else "group_a"]
    require(type(namespace_id) is int and namespace_id > 0
            and isinstance(project, dict) and project.get("id") == fixtures[f"project_{label}"]
            and namespace.get("id") == namespace_id and namespace.get("kind") == "group", reason)
    visited = set()
    while namespace_id != root_id:
        require(type(namespace_id) is int and namespace_id > 0 and namespace_id != other_root
                and namespace_id not in visited and len(visited) < 32, reason)
        visited.add(namespace_id)
        group = gitlab(f"groups/{namespace_id}")
        require(isinstance(group, dict) and group.get("id") == namespace_id
                and type(group.get("parent_id")) is int and group["parent_id"] > 0, reason)
        namespace_id = group["parent_id"]


def live_readback(fixtures, evidence, *, aws, gitlab, manager, final=True):
    """Cross-check retained records against fresh independent provider clients."""
    require(aws.client("sts").get_caller_identity()["Account"] == fixtures["account_id"], "live_wrong_account")
    if not final:
        live_ec01_readback(fixtures, evidence, aws=aws, gitlab=gitlab)
    for label in ("a", "b"):
        row = evidence["gitlab"][label]
        project = gitlab(f"projects/{row['project_id']}")
        project_scope(fixtures, label, project, gitlab, "live_wrong_group")
        job = gitlab(f"projects/{row['project_id']}/jobs/{row['job_id']}")
        require(job["commit"]["id"] == fixtures["source_revision"], "live_job_source_wrong")
        expected = "success" if label == "a" else "canceled"
        require(job["status"] == expected, "live_job_status_wrong")
        if label == "a":
            require(job["runner"]["id"] == fixtures["runner_a"], "live_wrong_runner")
        else:
            require(job.get("runner") is None, "live_group_b_executed")
    live_network_job(fixtures, evidence, gitlab, live_executor_jobs(fixtures, evidence, gitlab))
    runner = gitlab(f"runners/{fixtures['runner_a']}")
    if final:
        require(runner is None, "live_runner_not_revoked")
    else:
        require(runner and runner["runner_type"] == "group_type" and runner["locked"] is True
                and runner["run_untagged"] is False
                and {group["id"] for group in runner["groups"]} == {fixtures["group_a"]}, "live_runner_scope_wrong")
    host_ids = [host["instance_id"] for host in evidence["hosts"]]
    described = aws.client("ec2").describe_instances(InstanceIds=host_ids)
    instances = [instance for reservation in described["Reservations"] for instance in reservation["Instances"]]
    require({instance["InstanceId"] for instance in instances} == set(host_ids), "live_host_inventory_missing")
    for instance in instances:
        require(instance["VpcId"] == fixtures["vpc_id"]
                and instance["SubnetId"] in fixtures["private_subnet_ids"]
                and instance["ImageId"] == fixtures["ami_id"]
                and {sg["GroupId"] for sg in instance["SecurityGroups"]} == set(fixtures["job_security_group_ids"])
                and not instance.get("PublicIpAddress"), "live_host_scope_wrong")
        require(not instance.get("IamInstanceProfile"), "live_job_instance_profile_forbidden")
        require(not any(interface.get("Ipv6Addresses") for interface in instance.get("NetworkInterfaces", [])), "live_host_ipv6_not_qualified")
        if ipv6_metadata.enabled(fixtures):
            try:
                receipts = [snapshot["ipv6_metadata"] for history in evidence["host_histories"]
                            if history["scope"]["instance"]["instance_id"] == instance["InstanceId"]
                            for snapshot in history["snapshots"] if "ipv6_metadata" in snapshot]
                require(receipts, "live_ipv6_provider_binding_changed")
                for receipt in receipts:
                    ipv6_metadata.current_instance_matches(instance, receipt["instance"])
                if not final:
                    for receipt in receipts:
                        template = receipt["launch_template"]
                        response = aws.client("ec2").describe_launch_template_versions(
                            LaunchTemplateId=template["id"], Versions=[str(template["version"])])
                        rows = response["LaunchTemplateVersions"]
                        require(len(rows) == 1 and rows[0]["LaunchTemplateId"] == template["id"]
                                and rows[0]["VersionNumber"] == template["version"]
                                and rows[0]["LaunchTemplateData"]["MetadataOptions"]["HttpProtocolIpv6"] == "disabled"
                                and not rows[0]["LaunchTemplateData"].get("IamInstanceProfile"),
                                "live_ipv6_template_changed")
            except (KeyError, TypeError, ValueError) as error:
                raise IncompleteEvidence("live_ipv6_provider_invalid") from error
    inventory = aws.client("autoscaling").describe_auto_scaling_groups(AutoScalingGroupNames=fixtures["asg_names"])
    asgs = inventory["AutoScalingGroups"]
    if final:
        require(asgs == [], "live_asg_not_removed")
    else:
        require({asg["AutoScalingGroupName"] for asg in asgs} == set(fixtures["asg_names"])
                and all(asg["DesiredCapacity"] == 0 and asg["Instances"] == [] for asg in asgs), "live_asg_not_zero")
    paginator = aws.client("ec2").get_paginator("describe_instances")
    for page in paginator.paginate(Filters=[
        # Independent ownership tags also find detached ASG orphans.
        {"Name": "tag:Component", "Values": ["gitlab-job"]},
        {"Name": "tag:Environment", "Values": ["dev"]},
        {"Name": "tag:GroupId", "Values": [str(fixtures["group_a"]), str(fixtures["group_b"])]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped", "shutting-down"]},
    ]):
        require(not any(row["Instances"] for row in page["Reservations"]), "live_orphan_instance_remaining")
    deployment = manager()
    if final:
        require(deployment is None, "live_manager_not_removed")
    else:
        require(deployment and deployment["spec"]["replicas"] == 1 and deployment["status"].get("readyReplicas") == 1
                and deployment["status"].get("observedGeneration", 0) >= deployment["metadata"]["generation"], "live_manager_not_ready")
    live_lifecycle_jobs(fixtures, evidence, aws, gitlab)
    live_ec02_readback(fixtures, evidence, gitlab)


def live_clients(args, fixtures):
    import boto3

    session = boto3.Session(profile_name=args.aws_profile, region_name=args.region)
    identity = session.client("sts").get_caller_identity()
    role = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/([A-Za-z0-9+=,.@_/-]+)", args.expected_role)
    require(role and role[1] == fixtures["account_id"] and identity["Account"] == role[1]
            and re.fullmatch(rf"arn:aws:sts::{role[1]}:assumed-role/{re.escape(role[2].split('/')[-1])}/[^/]+", identity["Arn"]),
            "live_operator_identity_wrong")
    url = urlsplit(args.gitlab_url)
    require(url.scheme == "https" and url.hostname and url.port in (None, 443)
            and not url.username and not url.password and not url.query and not url.fragment
            and url.path in ("", "/gitlab"), "live_gitlab_url_invalid")
    discovered = session.client("ssm").get_parameter(Name="/adp/dev/gitlab/runner-url", WithDecryption=False)["Parameter"]["Value"]
    require(args.gitlab_url == discovered, "live_gitlab_url_not_discovered")
    require(re.fullmatch(rf"arn:aws:secretsmanager:{re.escape(args.region)}:{fixtures['account_id']}:secret:adp/dev/gitlab-admin-token-[A-Za-z0-9]{{6}}",
                         args.admin_secret_arn), "live_admin_secret_scope_wrong")
    ca = ssl.create_default_context(cafile=str(args.ca_file))
    token = session.client("secretsmanager").get_secret_value(SecretId=args.admin_secret_arn)["SecretString"]
    require(isinstance(token, str) and token.startswith("glpat-"), "live_admin_credential_invalid")

    def gitlab(path):
        connection = http.client.HTTPSConnection(url.hostname, context=ca, timeout=10)
        try:
            connection.request("GET", f"{url.path}/api/v4/{path}", headers={"PRIVATE-TOKEN": token})
            response = connection.getresponse()
            if response.status == 404 and re.fullmatch(r"runners/[1-9][0-9]*", path):
                return None
            require(response.status == 200, "live_gitlab_read_failed")
            if path.endswith("/trace"):
                trace = response.read(131073)
                require(len(trace) <= 131072, "live_executor_trace_too_large")
                return trace.decode("utf-8", errors="replace")
            if path.endswith(("/artifacts/network-result.json", "/artifacts/executor-result.json")):
                artifact = response.read(32769)
                require(len(artifact) <= 32768, "live_probe_artifact_too_large")
                return json.loads(artifact)
            return json.loads(response.read())
        finally:
            connection.close()

    def manager():
        cluster = session.client("eks").describe_cluster(name=args.cluster)["cluster"]
        require(cluster["arn"].split(":")[4] == fixtures["account_id"], "live_cluster_account_wrong")
        require(cluster["resourcesVpcConfig"]["vpcId"] == fixtures["vpc_id"], "live_jobs_outside_eks_vpc")
        command = ["kubectl", "--kubeconfig", str(args.kubeconfig)]
        env = {**os.environ, "AWS_REGION": args.region}
        if args.aws_profile:
            env["AWS_PROFILE"] = args.aws_profile
        config = json.loads(subprocess.run(command + ["config", "view", "--minify", "-o", "json"],
                            check=True, capture_output=True, timeout=20, env=env).stdout)
        require(config["clusters"][0]["cluster"]["server"] == cluster["endpoint"], "live_kubernetes_context_wrong")
        output = subprocess.run(command + ["get", "deployment", args.manager_deployment,
                          "-n", f"gitlab-runner-{fixtures['group_a']}", "--ignore-not-found", "-o", "json"],
                          check=True, capture_output=True, timeout=20, env=env).stdout
        return json.loads(output) if output.strip() else None
    return session, gitlab, manager


def verify_release_binding(evidence, *, private_lock=None, approved_sha256=None):
    require(bool(private_lock) and bool(approved_sha256), "release_lock_binding_missing")
    private_json(private_lock)
    lock = Path(private_lock)
    require(re.fullmatch(r"[0-9a-f]{64}", approved_sha256) is not None,
            "release_lock_binding_invalid")
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    require(digest == evidence.get("release_lock_sha256") and digest == approved_sha256,
            "release_lock_mismatch")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--host-history", type=Path, action="append", required=True)
    parser.add_argument("--aws-profile")
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-role", required=True)
    parser.add_argument("--gitlab-url", required=True)
    parser.add_argument("--admin-secret-arn", required=True)
    parser.add_argument("--ca-file", type=Path, required=True)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--manager-deployment", required=True)
    parser.add_argument("--release-lock-file", type=Path, required=True)
    parser.add_argument("--release-lock-sha256", required=True)
    parser.add_argument("--stage", choices=("pre-teardown", "final"), required=True)
    args = parser.parse_args()
    try:
        fixtures, evidence = private_json(args.fixtures), private_json(args.evidence)
        import host_journal
        histories = [private_json(path) for path in args.host_history]
        require(all(history.get("scope", {}).get("observer_sha256") == host_journal.source_hash()
                    for history in histories), "host_observer_source_wrong")
        evidence["host_histories"] = histories
        verify_release_binding(evidence, private_lock=args.release_lock_file,
                               approved_sha256=args.release_lock_sha256)
        result = verify(fixtures, evidence, final=args.stage == "final")
        aws, gitlab, manager = live_clients(args, fixtures)
        live_readback(fixtures, evidence, aws=aws, gitlab=gitlab, manager=manager, final=args.stage == "final")
        result["fresh_independent_api_readbacks"] = "passed"
        if args.stage == "final":
            spec = importlib.util.spec_from_file_location("qualification_delivery", ROOT.parents[1] / "scripts/qualification_delivery.py")
            # The IDs are published only after every evidence oracle and fresh
            # provider readback above has passed; partial suites never reach here.
            import sys
            sys.path.insert(0, str(ROOT.parents[1] / "scripts"))
            delivery = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(delivery)
            result["checks"] = dict.fromkeys(sorted(delivery.LIVE_CHECKS), "passed")
    except IncompleteEvidence as error:
        print(json.dumps({"verdict": "incomplete", "reason": str(error)}))
        return 1
    except Exception:
        # Provider exceptions may include credentials, URLs and raw responses.
        print(json.dumps({"verdict": "incomplete", "reason": "required independent evidence missing or invalid"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
