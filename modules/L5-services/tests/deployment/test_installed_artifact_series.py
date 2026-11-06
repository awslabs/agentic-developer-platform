"""Offline contracts for executed modular readbacks; never live acceptance."""

import base64
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import sys
from types import SimpleNamespace

from botocore.exceptions import ClientError
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import qualification_installed_artifacts as artifacts
import qualification_well_architected as driver
from l5_services.runtime_names import functions, queue_scope


@pytest.fixture
def selection(tmp_path):
    cli = tmp_path / "cli"
    cli.mkdir()
    pins = {}
    for name in ("adp", "adp_deployments.py", "adp-service.py", "adp_common.py", "bg-cognito-auth.sh", "command-manifest.json"):
        path = cli / name
        path.write_text("installed fixture " + name)
        path.chmod(0o700 if name == "adp" else 0o600)
        pins[name] = sha256(path.read_bytes()).hexdigest()
    assets = {"index.html": "1" * 64, "assets/client.js": "2" * 64}
    config = {"cluster_name": "selected-cluster", "namespace": "adp-gateway",
              "proof_invoke_url": "https://example.execute-api.us-east-1.amazonaws.com/prod",
              "release_bucket": "private-release-example", "frontend_assets": assets,
              "cli_directory": str(cli), "cli_files_sha256": pins,
              "product_profile_enabled": True, "product_onboarding_enabled": True}
    fixture = SimpleNamespace(queue_arn="arn:aws:sqs:us-east-1:000000000000:adp-dev-l5-well-architected-work",
                              account_id="000000000000", role_arn="arn:aws:iam::000000000000:role/release",
                              source_revision="a" * 40, wheel_sha256="b" * 64,
                              worker_code_sha256=base64.b64encode(bytes.fromhex("c" * 64)).decode(),
                              schema_sha256="d" * 64, gateway_image_digest="e" * 64,
                              frontend_asset_sha256=artifacts.canonical(assets), cli_manifest_sha256=pins["command-manifest.json"],
                              manifest_version="manifest-1", database_head="head-1", package_version="0.1.0")
    context = {"service": "well_architected", "environment": "dev", "source_revision": "a" * 40, "run_id": 7}
    return config, fixture, context


def fake_readers(fixture, *, fault=None):
    names = functions("dev", "well_architected")
    calls = []
    count = 0

    def identity(account, role):
        assert account == fixture.account_id and role == fixture.role_arn
        calls.append("identity")

    def state(environment, worker, repair, uuid):
        nonlocal count
        count += 1
        assert (worker, repair) == names
        return {"worker": {"CodeSha256": fixture.worker_code_sha256, "State": "Active", "LastUpdateStatus": "Successful",
                           "RevisionId": "changed" if fault == "rollout" and count > 1 else "worker-1"},
                "repair": {"CodeSha256": "wrong" if fault == "wrong_archive" else fixture.worker_code_sha256,
                           "State": "Active", "LastUpdateStatus": "Successful", "RevisionId": "repair-1"},
                "mapping": "Enabled", "rule": "ENABLED"}

    def mapping(worker, environment, account, service):
        assert worker == names[0] and service == "well_architected"
        calls.append("mapping")
        return "mapping-1"

    def url(**kwargs):
        assert kwargs["FunctionName"] in names
        if fault == "public_url":
            return {"FunctionUrl": "https://example.lambda-url.us-east-1.on.aws"}
        code = "AccessDeniedException" if fault == "url_denied" else "ResourceNotFoundException"
        raise ClientError({"Error": {"Code": code}}, "GetFunctionUrlConfig")

    sts = SimpleNamespace(get_caller_identity=lambda: {"Account": fixture.account_id,
                          "Arn": "arn:aws:sts::000000000000:assumed-role/" + ("foreign" if fault == "role" else "release") + "/session"})
    session = SimpleNamespace(region_name="us-east-1", client=lambda name, **kwargs: sts if name == "sts" else SimpleNamespace(get_function_url_config=url))
    lifecycle = SimpleNamespace(identity=identity, manifest_for=lambda *args: ("archive", "archive-version-1", fixture.worker_code_sha256),
        mapping=mapping, state=state, scope_readback=lambda *args: calls.append("scope"), auxiliary_names=lambda *args: (),
        auxiliary_state=lambda *args: {}, verify_runtime=lambda *args: calls.append("runtime"))
    gateway = lambda *args: {"deployment_uid": "deployment-1", "deployment_generation": 3, "migration_head": "head-1", "pod_uids": ["pod-1"]}
    frontend = lambda session, config, fixture, context: {"assets": config["frontend_assets"], "asset_map_sha256": artifacts.canonical(config["frontend_assets"])}
    return {"session": session, "lifecycle": lifecycle, "read_gateway": gateway, "read_frontend": frontend}, calls


