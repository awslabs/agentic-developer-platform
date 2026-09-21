"""Verify CLI parent/child identity, evidence joins and the accepted shared bounds."""

import hashlib
from datetime import UTC
from decimal import Decimal

from .cli_live_contract import CaseResult, QualificationManifest, SuiteReport
from .repository_evaluation_contract import canonical
from .repository_evaluation_provider import parse_document, require, timestamp


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def aware(*values):
    require(all(value.tzinfo is not None for value in values), "cli_timestamp_unverifiable")


def file_evidence(reference, files):
    data = files.get(reference.path)
    require(data is not None and hashlib.sha256(data).hexdigest() == reference.sha256, "cli_evidence_file_missing_or_changed")
    return data


def structured_evidence(record, files):
    # Structured identity/guard/cleanup snapshots are independently archived,
    # not optional booleans in a parent summary. Their producer is source-pinned.
    actual = parse_document(file_evidence(record.evidence, files))
    require(isinstance(actual, dict) and "evidence" not in actual, "cli_structured_evidence_changed")
    require(type(record).model_validate({**actual, "evidence": record.evidence}) == record, "cli_structured_evidence_changed")


def runtime(record, expected, files, started, completed):
    require(record is not None, "cli_ec2_runtime_missing")
    aware(record.observed_at, record.instance_launched_at)
    require(
        record.instance_launched_at <= record.observed_at
        and record.deployment == expected.deployment
        and record.gateway_before == record.gateway_after == expected.deployment.gateway_revision
        and started <= record.observed_at <= completed,
        "cli_target_revision_release_or_identity_changed",
    )
    structured_evidence(record, files)


def cleanup(record, files, started, completed, *, instance=None, resources=(), configuration=()):
    aware(record.observed_at)
    require(
        record.complete
        and record.configuration_restored
        and not record.unresolved_operations
        and "unresolved" not in record.resources.values()
        and started <= record.observed_at <= completed
        and set(resources) <= set(record.resources)
        and set(configuration) <= set(record.restored_configuration),
        "cli_cleanup_or_restoration_unverified",
    )
    if instance:
        require(record.resources.get(instance) == "terminated", "cli_ec2_termination_unverified")
    structured_evidence(record, files)


