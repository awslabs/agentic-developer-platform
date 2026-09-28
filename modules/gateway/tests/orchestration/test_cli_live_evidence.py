"""Only complete authenticated CLI qualification artifacts can attest live acceptance."""

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.orchestration.cli_live_contract import REQUIRED_CRITERIA, CliLiveSpecification, Qualification
from src.orchestration.cli_live_evidence import digest, validate_qualification
from src.orchestration.repository_evaluation_contract import harness_digest
from src.orchestration.repository_evaluation_provider import RepositoryEvidenceProvider
from src.orchestration.review_cycle import CycleBlockedError


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def proof(record, files, path):
    data = encoded(record)
    files[path] = data
    record["evidence"] = dict(path=path, sha256=hashlib.sha256(data).hexdigest())
    return record


@pytest.fixture
def cli_evidence():
    started = datetime(2026, 9, 21, 12, tzinfo=UTC)
    ended = started + timedelta(minutes=5)

    def phase(key):
        return "pre" if key.startswith("5564/") or key in {f"5516/AC-{i:02}" for i in range(1, 5)} else "live"

    criteria = [
        dict(
            criterion_id=key,
            source_body_sha256="a" * 64,
            requirement_sha256="b" * 64,
            phases=[phase(key)],
            cases=[
                dict(
                    case_id=key,
                    suite_id=phase(key),
                    phase=phase(key),
                    actor_id="admin" if key == "5516/AC-05" else "member",
                    command="adp example --json",
                    expected="expected",
                    requires_inference=key == "5516/AC-05",
                )
            ],
        )
        for key in sorted(REQUIRED_CRITERIA)
    ]
    qualification = Qualification.model_validate(
        dict(
            owner_issue=5644,
            requirements_sha256="d" * 64,
            manifest_artifact="qualification-{run_attempt}",
            manifest_path="qualification.json",
            deployment=dict(
                account_id="123456789012",
                region="us-east-1",
                gateway_url="https://adp.example/api",
                deployment_id="dev",
                gateway_revision="b" * 40,
                gateway_image_digest="sha256:" + "e" * 64,
                served_release_sha256="f" * 64,
                installed_files={"adp": "f" * 64},
                worker_revisions={"worker": "b" * 40},
                tenant_id="tenant",
                ordinary_user_id="member",
                admin_user_id="admin",
            ),
            bounds=dict(
                shared_meter_ref="cli-shared-daily-meter",
                max_duration_seconds=3600,
                max_daily_inference_usd="5",
                max_requests=48,
                max_input_tokens=4096,
                max_output_tokens=2048,
                max_output_tokens_per_request=256,
            ),
            criteria=criteria,
            suites=[
                dict(
                    suite_id=key,
                    workflow_path=f".github/workflows/cli-{key}.yml",
                    revision="b" * 40,
                    job_name=f"{key} suite",
                    artifact=f"{key}-{{run_attempt}}",
                    report_path="report.json",
                )
                for key in ("pre", "live")
            ],
        )
    )
    identity = dict(
        repository_id=123,
        run_id=10,
        run_attempt=1,
        workflow_revision="b" * 40,
        requirements_sha256=qualification.requirements_sha256,
        qualification_sha256=digest(qualification.model_dump(mode="json")),
        correlation=None,
    )
    parent_files = {}
    runtime = dict(
        deployment=qualification.deployment.model_dump(mode="json"),
        client_kind="ec2",
        instance_id="i-0123456789abcdef0",
        instance_launched_at=started.isoformat(),
        gateway_before="b" * 40,
        gateway_after="b" * 40,
        observed_at=started.isoformat(),
    )
    cleanup = dict(
        complete=True,
        configuration_restored=True,
        restored_configuration=["test-setting"],
        unresolved_operations=[],
        resources={runtime["instance_id"]: "terminated", "test-resource": "deleted"},
        observed_at=ended.isoformat(),
    )
    guard = dict(
        limits=qualification.bounds.model_dump(mode="json"),
        guard_id="daily-guard",
        enforcement="independent_hard_guard",
        day="2026-09-21",
        window_started_at=started.isoformat(),
        window_completed_at=ended.isoformat(),
        daily_spend_before_usd="1",
        daily_spend_after_usd="2",
        requests=1,
        input_tokens=100,
        output_tokens=100,
        max_observed_output_tokens_per_request=100,
        operations=[],
        peak_instances=1,
    )
    manifest = dict(
        **identity,
        evidence_schema="cli-live-qualification/v1",
        partial=False,
        started_at=started.isoformat(),
        completed_at=ended.isoformat(),
        runtime=proof(copy.deepcopy(runtime), parent_files, "runtime.json"),
        guards=[proof(guard, parent_files, "guard.json")],
        cleanup=proof(copy.deepcopy(cleanup), parent_files, "cleanup.json"),
        children=[],
    )
    archives = {}
    for suite in qualification.suites:
        files = {}
        cases = []
        for item in qualification.criteria:
            if item.cases[0].suite_id != suite.suite_id:
                continue
            path = f"evidence/{len(cases)}.json"
            case = dict(
                case_id=item.criterion_id,
                phase=suite.suite_id,
                status="passed",
                execution_path="served_cli" if suite.suite_id == "live" else "offline_test",
                actor_id=item.cases[0].actor_id,
                command="adp example --json",
                expected="expected",
                observed="expected",
                operation_refs=[item.criterion_id] if suite.suite_id == "live" else [],
                started_at=(started + timedelta(seconds=1)).isoformat(),
                completed_at=(started + timedelta(seconds=2)).isoformat(),
            )
            evidence = encoded(case)
            files[path] = evidence
            case["evidence"] = [dict(path=path, sha256=hashlib.sha256(evidence).hexdigest())]
            cases.append(case)
            if suite.suite_id == "live":
                guard["operations"].append(
                    dict(
                        operation_ref=item.criterion_id,
                        suite_id=suite.suite_id,
                        case_id=item.criterion_id,
                        actor_id=case["actor_id"],
                        command=case["command"],
                        started_at=case["started_at"],
                        completed_at=case["completed_at"],
                        requests=[dict(request_id="request-1", input_tokens=100, output_tokens=100, cost_usd="1", completed_at=case["completed_at"])]
                        if item.cases[0].requires_inference
                        else [],
                        created_resources=["test-resource"],
                        changed_configuration=["test-setting"],
                    )
                )
        child = dict(
            **identity,
            evidence_schema="cli-live-suite/v1",
            suite_id=suite.suite_id,
            job_id=11 if suite.suite_id == "pre" else 12,
            runtime=proof(copy.deepcopy(runtime), files, "runtime.json") if suite.suite_id == "live" else None,
            started_at=started.isoformat(),
            completed_at=ended.isoformat(),
            cases=cases,
            guard_id="daily-guard" if suite.suite_id == "live" else None,
            cleanup=proof(copy.deepcopy(cleanup), files, "cleanup.json"),
        )
        files[suite.report_path] = encoded(child)
        archives[(suite.artifact, suite.report_path)] = child, files
        manifest["children"].append(
            dict(
                suite_id=suite.suite_id,
                artifact=suite.artifact,
                report_path=suite.report_path,
                sha256=hashlib.sha256(files[suite.report_path]).hexdigest(),
            )
        )
    guard.pop("evidence")
    proof(guard, parent_files, "guard.json")
    parent_files[qualification.manifest_path] = encoded(manifest)
    archives[(qualification.manifest_artifact, qualification.manifest_path)] = manifest, parent_files
    run = dict(
        repository=dict(id=123, full_name="o/r"),
        head_repository=dict(id=123),
        id=10,
        run_number=5,
        run_attempt=1,
        head_sha="b" * 40,
        path=".github/workflows/nightly-cli-regression.yml",
        event="workflow_dispatch",
        status="completed",
        conclusion="success",
        run_started_at=started.isoformat(),
        updated_at=ended.isoformat(),
        referenced_workflows=[dict(path="o/r/" + s.workflow_path + "@main", sha=s.revision) for s in qualification.suites],
    )
    jobs = [dict(id=11 + i, run_id=10, name=s.job_name, status="completed", conclusion="success") for i, s in enumerate(qualification.suites)]
    spec = CliLiveSpecification.model_validate(
        dict(
            evidence_schema="cli-live-evaluation/v1",
            runner=dict(
                adapter="engine-cli-live-evidence-v1",
                qualification_config_path="tests/e2e/cli_regression/qualification.json",
                requirements_path="tests/e2e/cli_regression/requirements.json",
                repository="o/r",
                repository_id=123,
                harness_sha256=harness_digest(),
            ),
            qualification=qualification.model_dump(mode="json"),
            workflows=[
                dict(
                    criterion_id="qualification-run",
                    path=run["path"],
                    source=dict(revision="b" * 40),
                    definition=dict(revision="b" * 40),
                    dispatch_only=False,
                    required_jobs=[j["name"] for j in jobs],
                    artifacts=[
                        dict(
                            name=key[0],
                            path=key[1],
                            predicates=[
                                dict(criterion_id=f"schema-{i}", pointer="/evidence_schema", operation="equals", expected=value[0]["evidence_schema"])
                            ],
                        )
                        for i, (key, value) in enumerate(archives.items())
                    ],
                )
            ],
        )
    )
    return SimpleNamespace(
        qualification=qualification, manifest=manifest, archives=archives, run=run, jobs=jobs, spec=spec, now=ended + timedelta(seconds=1)
    )


