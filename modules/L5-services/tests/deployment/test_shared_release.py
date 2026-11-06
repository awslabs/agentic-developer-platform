"""Shared release identity, authorization and independently read artifact bytes."""

import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


context = load("release_context", "release_context.py")
artifacts = load("l5_release_artifacts", "release-artifacts.py")
SOURCE = "a" * 40
DEFINITION = "b" * 40
ROLE = "arn:aws:iam::123456789012:role/selected-release"
MUTATION_ROLE = "arn:aws:iam::123456789012:role/adp-example-trusted-deployment"


@pytest.fixture
def environment():
    return {
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": "example/project", "GITHUB_REPOSITORY_ID": "123",
        "GITHUB_WORKFLOW_REF": "example/project/.github/workflows/l5-services-release.yml@refs/heads/main",
        "GITHUB_RUN_ID": "456", "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_WORKFLOW_SHA": DEFINITION, "GITHUB_SHA": DEFINITION,
        "L5_CHECKOUT_REVISION": SOURCE, "L5_SERVICE": "security", "L5_ENVIRONMENT": "dev",
        "REVIEWED_SOURCE_SHA": "", "ADP_SOURCE_REVISION": SOURCE,
        "ADP_DEFINITION_REVISION": DEFINITION, "ADP_CONTRACT_DIGEST": "c" * 64,
        "ADP_CORRELATION": "orch:00000000-0000-4000-8000-000000000001",
        "L5_RELEASE_ROLE_ARN": ROLE, "L5_MUTATION_ROLE_ARN": MUTATION_ROLE,
        "ADP_GATEWAY_BASE_URL": "https://gateway.example.invalid",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://pipeline.actions.githubusercontent.com/token?job=example",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "fictional-request-credential",
        "RELEASE_BUCKET": "example-private-release",
    }


@pytest.mark.parametrize("service", ["security", "devops", "finops", "transform", "well_architected"])
def test_release_identity_accepts_shared_services_and_provider_retry(environment, service):
    environment["L5_SERVICE"] = service
    identity = context.release_identity(environment)
    assert identity["source_revision"] == SOURCE
    assert identity["definition_revision"] == DEFINITION
    assert identity["service"] == service
    assert identity["run_attempt"] == 2
    assert identity["phase"] == "artifacts"
    assert "qualification" not in identity


@pytest.mark.parametrize("key,value", [
    ("ADP_CORRELATION", ""), ("ADP_CONTRACT_DIGEST", "invalid"),
    ("ADP_DEFINITION_REVISION", SOURCE), ("GITHUB_SHA", SOURCE),
    ("L5_CHECKOUT_REVISION", DEFINITION), ("REVIEWED_SOURCE_SHA", DEFINITION),
    ("GITHUB_REF", "refs/heads/unreviewed"), ("GITHUB_EVENT_NAME", "pull_request"),
    ("GITHUB_RUN_ATTEMPT", "0"), ("GITHUB_REPOSITORY", "foreign/project"),
    ("L5_SERVICE", "unlisted"), ("L5_ENVIRONMENT", "unlisted"),
])
def test_release_refuses_mixed_stale_or_unreviewed_identity(environment, key, value):
    environment[key] = value
    with pytest.raises(ValueError):
        context.release_identity(environment)


def test_standalone_requires_immutable_reviewed_source_without_engine_claim(environment):
    for key in context.TRANSPORT:
        environment.pop(key)
    environment["REVIEWED_SOURCE_SHA"] = SOURCE
    identity = context.release_identity(environment)
    assert identity["correlation"] is None and identity["contract_digest"] is None
    context.authorize_engine(environment, identity, opener=object())
    environment["REVIEWED_SOURCE_SHA"] = "main"
    with pytest.raises(ValueError):
        context.release_identity(environment)


