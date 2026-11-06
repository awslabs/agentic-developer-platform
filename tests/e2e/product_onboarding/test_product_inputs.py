"""Check the protected workflow handoff without treating fixture reports as live evidence."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from tests.e2e.product_onboarding import product_inputs
from tests.e2e.product_onboarding.product_d01_release import materialize
from tests.e2e.product_onboarding.product_d01_cases import evaluate as grade_release
from tests.e2e.product_onboarding.product_evidence import SCENARIOS
from tests.e2e.product_onboarding.product_publish import publish
from tests.e2e.product_onboarding.test_product_evidence import complete_evidence


@pytest.fixture
def reports(tmp_path):
    evidence = complete_evidence()
    selection = list(SCENARIOS)
    dispatch = {
        "selected_cases": selection,
        "expected_revision": evidence["identities"]["source_revision"],
        "observed_revision": evidence["identities"]["source_revision"],
        "workflow_revision": evidence["identities"]["workflow_revision"],
        "run_id": evidence["run_id"],
        "status": "pass",
        "cleanup_verified": True,
        "observation_ref": evidence["deployment"]["observation_ref"],
    }
    api = {
        "run_id": evidence["run_id"],
        "source_revision": dispatch["expected_revision"],
        "workflow_revision": dispatch["workflow_revision"],
        "observation_ref": dispatch["observation_ref"],
        "inventory": {
            "source_revision": dispatch["expected_revision"],
            "digest": evidence["identities"]["api_image_digest"],
            "pods": [
                {
                    "uid": "private:pod-1",
                    "digest": evidence["identities"]["api_image_digest"],
                }
            ],
        },
        "native": {
            "source_revision": dispatch["expected_revision"],
            "observation_ref": dispatch["observation_ref"],
            "tick_verified": True,
            "tick_digest": evidence["identities"]["api_image_digest"],
            "pod_uids": ["private:pod-1"],
            "database_heads": ["schema-01"],
            "image_heads": ["schema-01"],
        },
        "product": {
            "run_id": evidence["run_id"],
            "source_revision": dispatch["expected_revision"],
            "cleanup": {"fixture_removed": True},
            "results": {
                name: {
                    "product_id": "owned-product",
                    **(
                        {
                            "attempts": [{"state": "response_unknown"}],
                            "cleanup_verified": True,
                        }
                        if name == "api_recovery_cleanup"
                        else {}
                    ),
                }
                for name in selection
            },
        },
    }
    release = {
        "plan_revision": evidence["plan_revision"],
        "identities": deepcopy(evidence["identities"]),
        "deployment": deepcopy(evidence["deployment"]),
    }
    cli = {
        "schema_version": 1,
        "surface": "installed-cli",
        "status": "passed",
        "failures": [],
        "cleanup": "complete",
        "cleanup_outstanding": [],
        "workflow_run_id": evidence["run_id"],
        "evaluation_id": "adp-e2e-20261020-113000-abcdef",
        "artifact_ref": "private:product-v03/test-run-0001/adp-e2e-20261020-113000-abcdef/report.json",
        "attempt_id": "attempt-1",
        "source_revision": dispatch["expected_revision"],
        "workflow_revision": dispatch["workflow_revision"],
        "api_image_digest": evidence["identities"]["api_image_digest"],
        "cli_manifest_sha256": evidence["identities"]["cli_checksum"],
        "cases": [
            {"case_id": case_id, "status": "passed"}
            for case_id in ("PR03-C01", "PR03-C02", "PR03-C03")
        ],
        "attempts": [
            {
                "case_id": case_id,
                "status": "passed",
                **(
                    {
                        "cli_events": [
                            {
                                "argv": ["adp", "product", "show", "--json"],
                                "exit_code": 0,
                                "output": {"status": "ok"},
                            }
                        ]
                    }
                    if case_id == "PR03-C01"
                    else {}
                ),
            }
            for case_id in ("PR03-C01", "PR03-C02", "PR03-C03")
        ],
    }
    paths = {
        key: tmp_path / (key + ".json") for key in ("dispatch", "api", "release", "cli")
    }
    return paths, {"dispatch": dispatch, "api": api, "release": release, "cli": cli}


def produce_and_publish(tmp_path, reports):
    paths, documents = reports
    for key, path in paths.items():
        if documents[key] is not None:
            path.write_text(json.dumps(documents[key]))
    inputs_path, packet_path = (
        tmp_path / "product-b01-inputs.json",
        tmp_path / "product-b01.json",
    )
    inputs = product_inputs.assemble(
        paths["dispatch"],
        paths["api"],
        paths["release"],
        paths["cli"],
        inputs_path,
        packet_path,
    )
    dispatch = documents["dispatch"]
    packet = publish(
        inputs_path,
        paths["dispatch"],
        packet_path,
        selection=",".join(SCENARIOS),
        source_revision=dispatch["expected_revision"],
        workflow_revision=dispatch["workflow_revision"],
    )
    return inputs, packet


def test_controlled_reports_flow_into_exact_dispatch_packet(tmp_path, reports):
    inputs, packet = produce_and_publish(tmp_path, reports)
    assert packet["status"] == "pass"
    assert packet["report_artifact"] == str(tmp_path / "product-b01.json")
    assert packet["dispatch"]["run_id"] == inputs["run_id"]
    assert (
        packet["dispatch"]["observation_ref"] == inputs["deployment"]["observation_ref"]
    )
    assert (
        packet["cases"]["api_recovery_cleanup"]["attempts"][0]["result"]
        == "interrupted"
    )
    assert packet["surfaces"]["api"]["artifact_ref"] == str(reports[0]["api"])
    assert (
        packet["surfaces"]["cli"]["cli_checksum"]
        == inputs["identities"]["cli_checksum"]
    )
    assert json.loads((tmp_path / "product-b01.json").read_text()) == packet


def signed_release_reports(reports):
    _, documents = reports
    dispatch, api, release = (documents[key] for key in ("dispatch", "api", "release"))
    source = dispatch["expected_revision"]
    workflow = dispatch["workflow_revision"]
    digest = api["inventory"]["digest"]
    schema = api["native"]["database_heads"][0]
    checksum = release["identities"]["cli_checksum"]
    api["native"].update({
        "release_ref": "s3://private/release?versionId=one",
        "release_hash": "f" * 64,
        "release_artifact": {
            "component": "gateway-backend",
            "workflow_path": ".github/workflows/gateway-deploy.yml",
            "account_id": "000000000000",
            "region": "us-east-1",
            "resource_id": "example-cluster/adp-gateway",
            "repository_id": 17,
            "run_id": 42,
            "run_attempt": 1,
            "source_revision": source,
            "workflow_revision": workflow,
            "image_digest": digest,
            "migration_head": schema,
            "cli_manifest_sha256": checksum,
            "product_profile_enabled": True,
            "product_onboarding_enabled": False,
        },
        "runtime_receipt": {
            "component": "gateway-backend",
            "actual_revision": source,
            "artifact_hash": "f" * 64,
            "image_digest": digest,
            "migration_head": schema,
            "cli_manifest_sha256": checksum,
            "product_profile_enabled": True,
            "product_onboarding_enabled": False,
            "tick_digest": digest,
            "pod_uids": [pod["uid"] for pod in api["inventory"]["pods"]],
        },
        "target": {
            "account_id": "000000000000",
            "region": "us-east-1",
            "resource_id": "example-cluster/adp-gateway",
            "repository_id": 17,
        },
    })
    dispatch["run_id"] = "product-42-1"
    dispatch["observation_ref"] = "runtime:sha256:" + "a" * 64
    api["run_id"] = "product-42-1"
    api["observation_ref"] = dispatch["observation_ref"]
    api["native"]["observation_ref"] = dispatch["observation_ref"]
    api["product"]["run_id"] = "product-42-1"
    documents["cli"]["workflow_run_id"] = "product-42-1"
    documents["cli"]["artifact_ref"] = (
        "private:product-v03/product-42-1/adp-e2e-20261020-113000-abcdef/report.json"
    )


def test_selected_signed_release_feeds_api_and_installed_cli_packet(tmp_path, reports):
    signed_release_reports(reports)
    paths, documents = reports
    for key in ("dispatch", "api"):
        paths[key].write_text(json.dumps(documents[key]))
    documents["release"] = materialize(
        paths["dispatch"], paths["api"], paths["release"]
    )
    inputs, packet = produce_and_publish(tmp_path, reports)
    assert packet["status"] == "pass"
    assert inputs["deployment"]["receipt_ref"] == "s3://private/release?versionId=one"
    assert inputs["identities"]["schema_head"] == "schema-01"
    assert inputs["surfaces"]["cli"]["cli_checksum"] == documents["release"]["identities"]["cli_checksum"]
    verdict = grade_release(inputs, documents["dispatch"], packet, documents["release"])
    assert verdict["cases"]["PR01-C01"]["status"] == "PASSED"
    assert verdict["cases"]["PR01-C02"]["status"] == "PASSED"
    assert all(verdict["cases"][case]["status"] == "NOT_RUN" for case in ("PR01-C03", "PR01-C04"))
    assert verdict["status"] == "incomplete"


@pytest.mark.parametrize("change", (
    "missing_case", "stale_source", "stale_schema", "mismatched_image",
    "wrong_target", "missing_cli", "health_only", "missing_packet",
))
def test_d01_release_verdict_rejects_false_green(tmp_path, reports, change):
    signed_release_reports(reports)
    paths, documents = reports
    for key in ("dispatch", "api"):
        paths[key].write_text(json.dumps(documents[key]))
    documents["release"] = materialize(paths["dispatch"], paths["api"], paths["release"])
    inputs, packet = produce_and_publish(tmp_path, reports)
    assert packet["status"] == "pass"
    if change == "missing_case":
        packet["cases"].pop("api_product_lifecycle")
    elif change == "stale_source":
        packet["identities"]["source_revision"] = "0" * 40
    elif change == "stale_schema":
        packet["deployment"]["observed"]["schema_head"] = "older-head"
    elif change == "mismatched_image":
        packet["deployment"]["observed"]["api_image_digest"] = "sha256:" + "0" * 64
    elif change == "wrong_target":
        documents["release"]["deployment"]["receipt_ref"] = "s3://other/release?versionId=two"
    elif change == "missing_cli":
        packet["surfaces"].pop("cli")
    elif change == "health_only":
        packet["deployment"]["observed"]["serving_pods"] = []
    elif change == "missing_packet":
        packet = {}
    verdict = grade_release(inputs, documents["dispatch"], packet, documents["release"])
    assert verdict["status"] == "incomplete"
    assert verdict["cases"]["PR01-C01"]["status"] == "FAILED"
    assert verdict["cases"]["PR01-C02"]["status"] == "NOT_RUN"


@pytest.mark.parametrize("change", ("missing", "schema", "digest", "source", "run", "flags", "pod", "receipt", "target"))
def test_release_handoff_rejects_incomplete_or_mismatched_observation(tmp_path, reports, change):
    signed_release_reports(reports)
    paths, documents = reports
    native = documents["api"]["native"]
    if change == "missing":
        native.pop("runtime_receipt")
    elif change == "schema":
        native["runtime_receipt"]["migration_head"] = "old-schema"
    elif change == "digest":
        native["release_artifact"]["image_digest"] = "sha256:" + "1" * 64
    elif change == "source":
        native["runtime_receipt"]["actual_revision"] = "1" * 40
    elif change == "run":
        native["release_artifact"]["run_attempt"] = 2
    elif change == "flags":
        native["runtime_receipt"]["product_onboarding_enabled"] = True
    elif change == "pod":
        native["runtime_receipt"]["pod_uids"] = ["wrong-pod"]
    elif change == "receipt":
        native.pop("release_ref")
    elif change == "target":
        native["release_artifact"]["resource_id"] = "another-cluster/adp-gateway"
    for key in ("dispatch", "api"):
        paths[key].write_text(json.dumps(documents[key]))
    with pytest.raises(ValueError, match="matched signed and independent"):
        materialize(paths["dispatch"], paths["api"], paths["release"])
    assert not paths["release"].exists()


def test_v03_writer_with_controlled_private_retention_feeds_packet(tmp_path, reports):
    from tests.e2e.cli_uplift import report
    from tests.e2e.cli_uplift import product_cli_handoff, statestore
    from tests.unit.test_cli_uplift import FakeS3
    from tests.unit.test_product_profile_cli import complete_product_report_fixture

    document, matrix, attempts = complete_product_report_fixture()
    run_id = "product-12345-1"
    reports[1]["dispatch"]["run_id"] = run_id
    reports[1]["api"]["run_id"] = run_id
    reports[1]["api"]["product"]["run_id"] = run_id
    document["product_run_id"] = run_id
    private = report.product_cli_report(document, attempts)
    assert private["status"] == "passed", private["failures"]
    document.pop("product_run_id")
    paths = report.write(
        tmp_path / "v03", document, matrix, document["evaluation_id"],
        product_cli_attempts=attempts, product_run_id=run_id,
    )
    store = statestore.S3StateStore(FakeS3(), "private-evaluator-state")
    private_path = paths["product_cli_private"]
    product_cli_handoff.retain(
        store, run_id, private_path, private["source_revision"], private["workflow_revision"]
    )
    dispatch_path = tmp_path / "dispatch-v03.json"
    dispatch_path.write_text(json.dumps(reports[1]["dispatch"]))
    loaded_path = tmp_path / "loaded-private.json"
    product_cli_handoff.load(store, dispatch_path, loaded_path)
    private = json.loads(loaded_path.read_text())
    digest = private["api_image_digest"]
    checksum = private["cli_manifest_sha256"]
    reports[1]["api"]["inventory"]["digest"] = digest
    reports[1]["api"]["inventory"]["pods"][0]["digest"] = digest
    reports[1]["api"]["native"]["tick_digest"] = digest
    reports[1]["release"]["identities"].update(
        api_image_digest=digest, cli_checksum=checksum
    )
    observed = reports[1]["release"]["deployment"]["observed"]
    observed.update(api_image_digest=digest, cli_checksum=checksum)
    observed["serving_pods"][0]["api_image_digest"] = digest
    reports[1]["cli"] = private
    inputs, packet = produce_and_publish(tmp_path, reports)
    assert packet["status"] == "pass"
    assert inputs["surfaces"]["cli"]["artifact_ref"] == private["artifact_ref"]
    assert inputs["surfaces"]["cli"]["stdout_ref"].startswith(private["artifact_ref"])
    assert "private-fixture-id" not in str(packet)


@pytest.mark.parametrize(
    "source,field,value",
    [
        ("dispatch", "status", "incomplete"),
        ("api", "run_id", "other-run"),
        ("api", "source_revision", "f" * 40),
        ("api", "observation_ref", "other-observation"),
        ("release", "plan_revision", None),
        ("cli", "workflow_run_id", "other-run"),
        ("cli", "status", "incomplete"),
    ],
)
def test_mismatched_or_incomplete_inputs_refuse(
    tmp_path, reports, source, field, value
):
    reports[1][source][field] = value
    _, packet = produce_and_publish(tmp_path, reports)
    assert packet["status"] == "incomplete" and packet["issues"]


@pytest.mark.parametrize(
    "change",
    [
        lambda cli: cli.update(workflow_run_id="other-run"),
        lambda cli: cli.pop("artifact_ref"),
        lambda cli: cli.update(artifact_ref="/tmp/unretained-cli-report.json"),
        lambda cli: cli.update(cli_manifest_sha256="f" * 64),
        lambda cli: cli.update(api_image_digest="sha256:" + "f" * 64),
        lambda cli: cli.update(cleanup="failed"),
        lambda cli: cli["cases"].pop(),
        lambda cli: cli["attempts"].pop(),
        lambda cli: cli["attempts"].append({"case_id": "PR03-C01", "status": "failed"}),
        lambda cli: cli["attempts"][0]["cli_events"][0].update(exit_code=1),
        lambda cli: cli["attempts"][0]["cli_events"][0].update(
            argv=["adp", "product", "create"]
        ),
        lambda cli: cli.update(surface={"status": "pass"}, status="incomplete"),
    ],
)
def test_private_v03_proof_is_required_before_cli_surface_passes(
    tmp_path, reports, change
):
    change(reports[1]["cli"])
    inputs, packet = produce_and_publish(tmp_path, reports)
    assert "cli" not in inputs["surfaces"]
    assert packet["status"] == "incomplete"
    assert any("installed CLI report" in issue for issue in inputs["producer_issues"])


@pytest.mark.parametrize(
    "tamper",
    [
        lambda reports: reports["api"]["native"].update(tick_verified=False),
        lambda reports: reports["api"]["inventory"]["pods"][0].update(
            uid="replaced-pod"
        ),
        lambda reports: reports["release"]["identities"].update(
            schema_head="stale-head"
        ),
        lambda reports: reports["release"]["deployment"]["observed"]["serving_pods"][
            0
        ].update(pod_ref="other-pod"),
        lambda reports: reports["cli"].update(cli_manifest_sha256="f" * 64),
    ],
)
def test_stale_native_deployment_or_installed_cli_is_incomplete(
    tmp_path, reports, tamper
):
    tamper(reports[1])
    _, packet = produce_and_publish(tmp_path, reports)
    assert packet["status"] == "incomplete" and packet["issues"]


def test_nested_reports_do_not_publish_unrelated_private_fields(tmp_path, reports):
    reports[1]["release"]["identities"]["actor_token"] = "private-actor-secret"
    reports[1]["release"]["deployment"]["observed"]["actor_token"] = (
        "private-actor-secret"
    )
    reports[1]["cli"]["attempts"][0]["cli_events"][0]["output"]["actor_token"] = (
        "private-actor-secret"
    )
    reports[1]["api"]["product"]["actor_token"] = "private-actor-secret"
    inputs, packet = produce_and_publish(tmp_path, reports)
    assert packet["status"] == "pass"
    assert "private-actor-secret" not in str(inputs)
    assert "private-actor-secret" not in str(packet)


def test_malformed_native_proof_is_incomplete_not_a_producer_crash(tmp_path, reports):
    reports[1]["api"]["native"]["database_heads"] = [{"not": "a-head"}]
    _, packet = produce_and_publish(tmp_path, reports)
    assert packet["status"] == "incomplete"


def test_cli_entrypoint_writes_inputs_for_publisher(tmp_path, reports):
    paths, documents = reports
    for key, path in paths.items():
        path.write_text(json.dumps(documents[key]))
    inputs = tmp_path / "product-b01-inputs.json"
    packet = tmp_path / "product-b01.json"
    assert (
        product_inputs.main(
            [
                "--dispatch-report",
                str(paths["dispatch"]),
                "--api-evidence",
                str(paths["api"]),
                "--release-report",
                str(paths["release"]),
                "--cli-report",
                str(paths["cli"]),
                "--output",
                str(inputs),
                "--packet-path",
                str(packet),
            ]
        )
        == 0
    )
    assert json.loads(inputs.read_text())["report_artifact"] == str(packet)
    assert (
        publish(
            inputs,
            paths["dispatch"],
            packet,
            selection=",".join(SCENARIOS),
            source_revision=documents["dispatch"]["expected_revision"],
            workflow_revision=documents["dispatch"]["workflow_revision"],
        )["status"]
        == "pass"
    )


def test_absent_independent_release_or_cli_never_passes(tmp_path, reports):
    reports[1]["release"] = None
    reports[1]["cli"] = None
    inputs, packet = produce_and_publish(tmp_path, reports)
    assert inputs["deployment"] == {} and "cli" not in inputs["surfaces"]
    assert packet["status"] == "incomplete"
    assert (
        "independent deployment receipt is missing or unreadable"
        in inputs["producer_issues"]
    )
    assert "installed CLI report is missing or unreadable" in inputs["producer_issues"]


def test_packet_shaped_cli_claim_does_not_replace_private_v03_receipt(
    tmp_path, reports
):
    reports[1]["cli"] = {
        "run_id": reports[1]["dispatch"]["run_id"],
        "source_revision": reports[1]["dispatch"]["expected_revision"],
        "surface": {
            "status": "pass",
            "cli_checksum": reports[1]["release"]["identities"]["cli_checksum"],
        },
    }
    inputs, packet = produce_and_publish(tmp_path, reports)
    assert "cli" not in inputs["surfaces"]
    assert packet["status"] == "incomplete"


def test_workflow_produces_inputs_from_same_api_report_and_uploads_packet():
    workflow = Path(".github/workflows/gateway-live-tests.yml").read_text()
    assert "python -m tests.e2e.product_onboarding.product_d01_release" in workflow
    assert workflow.index("python -m tests.e2e.product_onboarding.product_d01_release") < workflow.index("python -m tests.e2e.product_onboarding.product_inputs")
    assert workflow.index("python -m tests.e2e.product_onboarding.product_publish") < workflow.index("python -m tests.e2e.product_onboarding.product_d01_cases")
    assert workflow.index("python -m tests.e2e.product_onboarding.product_d01_cases") < workflow.index("uses: ./.github/actions/upload-ci-evidence-s3")
    assert "--api-evidence test-results/product-v01-evidence.json" in workflow
    assert '--cli-report "$RUNNER_TEMP/product-profile-cli.private.json"' in workflow
    assert "python -m tests.e2e.product_onboarding.product_inputs" in workflow
    assert "--packet-path test-results/product-b01.json" in workflow
    assert workflow.index("Assemble selected B01 Product inputs") < workflow.index(
        "Publish selected B01 Product packet"
    )