def validate(data):
    return validate_qualification(data.qualification, run=data.run, jobs=data.jobs, archives=data.archives, correlation=None, now=data.now)


def refresh_case(case, files):
    reference = case["evidence"][0]
    files[reference["path"]] = encoded({key: value for key, value in case.items() if key != "evidence"})
    reference["sha256"] = hashlib.sha256(files[reference["path"]]).hexdigest()


def refresh_child(data, suite="live"):
    binding = next(s for s in data.qualification.suites if s.suite_id == suite)
    document, files = data.archives[(binding.artifact, binding.report_path)]
    files[binding.report_path] = encoded(document)
    next(item for item in data.manifest["children"] if item["suite_id"] == suite)["sha256"] = hashlib.sha256(files[binding.report_path]).hexdigest()


def test_complete_parent_and_child_evidence_proves_exactly_103_plus_21_criteria(cli_evidence):
    manifest, criteria = validate(cli_evidence)
    assert manifest["partial"] is False and len(criteria) == 124
    assert all(item["passed"] for item in criteria)


@pytest.mark.parametrize(
    "change",
    [
        "partial",
        "target",
        "revision",
        "installed",
        "worker",
        "client",
        "child",
        "missing_case",
        "phase",
        "command_proof",
        "identity",
        "job",
        "child_source",
        "guard",
        "spend",
        "requests",
        "per_request",
        "time",
        "cleanup",
        "restoration",
        "termination",
    ],
)
def test_incomplete_or_changed_cli_evidence_never_becomes_live_acceptance(cli_evidence, change):
    d = cli_evidence
    child, files = d.archives[("live-{run_attempt}", "report.json")]
    if change == "partial":
        d.manifest["partial"] = True
    elif change == "target":
        d.manifest["runtime"]["deployment"]["account_id"] = "999999999999"
    elif change == "revision":
        child["runtime"]["gateway_after"] = "c" * 40
    elif change == "installed":
        child["runtime"]["deployment"]["installed_files"]["adp"] = "0" * 64
    elif change == "worker":
        child["runtime"]["deployment"]["worker_revisions"]["worker"] = "0" * 40
    elif change == "client":
        child["runtime"]["client_kind"] = "eks"
    elif change == "child":
        d.manifest["children"].pop()
    elif change == "missing_case":
        child["cases"].pop()
    elif change == "phase":
        child["cases"][0]["phase"] = "pre"
    elif change == "command_proof":
        files.pop(child["cases"][0]["evidence"][0]["path"])
    elif change == "identity":
        child["cases"][0]["actor_id"] = "foreign-user"
    elif change == "job":
        child["job_id"] = 99
    elif change == "child_source":
        d.run["referenced_workflows"][-1]["sha"] = "c" * 40
    elif change == "guard":
        d.manifest["guards"][0]["enforcement"] = "declared_only"
    elif change == "spend":
        d.manifest["guards"][0]["daily_spend_after_usd"] = "6"
    elif change == "requests":
        d.manifest["guards"][0]["requests"] = 1000
    elif change == "per_request":
        d.manifest["guards"][0]["max_observed_output_tokens_per_request"] = 257
    elif change == "time":
        d.manifest["started_at"] = (datetime.fromisoformat(d.manifest["started_at"]) - timedelta(hours=2)).isoformat()
    elif change == "cleanup":
        d.manifest["cleanup"]["complete"] = False
    elif change == "restoration":
        d.manifest["cleanup"]["configuration_restored"] = False
    elif change == "termination":
        d.manifest["cleanup"]["resources"] = {}
    if change != "child":
        refresh_child(d)
    if change in {"spend", "requests", "per_request"}:
        guard = d.manifest["guards"][0]
        parent_files = d.archives[(d.qualification.manifest_artifact, d.qualification.manifest_path)][1]
        proof({key: value for key, value in guard.items() if key != "evidence"}, parent_files, guard["evidence"]["path"])
        guard["evidence"]["sha256"] = hashlib.sha256(parent_files[guard["evidence"]["path"]]).hexdigest()
    with pytest.raises((CycleBlockedError, ValueError)):
        validate(d)


