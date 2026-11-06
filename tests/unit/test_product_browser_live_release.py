"""Offline checks for the protected Product browser release selection."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.new_ui.product_live_release import selection_from_environment, verified_frontend_release

ROLE = "arn:aws:iam::123456789012:role/adp-dev-trusted-checks"
SOURCE = "a" * 40
WORKFLOW = "b" * 40
BASE = "https://example.cloudfront.net"


@pytest.fixture
def context():
    selection = {
        "checks_role": ROLE, "source_revision": SOURCE, "workflow_revision": WORKFLOW,
        "repository": "aws-e/adp", "repository_id": 5, "run_id": 42, "attempt": 1,
        "region": "us-east-1", "environment": "dev",
    }
    release = {
        "schema_version": 1, "repository_id": 5, "run_id": 42, "run_attempt": 1,
        "source_revision": SOURCE, "workflow_revision": WORKFLOW,
        "workflow_path": ".github/workflows/gateway-frontend-deploy.yml",
        "account_id": "123456789012", "region": "us-east-1", "component": "gateway-frontend",
        "assets": {"index.html": "c" * 64},
    }

    class Store:
        calls = []
        def read(self, **kwargs):
            self.calls.append(kwargs)
            return json.dumps(release).encode(), "d" * 64, "signed-version-reference"

    store = Store()
    sts = SimpleNamespace(get_caller_identity=lambda: {
        "Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/adp-dev-trusted-checks/checks-7-1",
    })
    ssm = SimpleNamespace(get_parameter=lambda **kwargs: {"Parameter": {"Value": "D123"}})
    cloudfront = SimpleNamespace(get_distribution=lambda **kwargs: {"Distribution": {
        "ARN": "arn:aws:cloudfront::123456789012:distribution/D123",
        "DomainName": "example.cloudfront.net", "Status": "Deployed", "DistributionConfig": {"Enabled": True},
    }})
    return selection, release, store, sts, ssm, cloudfront


def observe(context, **overrides):
    selection, release, store, sts, ssm, cloudfront = context
    arguments = dict(selection=selection, base_url=BASE, sts=sts, ssm=ssm,
                     cloudfront=cloudfront, store=store)
    arguments.update(overrides)
    return verified_frontend_release(**arguments)


def test_signed_release_binds_source_workflow_role_and_live_distribution(context):
    receipt = observe(context)
    assert receipt == {"verified": True, "release": context[1], "domain": "example.cloudfront.net",
                       "digest": "d" * 64, "reference": "signed-version-reference"}
    assert context[2].calls[0]["workflow_revision"] == WORKFLOW
    assert context[2].calls[0]["name"] == "gateway-frontend"


@pytest.mark.parametrize("field,value", [
    ("source_revision", "f" * 40), ("workflow_revision", "f" * 40),
    ("run_id", 43), ("run_attempt", 2), ("component", "gateway-backend"),
    ("account_id", "987654321098"),
])
def test_different_signed_release_refused(context, field, value):
    context[1][field] = value
    with pytest.raises(ValueError, match="release differs"):
        observe(context)


def test_wrong_role_refused_before_evidence_read(context):
    with pytest.raises(ValueError, match="selected checks role"):
        observe(context, sts=SimpleNamespace(get_caller_identity=lambda: {
            "Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/adp-other-role/7",
        }))
    assert context[2].calls == []


def test_missing_evidence_and_read_denial_refuse_without_distribution_read(context):
    for result in (None, PermissionError("missing read permission")):
        def read(**kwargs):
            if isinstance(result, Exception):
                raise result
            return result
        with pytest.raises((ValueError, PermissionError)):
            observe(context, store=SimpleNamespace(read=read),
                    ssm=SimpleNamespace(get_parameter=lambda **kwargs: pytest.fail("SSM read before receipt")))


def test_wrong_distribution_and_missing_observation_permission_refuse(context):
    with pytest.raises(ValueError, match="distribution"):
        observe(context, base_url="https://wrong.cloudfront.net")
    with pytest.raises(ValueError, match="HTTPS origin"):
        observe(context, base_url="https://user:password@example.cloudfront.net")
    with pytest.raises(PermissionError):
        observe(context, ssm=SimpleNamespace(get_parameter=lambda **kwargs: (_ for _ in ()).throw(PermissionError())))
    with pytest.raises(PermissionError):
        observe(context, cloudfront=SimpleNamespace(get_distribution=lambda **kwargs: (_ for _ in ()).throw(PermissionError())))


def test_incomplete_selection_fails_closed_and_workflow_supplies_inputs():
    with pytest.raises(ValueError, match="requires non-empty inputs"):
        selection_from_environment({})
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/e2e-new-ui-playwright.yml").read_text()
    for field in ("product_source_revision", "product_release_run_id", "product_release_attempt",
                  "product_release_workflow_revision"):
        assert workflow.count(field + ":") == 2
        assert "${{ inputs." + field + " }}" in workflow
    assert "if: github.ref == 'refs/heads/main'" in workflow
    assert "'PyJWT[crypto]'" in workflow
    assert "ADP_CHECKS_ROLE_ARN: ${{ vars.ADP_CHECKS_ROLE_ARN }}" in workflow
    assert "selected_cases=(tests/e2e/new_ui/)" in workflow


@pytest.fixture
def release_environment():
    return {
        "ADP_CHECKS_ROLE_ARN": ROLE, "PRODUCT_EXPECTED_REVISION": SOURCE,
        "PRODUCT_RELEASE_WORKFLOW_REVISION": WORKFLOW, "GITHUB_REPOSITORY": "aws-e/adp",
        "GITHUB_REPOSITORY_ID": "5", "PRODUCT_RELEASE_RUN_ID": "42", "PRODUCT_RELEASE_ATTEMPT": "1",
        "AWS_REGION": "us-east-1", "ENVIRONMENT": "dev",
    }


def test_complete_environment_keeps_exact_release_selection(context, release_environment):
    assert selection_from_environment(release_environment) == context[0]


@pytest.mark.parametrize("name", ["PRODUCT_EXPECTED_REVISION", "PRODUCT_RELEASE_WORKFLOW_REVISION",
                                  "PRODUCT_RELEASE_RUN_ID", "PRODUCT_RELEASE_ATTEMPT"])
@pytest.mark.parametrize("value", [None, "", " \t"])
def test_missing_or_blank_product_input_is_named_and_never_defaulted(release_environment, name, value):
    if value is None:
        release_environment.pop(name)
    else:
        release_environment[name] = value
    with pytest.raises(ValueError, match="requires non-empty inputs: " + name):
        selection_from_environment(release_environment)


@pytest.mark.parametrize("name", ["GITHUB_REPOSITORY_ID", "PRODUCT_RELEASE_RUN_ID", "PRODUCT_RELEASE_ATTEMPT"])
@pytest.mark.parametrize("value", ["0", "-1", "bad", "1.5"])
def test_release_identity_numbers_fail_closed_without_echoing_values(release_environment, name, value):
    release_environment[name] = value
    with pytest.raises(ValueError, match="requires a positive integer: " + name) as error:
        selection_from_environment(release_environment)
    assert value not in str(error.value)


@pytest.mark.parametrize("product_selected", [False, True])
@pytest.mark.parametrize("value", [None, ""])
def test_collection_preflights_selected_product_cases_before_fixtures(monkeypatch, product_selected, value):
    from tests.e2e.new_ui import conftest

    for name in ("PRODUCT_EXPECTED_REVISION", "PRODUCT_RELEASE_WORKFLOW_REVISION", "PRODUCT_RELEASE_RUN_ID", "PRODUCT_RELEASE_ATTEMPT"):
        monkeypatch.delenv(name, raising=False)
        if value is not None:
            monkeypatch.setenv(name, value)
    monkeypatch.setenv("E2E_NEW_UI_ENABLED", "1")
    monkeypatch.setenv("E2E_NEW_UI_EXPECTED_FLAG", "on")
    item = SimpleNamespace(path=conftest.PACKAGE / ("test_product_profile.py" if product_selected else "test_coexistence.py"),
                           get_closest_marker=lambda _name: None)
    items = [item]
    config = SimpleNamespace(hook=SimpleNamespace(pytest_deselected=lambda **_kwargs: pytest.fail("Unexpected deselection")))
    if product_selected:
        with pytest.raises(pytest.UsageError, match="PRODUCT_RELEASE_RUN_ID") as error:
            conftest.pytest_collection_finish(SimpleNamespace(config=config, items=items))
        assert "full New UI suite includes Product checks" in str(error.value)
    else:
        conftest.pytest_collection_finish(SimpleNamespace(config=config, items=items))
    assert items == [item]


def test_valid_product_selection_retains_all_selected_cases(monkeypatch, release_environment):
    from tests.e2e.new_ui import conftest

    for name, value in release_environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("E2E_NEW_UI_ENABLED", "1")
    monkeypatch.setenv("E2E_NEW_UI_EXPECTED_FLAG", "on")
    items = [SimpleNamespace(path=conftest.PACKAGE / name, get_closest_marker=lambda _name: None)
             for name in ("test_coexistence.py", "test_product_profile.py", "test_product_catalog.py")]
    original = list(items)
    config = SimpleNamespace(hook=SimpleNamespace(pytest_deselected=lambda **_kwargs: pytest.fail("Unexpected deselection")))
    conftest.pytest_collection_finish(SimpleNamespace(config=config, items=items))
    assert items == original


@pytest.mark.parametrize("enabled", ["", "0"])
def test_disabled_live_suite_needs_no_release_selection(monkeypatch, enabled):
    from tests.e2e.new_ui import conftest

    monkeypatch.setenv("E2E_NEW_UI_ENABLED", enabled)
    monkeypatch.delenv("PRODUCT_RELEASE_RUN_ID", raising=False)
    item = SimpleNamespace(path=conftest.PACKAGE / "test_product_profile.py")
    conftest.pytest_collection_finish(SimpleNamespace(items=[item]))