@pytest.mark.parametrize("operation", ["install", "publish"])
def test_installation_cannot_acquire_engine_delivery_identity(environment, operation):
    environment["L5_RELEASE_OPERATION"] = operation
    with pytest.raises(ValueError, match="cannot use engine delivery"):
        context.release_identity(environment)
    for key in context.TRANSPORT:
        environment.pop(key)
    environment["REVIEWED_SOURCE_SHA"] = SOURCE
    identity = context.release_identity(environment)
    assert identity["correlation"] is None and identity["contract_digest"] is None
    environment["L5_RELEASE_OPERATION"] = "bypass"
    with pytest.raises(ValueError, match="standalone installation"):
        context.release_identity(environment)


class Response(io.BytesIO):
    status = 200


class Opener:
    def __init__(self, response=None):
        self.response = {"authorized": True} if response is None else response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        assert timeout == 15
        payload = {"value": "fictional-oidc-credential"} if len(self.requests) == 1 else self.response
        return Response(json.dumps(payload).encode())


@pytest.mark.parametrize("path", ["", "/api", "/api/"])
def test_engine_uses_signed_bounded_check_with_contract_and_exact_roles(environment, path):
    environment["ADP_GATEWAY_BASE_URL"] += path
    opener = Opener()
    context.authorize_engine(environment, context.release_identity(environment), opener=opener)
    first, second = opener.requests
    assert "audience=adp-operations-release-job-v1" in first.full_url
    assert second.full_url == "https://gateway.example.invalid/api/orchestration/operations-release-job/check"
    body = json.loads(second.data)
    assert body == {
        "correlation": environment["ADP_CORRELATION"], "source_revision": SOURCE,
        "dispatch_revision": DEFINITION, "contract_digest": "c" * 64,
        "workflow": "l5-services-release.yml", "role_arn": ROLE,
        "mutation_role_arn": MUTATION_ROLE,
    }


@pytest.mark.parametrize("response", [{"authorized": False}, {"authorized": 1}, {}, {"authorized": True, "extra": True}])
def test_authorization_requires_exact_success(environment, response):
    with pytest.raises(ValueError, match="did not authorize"):
        context.authorize_engine(environment, context.release_identity(environment), opener=Opener(response))


@pytest.mark.parametrize("key,value", [
    ("ADP_GATEWAY_BASE_URL", "http://gateway.example.invalid"),
    ("ADP_GATEWAY_BASE_URL", "https://user:pass@gateway.example.invalid"),
    ("ADP_GATEWAY_BASE_URL", "https://gateway.example.invalid/foreign"),
    ("ACTIONS_ID_TOKEN_REQUEST_URL", "https://actions.githubusercontent.com.example.invalid/token"),
    ("L5_RELEASE_ROLE_ARN", "*"),
    ("L5_MUTATION_ROLE_ARN", "arn:aws:iam::000000000002:role/foreign"),
])
def test_authorization_refuses_bad_endpoints_and_foreign_roles_before_network(environment, key, value):
    environment[key] = value
    opener = Opener()
    with pytest.raises(ValueError):
        context.authorize_engine(environment, context.release_identity(environment), opener=opener)
    assert not opener.requests


@pytest.mark.parametrize("fault", ["accepted", "wrong_source", "wrong_run", "foreign_region",
                                     "missing_connection", "extra_field", "denied"])
def test_engine_target_requires_exact_signed_selected_identity(environment, fault):
    selected = {"connection_id": "selected-connection", "resource_kind": "eks-namespace",
                "resource_id": "example-cluster/example-namespace", "account_id": "123456789012",
                "region": "us-east-1"}
    response = {"authorized": True, "target": selected,
                "source_revision": SOURCE, "contract_digest": environment["ADP_CONTRACT_DIGEST"],
                "run_id": environment["GITHUB_RUN_ID"], "run_attempt": environment["GITHUB_RUN_ATTEMPT"]}
    if fault == "wrong_source":
        response["source_revision"] = DEFINITION
    elif fault == "wrong_run":
        response["run_attempt"] = "1"
    elif fault == "foreign_region":
        selected["region"] = "not-a-region"
    elif fault == "missing_connection":
        selected["connection_id"] = ""
    elif fault == "extra_field":
        response["extra"] = "untrusted"
    elif fault == "denied":
        response["authorized"] = False
    opener = Opener(response)
    if fault == "accepted":
        assert context.authorize_engine(environment, context.release_identity(environment), opener=opener,
                                        target=True) == selected
        assert opener.requests[1].full_url.endswith("/orchestration/operations-release-job/target")
    else:
        with pytest.raises(ValueError):
            context.authorize_engine(environment, context.release_identity(environment), opener=opener, target=True)