def test_failed_actual_case_is_a_failed_criterion_not_a_successful_parent_summary(cli_evidence):
    child, files = cli_evidence.archives[("live-{run_attempt}", "report.json")]
    child["cases"][0]["status"] = "blocked"
    refresh_case(child["cases"][0], files)
    refresh_child(cli_evidence)
    _, criteria = validate(cli_evidence)
    assert not next(item["passed"] for item in criteria if item["criterion_id"] == child["cases"][0]["case_id"])


@pytest.mark.parametrize("change", ["missing_criterion", "phase_downgrade", "budget", "time", "schedule"])
def test_acceptance_contract_cannot_drop_criteria_or_expand_shared_bounds(cli_evidence, change):
    document = cli_evidence.spec.model_dump(mode="json")
    if change == "missing_criterion":
        document["qualification"]["criteria"].pop()
    elif change == "phase_downgrade":
        criterion = next(c for c in document["qualification"]["criteria"] if c["criterion_id"] == "5516/AC-05")
        criterion["phases"] = ["pre"]
        criterion["cases"][0]["phase"] = "pre"
    elif change == "budget":
        document["qualification"]["bounds"]["max_daily_inference_usd"] = "6"
    elif change == "time":
        document["qualification"]["bounds"]["max_duration_seconds"] = 10800
    else:
        document["workflows"][0]["dispatch_only"] = True
    with pytest.raises(ValueError):
        CliLiveSpecification.model_validate(document)