@pytest.mark.parametrize("service", ["security", "devops", "transform", "well_architected", "finops"])
def test_all_modular_service_names_round_trip_through_the_same_runtime_authority(service):
    worker, repair = functions("test", service)
    queue = f"arn:aws:sqs:us-east-1:000000000000:{worker.removesuffix('-worker')}-work"
    assert queue_scope(queue) == ("us-east-1", "000000000000", "test", service)
    assert repair == worker.removesuffix("-worker") + "-repair"
    with pytest.raises(ValueError):
        queue_scope(queue + "-other")


def test_collector_uses_actual_selected_modular_readers_and_retains_limitations(selection):
    config, fixture, context = selection
    readers, calls = fake_readers(fixture)
    result = artifacts.collect(config, fixture, context, **readers)
    assert result["digests"]["worker"] == "c" * 64
    assert set(result["functions"]) == set(functions("dev", "well_architected"))
    assert calls == ["identity", "mapping", "scope", "runtime", "identity"]
    assert "prior_schema_and_rollback_not_observed" in result["limitations"]


@pytest.mark.parametrize("fault", ["role", "wrong_archive", "public_url", "url_denied", "rollout"])
def test_denied_unknown_foreign_or_changed_runtime_never_passes(selection, fault):
    config, fixture, context = selection
    readers, _ = fake_readers(fixture, fault=fault)
    with pytest.raises(ValueError):
        artifacts.collect(config, fixture, context, **readers)


@pytest.mark.parametrize("fault", ["service", "cli", "frontend", "traversal", "invoke_url"])
def test_selection_cannot_redirect_runtime_or_substitute_installed_bytes(selection, fault):
    config, fixture, context = selection
    if fault == "service":
        context["service"] = "security"
    elif fault == "cli":
        (Path(config["cli_directory"]) / "adp_common.py").write_text("changed")
    elif fault == "frontend":
        config["frontend_assets"]["index.html"] = "0" * 64
    elif fault == "traversal":
        config["frontend_assets"]["../private"] = "0" * 64
        fixture.frontend_asset_sha256 = artifacts.canonical(config["frontend_assets"])
    else:
        config["proof_invoke_url"] = "https://arbitrary.example.test"
    with pytest.raises(ValueError):
        artifacts.validate_selection(config, fixture, context)


def test_native_checkpoint_chain_survives_restart_without_retrospective_rebaseline(selection, tmp_path):
    config, fixture, context = selection
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    readers, _ = fake_readers(fixture)
    snapshot = artifacts.collect(config, fixture, context, **readers)
    calls = []
    def collect(*args):
        calls.append("read")
        return deepcopy(snapshot)
    series = artifacts.SnapshotSeries(directory, config, fixture, context, driver, collector=collect)
    first = series.checkpoint("before-native-cases")
    assert series.checkpoint("before-native-cases") == first
    assert calls == ["read"]
    cases = [{"run_id": "run-" + str(i), "result_revision": 1,
              "result": {"scope": {"tenant_id": "tenant", "product_id": "product-" + str(i), "project_id": "project-" + str(i)}}}
             for i in (1, 2)]
    for index, case in enumerate(cases, 1):
        series.checkpoint("after-native-case-" + str(index), completed_case=artifacts.case_identity(case))
    snapshot["observed_at"] = artifacts.datetime.now(artifacts.UTC).isoformat()
    proof = series.verify(cases)
    assert len(proof["snapshot_sha256"]) == 3
    assert proof["case_identities"] == [artifacts.case_identity(case) for case in cases]
    assert proof["observation_interval"] == {"start": first["observation"]["observed_at"], "end": snapshot["observed_at"]}
    assert proof["observation_interval"]["start"] != proof["observation_interval"]["end"]
    snapshot["functions"][functions("dev", "well_architected")[0]]["revision_id"] = "changed"
    with pytest.raises(ValueError, match="changed after"):
        series.verify(cases)
    with pytest.raises(ValueError, match="different Product"):
        series.checkpoint("after-native-case-1", completed_case=artifacts.case_identity(cases[1]))


