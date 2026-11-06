"""Deployment binding failures stop before a ConfigMap can be applied."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

SPEC = importlib.util.spec_from_file_location("adapter_config_renderer", Path(__file__).parents[1] / "scripts/tool-adapter-config.py")
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


def manifest():
    return {
        "observe_url": "https://api-vpce-01234567.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tool-adapters/observe",
        "assertion_key_arn": "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111",
        "core_role_arn": "arn:aws:iam::123456789012:role/core",
        "adapter_role_arn": "arn:aws:iam::123456789012:role/adapter",
        "release": {
            "contract_version": "1.0",
            "source_revision": "a" * 40,
            "image_digest": "sha256:" + "b" * 64,
            "group": "atlassian-foundation",
            "group_generation": 1,
            "backend_generation": 2,
        },
    }


def template():
    return (Path(__file__).parents[1] / "k8s/configmap.yaml").read_text()


def test_full_binding_survives_actual_yaml_and_release_json_round_trip():
    value = manifest()
    data = yaml.safe_load(renderer.render(template(), renderer.bindings(value, "123456789012", "us-east-1")))["data"]
    assert json.loads(data["ADP_TOOL_ADAPTER_RELEASE"]) == value["release"]
    assert data["ADP_TOOL_ADAPTER_OBSERVE_URL"] == value["observe_url"]
    assert data["ADP_TOOL_ADAPTER_CORE_ROLE_ARN"] != data["ADP_TOOL_ADAPTER_ADAPTER_ROLE_ARN"]


@pytest.mark.parametrize(
    "error,missing",
    [("(ParameterNotFound)", True), ("(AccessDeniedException)", False), ("credentials expired", False), ("connection timed out", False)],
)
def test_only_absent_optional_configuration_disables_adapter(monkeypatch, error, missing):
    monkeypatch.setattr(renderer.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr=error))
    if not missing:
        with pytest.raises(ValueError, match="Cannot read"):
            renderer.load("dev", "123456789012", "us-east-1")
    else:
        data = yaml.safe_load(renderer.render(template(), renderer.load("dev", "123456789012", "us-east-1")))["data"]
        assert all(data[name] == "" for name in renderer.FIELDS.values())


@pytest.mark.parametrize(
    "fault",
    [
        "partial",
        "wrong_account",
        "same_roles",
        "http",
        "wrong_path",
        "two_stages",
        "query",
        "fragment",
        "userinfo",
        "release_missing",
        "release_boolean_generation",
        "release_tag",
    ],
)
def test_malformed_or_mixed_authority_refuses_render(fault):
    value = manifest()
    if fault == "partial":
        del value["adapter_role_arn"]
    elif fault == "wrong_account":
        value["core_role_arn"] = value["core_role_arn"].replace("123456789012", "999999999999")
    elif fault == "same_roles":
        value["adapter_role_arn"] = value["core_role_arn"]
    elif fault == "http":
        value["observe_url"] = value["observe_url"].replace("https:", "http:")
    elif fault == "wrong_path":
        value["observe_url"] += "/other"
    elif fault == "two_stages":
        value["observe_url"] = value["observe_url"].replace("/dev/", "/dev/other/")
    elif fault in {"query", "fragment"}:
        value["observe_url"] += "?other=x" if fault == "query" else "#other"
    elif fault == "userinfo":
        value["observe_url"] = value["observe_url"].replace("https://", "https://core@")
    elif fault == "release_missing":
        value["release"] = None
    elif fault == "release_boolean_generation":
        value["release"]["backend_generation"] = True
    else:
        value["release"]["image_digest"] = "latest"
    with pytest.raises(ValueError):
        renderer.bindings(value, "123456789012", "us-east-1")


def test_duplicate_manifest_fields_refused(monkeypatch):
    result = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"Parameter": {"Value": '{"release":null,"release":null}'}}))
    monkeypatch.setattr(renderer.subprocess, "run", lambda *a, **kw: result)
    with pytest.raises(ValueError, match="Duplicate"):
        renderer.load("dev", "123456789012", "us-east-1")


@pytest.mark.parametrize(
    "url", ["https://core.example.test/internal/v1/tool-adapters/observe", "https://core.example.test/dev/internal/v1/tool-adapters/observe"]
)
def test_runtime_accepts_exact_stage_or_root_observation_url(monkeypatch, url):
    from src.internal.tool_adapter_observation import observation_settings

    values = renderer.bindings({**manifest(), "observe_url": url}, "123456789012", "us-east-1")
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    assert observation_settings() == (
        url,
        values["ADP_TOOL_ADAPTER_ASSERTION_KEY_ARN"],
        values["ADP_TOOL_ADAPTER_CORE_ROLE_ARN"],
        manifest()["release"],
    )


@pytest.mark.parametrize(
    "path",
    [
        "/dev/extra/internal/v1/tool-adapters/observe",
        "/dev/../internal/v1/tool-adapters/observe",
        "/dev/internal/v1/tool-adapters/observe/",
        "/dev/internal/v1/tool-adapters/observe?other=1",
        "/dev/internal/v1/tool-adapters/authorize",
    ],
)
def test_runtime_rejects_other_routes(monkeypatch, path):
    from fastapi import HTTPException

    from src.internal.tool_adapter_observation import observation_settings

    for name, value in renderer.bindings(manifest(), "123456789012", "us-east-1").items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ADP_TOOL_ADAPTER_OBSERVE_URL", "https://core.example.test" + path)
    with pytest.raises(HTTPException) as error:
        observation_settings()
    assert error.value.status_code == 503