@pytest.mark.parametrize("event", ["workflow_dispatch", "schedule"])
async def test_real_provider_downloads_every_cli_artifact_and_rechecks_the_selected_run(cli_evidence, event):
    import base64
    import io
    import zipfile

    data = cli_evidence
    data.run["event"] = event
    definition = b"on:\n  workflow_dispatch:\n  schedule:\n    - cron: '0 5 * * *'\n  pull_request:\njobs: {}\n"
    blobs, artifacts = {}, []
    for index, (key, (_, files)) in enumerate(data.archives.items()):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as zipped:
            for path, content in files.items():
                zipped.writestr(path, content)
        payload = stream.getvalue()
        blobs[index + 100] = payload
        artifacts.append(
            dict(
                id=index + 100,
                name=key[0].replace("{run_attempt}", "1"),
                expired=False,
                size_in_bytes=len(payload),
                digest="sha256:" + hashlib.sha256(payload).hexdigest(),
                created_at=data.run["updated_at"],
            )
        )
    requested = []

    def transport(request):
        path = request.url.path
        requested.append(path)
        if "/contents/" in path:
            value = dict(
                type="file",
                encoding="base64",
                size=len(definition),
                content=base64.b64encode(definition).decode(),
                sha=hashlib.sha1(b"blob " + str(len(definition)).encode() + b"\0" + definition).hexdigest(),
            )
        elif path.endswith("/workflows/nightly-cli-regression.yml/runs"):
            value = dict(workflow_runs=[data.run])
        elif path.endswith("/runs/10"):
            value = data.run
        elif path.endswith("/jobs"):
            value = dict(jobs=data.jobs)
        elif path.endswith("/runs/10/artifacts"):
            value = dict(artifacts=artifacts)
        elif path.endswith("/zip"):
            return httpx.Response(200, content=blobs[int(path.split("/")[-2])])
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RepositoryEvidenceProvider(client=client, clock=lambda: data.now)
        provider.token = AsyncMock(return_value="repository-read-token")
        observed = await provider.workflow(
            SimpleNamespace(repo="o/r", provider_repository_id=123),
            data.spec.workflows[0],
            revisions={},
            max_age_seconds=86400,
            qualification=data.qualification,
        )
    assert len(observed["criteria"]) == 127 and observed["qualification"]["runtime"]["client_kind"] == "ec2"
    assert requested.count("/repos/o/r/actions/workflows/nightly-cli-regression.yml/runs") == 2
    assert len([p for p in requested if p.endswith("/zip")]) == 3


