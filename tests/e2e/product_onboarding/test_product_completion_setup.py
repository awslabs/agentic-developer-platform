"""Release and fixture mismatches fail before browser identities or mutations."""

from copy import deepcopy
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest

from tests.e2e.product_onboarding.product_completion_setup import fixture_environment, load_observations
from tests.e2e.product_onboarding.test_product_completion_observe import artifact


@pytest.fixture
def setup_input():
    manifest = {"repository": "aws-e/adp", "repository_id": 123, "source_revision": "e" * 40,
                "workflow_revision": "f" * 40, "run_id": 42, "run_attempt": 2, "components": {}}
    observations = {}
    for name in ("gateway-backend", "gateway-frontend"):
        release = artifact(name)
        observations[name] = {"release": release.model_dump(mode="json"), "release_ref": "s3://signed/receipt"}
        digest = (release.image_digest[7:] if release.image_digest else
                  hashlib.sha256(json.dumps(release.assets, sort_keys=True).encode()).hexdigest())
        manifest["components"][name] = {"observed_source_revision": release.source_revision,
            "observed_sha256": digest, "desired_sha256": digest, "verified": True,
            "evidence_ref": f"evidence/{name}.json"}
    config = dict(base_url="https://fixture.example", state_bucket="test", state_kms_key_id="test",
        retirement_role="test", internal_invoke_url="test", tenant_id="owner-tenant", actor_id="owner",
        owner_subject="owner-sub", nonmember_id="nonmember", nonmember_subject="nonmember-sub",
        nonmember_tenant_id="other-tenant", plan_revision="a" * 40, requirements_revision="b" * 40,
        backend_release={"run_id": 100, "attempt": 1, "workflow_revision": "b" * 40},
        credentials={peer: dict(username=peer, password="unit-test-only", cognito_user_pool_id="pool",
                               cognito_client_id="client") for peer in ("owner", "nonmember")})
    environment = {"ACCOUNT_ID": "000000000000", "PRODUCT_TARGET_REGION": "us-east-1",
                   "ENVIRONMENT": "dev", "GITHUB_WORKFLOW_SHA": "f" * 40}
    return manifest, observations, config, environment


def test_export_uses_named_release_and_contains_no_fixture_credentials(setup_input):
    before = deepcopy(setup_input)
    values = fixture_environment(*setup_input)
    assert setup_input == before
    assert values["PRODUCT_EXPECTED_REVISION"] == "e" * 40
    assert values["PRODUCT_RELEASE_RUN_ID"] == "100"
    assert values["PRODUCT_BACKEND_RELEASE_ATTEMPT"] == "1"
    assert values["PRODUCT_V09_REQUIREMENTS_REVISION"] == "b" * 40
    assert values["E2E_CLOUDFRONT_URL"] == "https://fixture.example"
    assert not any(value in json.dumps(values) for value in ("unit-test-only", "owner-sub", "other-tenant"))


@pytest.mark.parametrize("mismatch", ["pairing", "account", "component", "hash", "source", "engine", "origin"])
def test_refuse_mismatched_fixture_release_before_export(setup_input, mismatch):
    manifest, observations, config, environment = setup_input
    if mismatch == "pairing":
        config["backend_release"]["run_id"] += 1
    elif mismatch == "account":
        environment["ACCOUNT_ID"] = "111111111111"
    elif mismatch == "component":
        observations["gateway-backend"] = observations["gateway-frontend"]
    elif mismatch == "hash":
        manifest["components"]["gateway-backend"]["observed_sha256"] = "9" * 64
    elif mismatch == "source":
        manifest["components"]["gateway-frontend"]["observed_source_revision"] = "9" * 40
    elif mismatch == "engine":
        environment["ADP_SOURCE_REVISION"] = "9" * 40
    else:
        config["base_url"] = "https://fixture.example\nINJECTED=true"
    with pytest.raises(ValueError):
        fixture_environment(manifest, observations, config, environment)


@pytest.mark.parametrize("problem", [None, "missing", "duplicate", "oversize", "reference"])
def test_observation_archive_comes_from_exact_signed_release(setup_input, problem):
    manifest, observations, _, environment = setup_input
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, observation in observations.items():
            archive.writestr(name + ".json", json.dumps(observation))
        if problem == "duplicate":
            with pytest.warns(UserWarning):
                archive.writestr("gateway-backend.json", "{}")
        if problem == "oversize":
            archive.writestr("oversize.json", "x" * (1024 * 1024 + 1))
    if problem == "reference":
        manifest["components"]["gateway-backend"]["evidence_ref"] = "evidence/other.json"
    store = SimpleNamespace(read_archive=Mock(return_value=None if problem == "missing" else
                                             (buffer.getvalue(), "digest", "signed-version")))
    if problem:
        with pytest.raises(ValueError):
            load_observations(manifest, environment, store)
    else:
        assert load_observations(manifest, environment, store) == observations
    assert store.read_archive.call_args.kwargs == dict(
        account="000000000000", environment="dev", region="us-east-1", repository="aws-e/adp",
        repository_id=123, run_id=42, attempt=2, workflow_path=".github/workflows/product-profile-release.yml",
        workflow_revision="f" * 40, source_revision="e" * 40,
        kind="workflow-artifact", name="product-profile-observations")


def test_export_reuses_the_release_when_its_source_is_in_the_engine_lineage(setup_input):
    manifest, observations, config, environment = setup_input
    environment["ADP_SOURCE_REVISION"] = "9" * 40
    with pytest.raises(ValueError):
        fixture_environment(manifest, observations, config, environment)
    values = fixture_environment(manifest, observations, config, environment,
                                 lineage_verified=True)
    assert values["PRODUCT_EXPECTED_REVISION"] == "e" * 40