def test_target_reader_rejects_a_supplied_release_identity(environment):
    identity = context.release_identity(environment)
    opener = Opener()
    with pytest.raises(ValueError, match="identity changed"):
        context.authorize_engine(environment, {**identity, "run_id": identity["run_id"] + 1},
                                 opener=opener, target=True)
    assert not opener.requests


def test_actual_credential_must_be_the_selected_mutation_role(environment):
    principal = {"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/adp-example-trusted-deployment/session"}
    context.verify_principal(environment, principal)
    for changed in ({**principal, "Account": "000000000002"},
                    {**principal, "Arn": "arn:aws:sts::123456789012:assumed-role/foreign/session"}):
        with pytest.raises(ValueError):
            context.verify_principal(environment, changed)


def published(tmp_path, environment, fault):
    wheel = tmp_path / "adp_l5_services-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as package:
        package.writestr("l5_services/workers/execution.py", "# shared execution\n")
        package.writestr("l5_services/workers/recovery.py", "# shared recovery\n")
        if environment["L5_SERVICE"] == "transform" and fault != "missing-transform":
            for entry in ("adapters/transform/execution.py", "adapters/transform/retention.py",
                          "migrations/versions/l5_transform_cleanup.py", "run_input.py"):
                package.writestr(f"l5_services/{entry}", "# transform\n")
        for name in ("adapters/finops/billing.py", "finops_reconciliation.py",
                     "publication/contributions.py"):
            if fault != "missing-finops-wheel" or name != "finops_reconciliation.py":
                package.writestr(f"l5_services/{name}", "# shared finops\n")
        if environment["L5_SERVICE"] == "well_architected":
            for name in ("adapters/well_architected/binding.py",
                         "adapters/well_architected/generation.py",
                         "adapters/well_architected/results.py",
                         "migrations/versions/l5_wa_usage.py", "migrations/retained_downgrade.py",
                         "migrations/versions/l5_wa_reuse_authorization.py", "models/wa_reuse_authorization.py",
                         "migrations/versions/l5_wa_qualification.py", "models/wa_qualification.py",
                         "workers/relay.py", "run_input.py"):
                if fault != "missing-wa-wheel" or name != "adapters/well_architected/generation.py":
                    package.writestr(f"l5_services/{name}", "# shared WA\n")
    archive = tmp_path / "workers.zip"
    with zipfile.ZipFile(archive, "w") as workers:
        workers.writestr(f"release/{wheel.name}", wheel.read_bytes())
        with zipfile.ZipFile(wheel) as package:
            for name in package.namelist():
                if (fault != "missing-finops-module" or name != "l5_services/finops_reconciliation.py") and (
                        fault != "missing-wa-module" or name != "l5_services/adapters/well_architected/generation.py"):
                    changed = ((fault == "changed-finops-module" and name == "l5_services/finops_reconciliation.py")
                               or (fault == "changed-wa-module" and name == "l5_services/adapters/well_architected/generation.py"))
                    workers.writestr(name, b"different" if changed else package.read(name))
        workers.writestr("release/wheel.json", json.dumps({
            "wheel_filename": wheel.name, "wheel_sha256": artifacts.digest(wheel)}))
    manifest = {"source_sha": SOURCE, "wheel_sha256": artifacts.digest(wheel),
                "archive_sha256": artifacts.digest(archive), "wheel_version": "wheel-v1",
                "archive_version": "archive-v1"}
    if fault == "wrong-source":
        manifest["source_sha"] = DEFINITION
    if fault == "missing-version":
        manifest["archive_version"] = "null"
    calls = []

    def read(*args):
        calls.append(args)
        key = args[args.index("--key") + 1]
        assert key.startswith(f"l5/security/{SOURCE}/{artifacts.digest(wheel)}/")
        if args[1] == "head-object":
            return {"VersionId": "manifest-v1"}
        selected_version = args[args.index("--version-id") + 1]
        payload = {"manifest.json": json.dumps(manifest).encode(),
                   "wheel.whl": wheel.read_bytes(), "workers.zip": archive.read_bytes()}[key.rsplit("/", 1)[-1]]
        if fault == "changed-bytes" and key.endswith("workers.zip"):
            payload += b"changed"
        Path(args[-1]).write_bytes(payload)
        return {"VersionId": "different" if fault == "changed-version" else selected_version}

    return wheel, archive, read, calls