@pytest.mark.parametrize("change", ["observed", "expected", "actor", "command", "arbitrary_proof"])
def test_case_outcome_follows_accepted_typed_execution_contract(cli_evidence, change):
    child, files = cli_evidence.archives[("live-{run_attempt}", "report.json")]
    case = child["cases"][0]
    if change == "observed":
        case["observed"] = "failure"
    elif change == "expected":
        case["expected"] = case["observed"] = "failure"
    elif change == "actor":
        case["actor_id"] = "admin"
    elif change == "command":
        case["command"] = "adp another-command"
    refresh_case(case, files)
    if change == "arbitrary_proof":
        reference = case["evidence"][0]
        files[reference["path"]] = encoded({"status": "passed"})
        reference["sha256"] = hashlib.sha256(files[reference["path"]]).hexdigest()
    refresh_child(cli_evidence)
    if change == "observed":
        _, criteria = validate(cli_evidence)
        assert not next(item["passed"] for item in criteria if item["criterion_id"] == case["case_id"])
    else:
        with pytest.raises((CycleBlockedError, ValueError)):
            validate(cli_evidence)


def test_case_proof_preserves_boolean_and_number_distinction(cli_evidence):
    child, files = cli_evidence.archives[("pre-{run_attempt}", "report.json")]
    case = child["cases"][0]
    document = cli_evidence.qualification.model_dump(mode="json")
    accepted = next(item for item in document["criteria"] if item["criterion_id"] == case["case_id"])
    accepted["cases"][0]["expected"] = 1
    cli_evidence.qualification = Qualification.model_validate(document)
    for payload, _ in cli_evidence.archives.values():
        payload["qualification_sha256"] = digest(document)
    case["expected"] = case["observed"] = 1
    refresh_case(case, files)
    reference = case["evidence"][0]
    record = json.loads(files[reference["path"]])
    record["observed"] = True
    files[reference["path"]] = encoded(record)
    reference["sha256"] = hashlib.sha256(files[reference["path"]]).hexdigest()
    refresh_child(cli_evidence, "pre")
    refresh_child(cli_evidence, "live")
    with pytest.raises(CycleBlockedError, match="command_result"):
        validate(cli_evidence)


def refresh_record(record, files):
    fresh = proof({key: value for key, value in record.items() if key != "evidence"}, files, record["evidence"]["path"])
    record["evidence"] = fresh["evidence"]


def test_live_cases_cannot_precede_actual_ec2_launch(cli_evidence):
    data = cli_evidence
    for artifact, path in [(data.qualification.manifest_artifact, data.qualification.manifest_path), ("live-{run_attempt}", "report.json")]:
        document, files = data.archives[(artifact, path)]
        document["runtime"]["instance_launched_at"] = document["runtime"]["observed_at"] = "2026-09-21T12:00:03Z"
        refresh_record(document["runtime"], files)
    refresh_child(data)
    with pytest.raises(CycleBlockedError, match="before_instance_launch"):
        validate(data)