def validate_qualification(expected, *, run, jobs, archives, correlation, now):
    """Archives are downloaded through the authenticated, run-bound provider API."""
    document, files = archives[(expected.manifest_artifact, expected.manifest_path)]
    manifest = QualificationManifest.model_validate(document)
    identity = dict(
        repository_id=run["repository"]["id"],
        run_id=run["id"],
        run_attempt=run["run_attempt"],
        workflow_revision=run["head_sha"],
        requirements_sha256=expected.requirements_sha256,
        qualification_sha256=digest(expected.model_dump(mode="json")),
        correlation=correlation,
    )
    require(all(getattr(manifest, key) == value for key, value in identity.items()), "cli_manifest_scope_changed")
    aware(manifest.started_at, manifest.completed_at)
    require(
        timestamp(run["run_started_at"]) <= manifest.started_at <= manifest.completed_at <= timestamp(run["updated_at"])
        and manifest.completed_at <= now
        and (manifest.completed_at - manifest.started_at).total_seconds() <= expected.bounds.max_duration_seconds,
        "cli_qualification_time_bound",
    )
    runtime(manifest.runtime, expected, files, manifest.started_at, manifest.completed_at)
    require(manifest.started_at <= manifest.runtime.instance_launched_at, "cli_instance_lifetime_changed")
    children = {child.suite_id: child for child in manifest.children}
    require(len(children) == len(manifest.children) == len(expected.suites), "cli_child_set_changed")
    guards = {guard.guard_id: guard for guard in manifest.guards}
    require(len(guards) == len(manifest.guards), "cli_guard_identity_ambiguous")
    request_total = input_total = output_total = 0
    spend = Decimal(0)
    last_spend = None
    operations, request_ids = {}, set()
    all_resources, all_configuration = set(), set()
    for guard in sorted(manifest.guards, key=lambda item: item.window_started_at):
        aware(guard.window_started_at, guard.window_completed_at)
        require(
            guard.limits == expected.bounds
            and manifest.started_at <= guard.window_started_at <= guard.window_completed_at <= manifest.completed_at
            and guard.day == guard.window_started_at.astimezone(UTC).date().isoformat()
            and guard.day == guard.window_completed_at.astimezone(UTC).date().isoformat()
            and guard.day == manifest.started_at.astimezone(UTC).date().isoformat()
            and guard.peak_instances <= 1
            and guard.max_observed_output_tokens_per_request <= expected.bounds.max_output_tokens_per_request
            and guard.daily_spend_before_usd <= guard.daily_spend_after_usd <= expected.bounds.max_daily_inference_usd
            and (last_spend is None or last_spend <= guard.daily_spend_before_usd),
            "cli_shared_guard_scope_or_bound_changed",
        )
        observed_requests = []
        for operation in guard.operations:
            aware(operation.started_at, operation.completed_at)
            require(
                operation.operation_ref not in operations
                and guard.window_started_at <= operation.started_at <= operation.completed_at <= guard.window_completed_at,
                "cli_guard_operation_changed",
            )
            operations[operation.operation_ref] = (guard.guard_id, operation)
            for request in operation.requests:
                aware(request.completed_at)
                require(
                    request.request_id not in request_ids
                    and operation.started_at <= request.completed_at <= operation.completed_at
                    and request.output_tokens <= expected.bounds.max_output_tokens_per_request,
                    "cli_guard_request_changed",
                )
                request_ids.add(request.request_id)
                observed_requests.append(request)
            all_resources.update(operation.created_resources)
            all_configuration.update(operation.changed_configuration)
        require(
            guard.requests == len(observed_requests)
            and guard.input_tokens == sum(item.input_tokens for item in observed_requests)
            and guard.output_tokens == sum(item.output_tokens for item in observed_requests)
            and guard.max_observed_output_tokens_per_request == max((item.output_tokens for item in observed_requests), default=0)
            and sum((item.cost_usd for item in observed_requests), Decimal(0)) <= guard.daily_spend_after_usd - guard.daily_spend_before_usd,
            "cli_guard_request_totals_changed",
        )
        structured_evidence(guard, files)
        last_spend = guard.daily_spend_after_usd
        spend += guard.daily_spend_after_usd - guard.daily_spend_before_usd
        request_total += guard.requests
        input_total += guard.input_tokens
        output_total += guard.output_tokens
    require(
        spend <= expected.bounds.max_daily_inference_usd
        and request_total <= expected.bounds.max_requests
        and input_total <= expected.bounds.max_input_tokens
        and output_total <= expected.bounds.max_output_tokens,
        "cli_aggregate_guard_bound_exceeded",
    )
    outcomes, used_guards, used_operations = {}, set(), set()
    child_completed = []
    for suite in expected.suites:
        child = children.get(suite.suite_id)
        payload, child_files = archives[(suite.artifact, suite.report_path)]
        require(
            child is not None
            and (child.artifact, child.report_path, child.sha256)
            == (suite.artifact, suite.report_path, hashlib.sha256(child_files[suite.report_path]).hexdigest()),
            "cli_child_artifact_changed",
        )
        report = SuiteReport.model_validate(payload)
        require(all(getattr(report, key) == value for key, value in identity.items()), "cli_child_scope_changed")
        matching_jobs = [job for job in jobs if job["name"] == suite.job_name]
        references = run.get("referenced_workflows", [])
        require(
            report.suite_id == suite.suite_id
            and len(matching_jobs) == 1
            and report.job_id == matching_jobs[0]["id"]
            and any(
                str(ref.get("path", "")).split("@", 1)[0].removeprefix(run["repository"].get("full_name", "") + "/") == suite.workflow_path
                and ref.get("sha") == suite.revision
                for ref in references
            ),
            "cli_child_workflow_or_job_changed",
        )
        aware(report.started_at, report.completed_at)
        require(
            manifest.started_at <= report.started_at <= report.completed_at <= manifest.completed_at,
            "cli_child_time_changed",
        )
        declared = {case.case_id: case for criterion in expected.criteria for case in criterion.cases if case.suite_id == suite.suite_id}
        actual = {case.case_id: case for case in report.cases}
        require(set(actual) == set(declared) and len(actual) == len(report.cases), "cli_case_coverage_changed")
        has_live = any(case.phase == "live" for case in declared.values())
        if has_live:
            runtime(report.runtime, expected, child_files, report.started_at, report.completed_at)
            require(
                report.runtime.instance_id == manifest.runtime.instance_id
                and report.runtime.instance_launched_at == manifest.runtime.instance_launched_at,
                "cli_shared_instance_changed",
            )
            require(report.guard_id in guards, "cli_independent_guard_missing")
            guard = guards[report.guard_id]
            require(guard.window_started_at <= report.started_at <= report.completed_at <= guard.window_completed_at, "cli_guard_window_changed")
            used_guards.add(report.guard_id)
        else:
            require(report.runtime is None and report.guard_id is None, "cli_offline_phase_changed")
        case_completed, suite_resources, suite_configuration = [], set(), set()
        for case_id, case in actual.items():
            aware(case.started_at, case.completed_at)
            require(report.started_at <= case.started_at <= case.completed_at <= report.completed_at, "cli_case_time_changed")
            require(
                case.phase != "live" or (report.runtime is not None and report.runtime.instance_launched_at <= case.started_at),
                "cli_case_before_instance_launch",
            )
            case_completed.append(case.completed_at)
            require(
                case.phase == declared[case_id].phase
                and case.actor_id == declared[case_id].actor_id
                and case.command == declared[case_id].command
                and canonical(case.expected) == canonical(declared[case_id].expected)
                and case.execution_path == ("served_cli" if case.phase == "live" else "offline_test")
                and (case.phase != "live" or bool(case.operation_refs)),
                "cli_case_execution_path_or_actor_changed",
            )
            for proof in case.evidence:
                file_evidence(proof, child_files)
            case_requests = []
            require(len(case.operation_refs) == len(set(case.operation_refs)), "cli_duplicate_case_operation")
            if case.phase == "pre":
                require(not case.operation_refs, "cli_offline_operation_changed")
            for reference in case.operation_refs:
                require(reference in operations and reference not in used_operations, "cli_case_operation_missing_or_reused")
                guard_id, operation = operations[reference]
                require(
                    guard_id == report.guard_id
                    and operation.suite_id == suite.suite_id
                    and operation.case_id == case_id
                    and operation.actor_id == case.actor_id
                    and operation.command == case.command
                    and case.started_at <= operation.started_at <= operation.completed_at <= case.completed_at,
                    "cli_case_guard_operation_changed",
                )
                used_operations.add(reference)
                case_requests.extend(operation.requests)
                suite_resources.update(operation.created_resources)
                suite_configuration.update(operation.changed_configuration)
            require(not declared[case_id].requires_inference or (case.phase == "live" and bool(case_requests)), "cli_required_inference_missing")
            command_record = parse_document(file_evidence(case.evidence[0], child_files))
            require(isinstance(command_record, dict) and "evidence" not in command_record, "cli_command_result_evidence_changed")
            recorded_case = CaseResult.model_validate({**command_record, "evidence": case.evidence})
            require(
                canonical(recorded_case.model_dump(mode="json")) == canonical(case.model_dump(mode="json")), "cli_command_result_evidence_changed"
            )
            outcomes[(suite.suite_id, case_id)] = case.status == "passed" and canonical(case.observed) == canonical(declared[case_id].expected)
        cleanup(report.cleanup, child_files, max(case_completed), report.completed_at, resources=suite_resources, configuration=suite_configuration)
        child_completed.append(report.completed_at)
    require(used_guards == set(guards) and used_operations == set(operations), "cli_unused_or_missing_shared_guard")
    cleanup(
        manifest.cleanup,
        files,
        max(child_completed),
        manifest.completed_at,
        instance=manifest.runtime.instance_id,
        resources=all_resources,
        configuration=all_configuration,
    )
    criteria = [
        dict(criterion_id=item.criterion_id, passed=all(outcomes[(case.suite_id, case.case_id)] for case in item.cases)) for item in expected.criteria
    ]
    return manifest.model_dump(mode="json"), criteria