@pytest.mark.parametrize("service", ["security", "devops", "finops", "transform", "well_architected"])
def test_readback_returns_pinned_shared_coordinates_without_live_success(tmp_path, environment, service):
    environment["L5_SERVICE"] = service
    wheel, archive, read, calls = published(tmp_path, environment, "")
    result = artifacts.read_artifacts(wheel, archive, context.release_identity(environment), environment, read=read)
    assert result["manifest"]["version_id"] == "manifest-v1"
    assert result["artifacts"]["archive"]["sha256"] == artifacts.digest(archive)
    assert result["artifacts"]["wheel"]["version_id"] == "wheel-v1"
    assert result["phase"] == "artifacts" and result["service"] == service
    assert len(calls) == 4
    assert not {"checks", "cleanup", "qualification", "epic_criteria"} & result.keys()


@pytest.mark.parametrize("fault,reason", [
    ("missing-finops-wheel", "wheel lacks selected service entry points"),
    ("missing-finops-module", "archive lacks selected worker modules"),
    ("changed-finops-module", "differ from the selected shared wheel"),
])
def test_finops_readback_rejects_incompatible_installed_module(tmp_path, environment, fault, reason):
    environment["L5_SERVICE"] = "finops"
    wheel, archive, read, _ = published(tmp_path, environment, fault)
    with pytest.raises(ValueError, match=reason):
        artifacts.read_artifacts(wheel, archive, context.release_identity(environment), environment, read=read)


@pytest.mark.parametrize("fault,reason", [
    ("missing-wa-wheel", "wheel lacks selected service entry points"),
    ("missing-wa-module", "archive lacks selected worker modules"),
    ("changed-wa-module", "differ from the selected shared wheel"),
])
def test_wa_readback_rejects_incompatible_worker_bytes(tmp_path, environment, fault, reason):
    environment["L5_SERVICE"] = "well_architected"
    wheel, archive, read, calls = published(tmp_path, environment, fault)
    with pytest.raises(ValueError, match=reason):
        artifacts.read_artifacts(wheel, archive, context.release_identity(environment), environment, read=read)
    assert calls == []


@pytest.mark.parametrize("fault", ["wrong-source", "missing-version", "changed-bytes", "changed-version"])
def test_artifact_readback_refuses_unpinned_or_changed_publication(tmp_path, environment, fault):
    wheel, archive, read, calls = published(tmp_path, environment, fault)
    with pytest.raises(ValueError):
        artifacts.read_artifacts(wheel, archive, context.release_identity(environment), environment, read=read)


def test_stale_attempt_and_wrong_embedded_wheel_fail_before_storage(tmp_path, environment):
    wheel, archive, read, calls = published(tmp_path, environment, "")
    identity = context.release_identity(environment)
    with pytest.raises(ValueError, match="another attempt"):
        artifacts.read_artifacts(wheel, archive, {**identity, "run_attempt": 1}, environment, read=read)
    with zipfile.ZipFile(archive, "w") as workers:
        workers.writestr(f"release/{wheel.name}", b"wrong wheel")
    with pytest.raises(ValueError, match="selected shared wheel"):
        artifacts.read_artifacts(wheel, archive, identity, environment, read=read)
    assert not calls


def test_transform_release_refuses_wheel_without_runtime_before_storage(tmp_path, environment):
    environment["L5_SERVICE"] = "transform"
    wheel, archive, read, calls = published(tmp_path, environment, "missing-transform")
    with pytest.raises(ValueError, match="selected service entry points"):
        artifacts.read_artifacts(wheel, archive, context.release_identity(environment), environment, read=read)
    assert not calls