@pytest.mark.parametrize(
    "change",
    [
        "no_instance",
        "old_instance",
        "no_requests",
        "unjoined_operation",
        "duplicate_request",
        "early_cleanup",
        "missing_resource",
        "missing_configuration",
        "token_limit",
    ],
)
def test_faithful_guard_and_cleanup_snapshots_must_join_actual_cases(cli_evidence, change):
    data = cli_evidence
    guard = data.manifest["guards"][0]
    parent_files = data.archives[(data.qualification.manifest_artifact, data.qualification.manifest_path)][1]
    if change == "no_instance":
        guard["peak_instances"] = 0
    elif change == "old_instance":
        data.manifest["runtime"]["instance_launched_at"] = "2026-09-21T10:00:00Z"
        refresh_record(data.manifest["runtime"], parent_files)
    elif change == "no_requests":
        for operation in guard["operations"]:
            operation["requests"] = []
        guard.update(requests=0, input_tokens=0, output_tokens=0, max_observed_output_tokens_per_request=0, daily_spend_after_usd="1")
    elif change == "unjoined_operation":
        guard["operations"][0]["case_id"] = "different-case"
    elif change == "duplicate_request":
        operation = next(item for item in guard["operations"] if item["requests"])
        operation["requests"].append(copy.deepcopy(operation["requests"][0]))
    elif change == "early_cleanup":
        data.manifest["cleanup"]["observed_at"] = data.manifest["started_at"]
    elif change == "missing_resource":
        data.manifest["cleanup"]["resources"].pop("test-resource")
    elif change == "token_limit":
        operation = next(item for item in guard["operations"] if item["requests"])
        operation["requests"][0]["input_tokens"] = 4097
        guard["input_tokens"] = 4097
    else:
        data.manifest["cleanup"]["restored_configuration"] = []
    refresh_record(guard, parent_files)
    refresh_record(data.manifest["cleanup"], parent_files)
    with pytest.raises((CycleBlockedError, ValueError)):
        validate(data)


@pytest.mark.parametrize("owner,count", [(5329, 8), (5331, 7)])
def test_prerequisite_scope_requires_its_entire_set_and_cannot_be_final(cli_evidence, owner, count):
    document = cli_evidence.qualification.model_dump(mode="json")
    document["owner_issue"] = owner
    document["criteria"] = [item for item in document["criteria"] if item["criterion_id"].startswith(f"{owner}/")]
    document["suites"] = [item for item in document["suites"] if item["suite_id"] == "live"]
    document["criteria"][0]["cases"][0].update(actor_id="admin", requires_inference=True)
    assert len(Qualification.model_validate(document).criteria) == count
    document["owner_issue"] = 5644
    with pytest.raises(ValueError):
        Qualification.model_validate(document)