def test_completed_cases_cannot_supply_pass_flags_in_place_of_the_original_baseline(selection, tmp_path):
    config, fixture, context = selection
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    series = artifacts.SnapshotSeries(directory, config, fixture, context, driver, collector=lambda *args: pytest.fail("no new baseline"))
    with pytest.raises(FileNotFoundError):
        series.checkpoint("after-native-case-1", completed_case={"passed": True})
    journal = directory / "bootstrap-journal"
    journal.mkdir(mode=0o700)
    (journal / "prior-intent.json").write_text("{}")
    with pytest.raises(ValueError, match="retrospective"):
        series.checkpoint("before-native-cases")


@pytest.mark.parametrize("fault", [None, "wheel", "schema", "package", "rollout", "missing_pod"])
def test_protected_gateway_proof_covers_all_serving_pods_at_one_deployment(selection, fault):
    config, fixture, context = selection
    description = {"arn": "arn:aws:eks:us-east-1:000000000000:cluster/selected-cluster", "status": "ACTIVE"}
    session = SimpleNamespace(region_name="us-east-1", client=lambda *args, **kwargs: SimpleNamespace(describe_cluster=lambda **kwargs: {"cluster": description}))

    class Runtime:
        def __init__(self, session, description, namespace, *, proof_invoke_url):
            self.reads = 0
            assert proof_invoke_url == config["proof_invoke_url"]

        def get(self, path):
            self.reads += 1
            return {"metadata": {"uid": "deployment", "generation": self.reads if fault == "rollout" else 1}}

        def schema(self, pod, *, product):
            return {"pod_uid": pod["uid"], "l5_package_version": "wrong" if fault == "package" else fixture.package_version,
                    "l5_wheel_sha256": "wrong" if fault == "wheel" else fixture.wheel_sha256,
                    "l5_schema_sha256": "wrong" if fault == "schema" else fixture.schema_sha256}

        def gateway(self, **kwargs):
            assert kwargs["wheel_sha256"] == fixture.wheel_sha256
            for uid in ["pod-one", "pod-two"]:
                self.schema({"uid": uid}, product=True)
            return (["pod-one"] if fault == "missing_pod" else ["pod-one", "pod-two"]), fixture.database_head

        def close(self):
            pass

    if fault:
        with pytest.raises(ValueError):
            artifacts.gateway_readback(session, config, fixture, runtime_class=Runtime)
    else:
        result = artifacts.gateway_readback(session, config, fixture, runtime_class=Runtime)
        assert len(result["proofs"]) == 2 and result["migration_head"] == fixture.database_head


def test_lifecycle_reads_use_the_selected_region_and_a_bounded_subprocess(monkeypatch):
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["timeout"] == 45 and kwargs["check"] is True
        assert kwargs.get("shell") is not True
        return SimpleNamespace(stdout="{}")
    monkeypatch.setattr(artifacts.subprocess, "run", run)
    lifecycle = artifacts.lifecycle_module("eu-west-1")
    lifecycle.aws("sts", "get-caller-identity")
    assert commands == [["aws", "sts", "get-caller-identity", "--region", "eu-west-1", "--output", "json",
                         "--cli-connect-timeout", "3", "--cli-read-timeout", "10"]]
