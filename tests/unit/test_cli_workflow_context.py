"""A bounded qualification cannot quietly widen its suite or change its target."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.e2e.cli_uplift import cases, workflow_context


def fixture(monkeypatch):
    sha = "a" * 40
    monkeypatch.setattr(
        workflow_context.config,
        "EXAMPLE_PATH",
        SimpleNamespace(
            read_text=lambda: '{"platform_account":"123456789012","region":"us-east-1"}'
        ),
    )
    monkeypatch.setattr(
        workflow_context.config,
        "from_environment",
        lambda _: {"platform_account": "123456789012", "region": "us-east-1"},
    )
    env = dict(
        GITHUB_SHA=sha,
        ADP_CHECKOUT_SHA=sha,
        GITHUB_WORKFLOW_SHA=sha,
        GITHUB_REF="refs/heads/main",
        GITHUB_REPOSITORY_ID="123",
        GITHUB_RUN_ID="12",
        GITHUB_RUN_ATTEMPT="1",
        AWS_REGION="us-east-1",
    )
    inputs = dict(
        environment="dev",
        expected_revision=sha,
        mode="start",
        fixtures_json="{}",
        suites="knowledge",
        evaluation_id="",
        inject_fault="none",
        adp_source_revision=sha,
        adp_definition_revision=sha,
        adp_correlation="test-1",
    )
    return env, inputs


def test_exact_knowledge_suite_has_no_indexing_or_inference():
    assert cases.suite_cases("knowledge") == (
        cases.BY_ID["E01"],
        cases.LOGIN_CHECKPOINT,
        cases.BY_ID["E32"],
    )
    assert not cases.is_full(["knowledge"])


def test_verified_context_contains_no_transport_inputs(monkeypatch):
    env, inputs = fixture(monkeypatch)
    result = workflow_context.build_context(env, inputs, "123456789012")
    assert result["correlation"] == "test-1"
    assert result["source_revision"] == result["workflow_revision"] == "a" * 40
    assert result["resource_kind"] == "cli-evaluation"
    assert not any(key.startswith("adp_") for key in result["inputs"])


@pytest.mark.parametrize(
    "key,value",
    [
        ("suites", "full"),
        ("mode", "resume"),
        ("expected_revision", "b" * 40),
        ("fixtures_json", '{"knowledge":{}}'),
        ("environment", "prod"),
        ("adp_correlation", ""),
    ],
)
def test_changed_dispatch_is_refused(monkeypatch, key, value):
    env, inputs = fixture(monkeypatch)
    changed = deepcopy(inputs)
    changed[key] = value
    with pytest.raises(ValueError):
        workflow_context.build_context(env, changed, "123456789012")


def test_wrong_account_is_refused(monkeypatch):
    env, inputs = fixture(monkeypatch)
    with pytest.raises(ValueError, match="account changed"):
        workflow_context.build_context(env, inputs, "999999999999")


def test_later_main_keeps_the_accepted_source_and_actual_workflow_identity(monkeypatch):
    env, inputs = fixture(monkeypatch)
    env["GITHUB_SHA"] = env["GITHUB_WORKFLOW_SHA"] = inputs[
        "adp_definition_revision"
    ] = "b" * 40
    result = workflow_context.build_context(env, inputs, "123456789012")
    assert result["source_revision"] == "a" * 40
    assert result["workflow_revision"] == "b" * 40


def test_github_omitted_empty_evaluation_id_preserves_accepted_context(monkeypatch):
    env, inputs = fixture(monkeypatch)
    expected = workflow_context.build_context(env, inputs, "123456789012")
    del inputs["evaluation_id"]
    assert workflow_context.build_context(env, inputs, "123456789012") == expected


@pytest.mark.parametrize("change", ["nonempty_id", "missing_suite", "unknown_input"])
def test_empty_default_normalization_does_not_relax_scope(monkeypatch, change):
    env, inputs = fixture(monkeypatch)
    if change == "nonempty_id":
        inputs["evaluation_id"] = "existing-evaluation"
    elif change == "missing_suite":
        del inputs["suites"]
    else:
        inputs["unexpected"] = ""
    with pytest.raises(ValueError):
        workflow_context.build_context(env, inputs, "123456789012")