@pytest.mark.parametrize("owner,count", [(5329, 8), (5331, 7)])
@pytest.mark.parametrize("use_inference", [True, False])
def test_complete_prerequisite_evidence_attests_only_its_accepted_owner(cli_evidence, owner, count, use_inference):
    data = cli_evidence
    document = data.qualification.model_dump(mode="json")
    document["owner_issue"] = owner
    document["criteria"] = [item for item in document["criteria"] if item["criterion_id"].startswith(f"{owner}/")]
    document["suites"] = [item for item in document["suites"] if item["suite_id"] == "live"]
    first = document["criteria"][0]["cases"][0]
    first.update(actor_id="admin" if use_inference else "member", requires_inference=use_inference)
    if not use_inference:
        document["deployment"].update(ordinary_user_id=None, admin_user_id=None)
    data.qualification = Qualification.model_validate(document)
    accepted = {item.criterion_id for item in data.qualification.criteria}
    parent_files = data.archives[(data.qualification.manifest_artifact, data.qualification.manifest_path)][1]
    guard = data.manifest["guards"][0]
    request = next(item["requests"] for item in guard["operations"] if item["requests"])
    guard["operations"] = [item for item in guard["operations"] if item["case_id"] in accepted]
    first_operation = next(item for item in guard["operations"] if item["case_id"] == first["case_id"])
    first_operation.update(actor_id=first["actor_id"], requests=request if use_inference else [])
    if not use_inference:
        guard.update(requests=0, input_tokens=0, output_tokens=0, max_observed_output_tokens_per_request=0, daily_spend_after_usd="1")
    refresh_record(guard, parent_files)
    child, files = data.archives[("live-{run_attempt}", "report.json")]
    child["cases"] = [item for item in child["cases"] if item["case_id"] in accepted]
    first_case = next(item for item in child["cases"] if item["case_id"] == first["case_id"])
    first_case["actor_id"] = first["actor_id"]
    refresh_case(first_case, files)
    for record, proof_files in [(data.manifest["runtime"], parent_files), (child["runtime"], files)]:
        record["deployment"] = data.qualification.deployment.model_dump(mode="json")
        refresh_record(record, proof_files)
    child["qualification_sha256"] = data.manifest["qualification_sha256"] = digest(data.qualification.model_dump(mode="json"))
    data.manifest["children"] = [item for item in data.manifest["children"] if item["suite_id"] == "live"]
    refresh_child(data)
    _, criteria = validate(data)
    assert len(criteria) == count and all(item["passed"] for item in criteria)
    assert {item["criterion_id"] for item in criteria} == accepted


@pytest.mark.parametrize(
    "criterion_id",
    [
        "5637/CLI-24-AC-03",  # Actual domain operations, not this issue's AC04 regressions.
        "5628/CLI-15-AC-01",  # Real marked inference; AC04 is output edge cases.
        "5629/CLI-16-AC-02",  # Hosted live control; AC04 is retry/detach semantics.
        "5627/CLI-14-AC-02",  # Actual RPM enforcement.
        "5627/CLI-14-AC-03",  # Real token usage/overlap.
        "5622/CLI-09-AC-01",  # Tenant selection through local inference/refresh.
        "5635/CLI-22-AC-01",  # Actual provider approval.
        "5329/validation-04",  # Live inputs and trusted validation evidence.
        "5329/validation-08",  # Integrated deployed amendment scenario.
        "5331/validation-07",  # Deployed hosted-planning smoke.
    ],
)
def test_source_defined_live_criteria_refuse_offline_only_mapping(cli_evidence, criterion_id):
    document = cli_evidence.qualification.model_dump(mode="json")
    item = next(item for item in document["criteria"] if item["criterion_id"] == criterion_id)
    item["phases"] = ["pre"]
    item["cases"][0].update(phase="pre", suite_id="pre", requires_inference=False)
    with pytest.raises(ValueError, match="source contract requires live evidence"):
        Qualification.model_validate(document)


@pytest.mark.parametrize("criterion_id", ["5637/CLI-24-AC-04", "5628/CLI-15-AC-04", "5629/CLI-16-AC-04"])
def test_regression_rows_do_not_acquire_live_or_inference_requirements_from_position(cli_evidence, criterion_id):
    document = cli_evidence.qualification.model_dump(mode="json")
    item = next(item for item in document["criteria"] if item["criterion_id"] == criterion_id)
    item["phases"] = ["pre"]
    item["cases"][0].update(phase="pre", suite_id="pre", requires_inference=False)
    accepted = Qualification.model_validate(document)
    item = next(item for item in accepted.criteria if item.criterion_id == criterion_id)
    assert item.phases == ["pre"] and not item.cases[0].requires_inference


@pytest.mark.parametrize("issue", [5623, 5625])
def test_shared_story_live_boundary_requires_reviewed_scenario_without_guessing_ac_position(cli_evidence, issue):
    document = cli_evidence.qualification.model_dump(mode="json")
    selected = [item for item in document["criteria"] if item["criterion_id"].startswith(f"{issue}/")]
    for item in selected:
        item["phases"] = ["pre"]
        item["cases"][0].update(phase="pre", suite_id="pre", requires_inference=False)
    with pytest.raises(ValueError, match="source live acceptance boundary"):
        Qualification.model_validate(document)
    selected[0]["phases"] = ["live"]
    selected[0]["cases"][0].update(phase="live", suite_id="live")
    accepted = Qualification.model_validate(document)
    assert next(item for item in accepted.criteria if item.criterion_id == selected[-1]["criterion_id"]).phases == ["pre"]


@pytest.fixture
def cli_sources(cli_evidence):
    data = cli_evidence
    document = data.spec.model_dump(mode="json")
    statements = [
        {"criterion_id": item.criterion_id, "source_text": f"Required source statement {item.criterion_id}."} for item in data.qualification.criteria
    ]
    bodies = {}
    for item in statements:
        issue = int(item["criterion_id"].split("/")[0])
        bodies[issue] = bodies.get(issue, "") + item["source_text"] + "\n"
    requirements = encoded(dict(evidence_schema="cli-live-requirements/v1", criteria=statements))
    qualification = document["qualification"]
    qualification["requirements_sha256"] = hashlib.sha256(requirements).hexdigest()
    for criterion, statement in zip(qualification["criteria"], statements, strict=True):
        criterion["source_body_sha256"] = hashlib.sha256(bodies[int(criterion["criterion_id"].split("/")[0])].encode()).hexdigest()
        criterion["requirement_sha256"] = hashlib.sha256(statement["source_text"].encode()).hexdigest()
    spec = CliLiveSpecification.model_validate(document)
    blobs = {spec.runner.qualification_config_path: encoded(qualification), spec.runner.requirements_path: requirements}
    provider = RepositoryEvidenceProvider()
    provider.pull_request = AsyncMock(side_effect=lambda binding, source: source)
    provider.definition_blob = AsyncMock(side_effect=lambda binding, path, revision: ("b" * 40, blobs[path]))

    async def request(binding, method, path, **kwargs):
        if path == "/repos/o/r":
            value = dict(id=123)
        else:
            issue = int(path.rsplit("/", 1)[1])
            value = dict(number=issue, body=bodies[issue])
        return httpx.Response(200, json=value)

    provider.request = request
    sources = [dict(issue_number=issue, merge_sha="b" * 40) for issue in bodies]
    return SimpleNamespace(
        spec=spec, provider=provider, blobs=blobs, bodies=bodies, sources=sources, binding=SimpleNamespace(repo="o/r", provider_repository_id=123)
    )


@pytest.mark.parametrize("change", [None, "configuration", "requirements", "body", "missing_external", "statement"])
async def test_configuration_requirements_and_each_delivery_source_are_source_pinned(cli_sources, change):
    ctx = cli_sources
    if change == "configuration":
        document = json.loads(ctx.blobs[ctx.spec.runner.qualification_config_path])
        document["bounds"]["max_requests"] -= 1
        ctx.blobs[ctx.spec.runner.qualification_config_path] = encoded(document)
    elif change == "requirements":
        ctx.blobs[ctx.spec.runner.requirements_path] += b"\n"
    elif change == "body":
        ctx.bodies[5329] += "Changed requirement."
    elif change == "missing_external":
        ctx.sources = [source for source in ctx.sources if source["issue_number"] != 5329]
    elif change == "statement":
        document = ctx.spec.model_dump(mode="json")
        criterion = next(item for item in document["qualification"]["criteria"] if item["criterion_id"].startswith("5329/"))
        criterion["requirement_sha256"] = "0" * 64
        ctx.spec = CliLiveSpecification.model_validate(document)
        ctx.blobs[ctx.spec.runner.qualification_config_path] = encoded(document["qualification"])
    if change is None:
        pulls, _ = await ctx.provider.verify_sources(ctx.binding, ctx.spec, ctx.sources)
        assert len(pulls) == 26
        assert all(call.args[2] == "b" * 40 for call in ctx.provider.definition_blob.await_args_list)
    else:
        with pytest.raises((CycleBlockedError, ValueError)):
            await ctx.provider.verify_sources(ctx.binding, ctx.spec, ctx.sources)
