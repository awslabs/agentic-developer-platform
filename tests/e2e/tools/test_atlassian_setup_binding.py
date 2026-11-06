"""Controlled protected-workflow binding and missing-authority cases."""

import json
from pathlib import Path

import pytest

from tests.e2e.tools.atlassian_setup_binding import (
    LOCAL_FILES,
    SESSION_ENV,
    bind_setup,
    main,
)
from tests.e2e.tools.atlassian_setup_live import load_setup
from tests.e2e.tools.test_atlassian_setup_live import configuration


def input_data(tmp_path):
    _, config = configuration(tmp_path)
    for role in ("core", "adapter"):
        config[f"{role}_label"] = f"oidc-{role}"
        config[f"{role}_role_arn"] = (
            f"arn:aws:iam::{config['account']}:role/"
            f"adp-dev-atlassian-mcp-observer-{role}"
        )
    manifest = {key: value for key, value in config.items() if key not in LOCAL_FILES}
    sessions = {key: key + "-private-session" for key in SESSION_ENV}
    return manifest, sessions


def test_binding_materializes_only_local_private_files(tmp_path):
    manifest, sessions = input_data(tmp_path)
    path = bind_setup(
        json.dumps(manifest),
        sessions,
        directory=tmp_path,
        gateway_url=manifest["gateway_url"],
    )
    fixture, config = load_setup(path, gateway_url=manifest["gateway_url"])
    assert fixture["ledger"].identity["run_id"] == manifest["run_id"]
    assert Path(path).parent.stat().st_mode & 0o077 == 0
    assert all(Path(config[key]).parent == Path(path).parent for key in LOCAL_FILES)
    assert all(Path(config[key]).stat().st_mode & 0o077 == 0 for key in sessions)
    assert all(sessions[key] not in Path(path).read_text() for key in sessions)


@pytest.mark.parametrize("invalid", ["extra", "session", "target", "path"])
def test_binding_refuses_missing_access_or_injected_paths(tmp_path, invalid):
    manifest, sessions = input_data(tmp_path)
    if invalid == "extra":
        manifest["python_module"] = "tests.e2e.tools.private_driver"
    elif invalid == "session":
        sessions.pop("observer_token_file")
    elif invalid == "target":
        manifest["gateway_url"] = "https://other.example.test"
    else:
        manifest["attempt_file"] = "/tmp/foreign-attempt"
    with pytest.raises(ValueError):
        bind_setup(
            json.dumps(manifest),
            sessions,
            directory=tmp_path,
            gateway_url="https://gateway.example.test",
        )
    assert not list(tmp_path.glob("atlassian-setup-*"))


def test_dispatch_requires_two_selected_observer_roles_before_publishing(
    tmp_path, monkeypatch
):
    manifest, sessions = input_data(tmp_path)
    output = tmp_path / "environment"
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_SETUP_JSON", json.dumps(manifest))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GATEWAY_URL", manifest["gateway_url"])
    monkeypatch.setenv("GITHUB_ENV", str(output))
    monkeypatch.setenv("EXPECTED_ACCOUNT", manifest["account"])
    monkeypatch.setenv("AWS_REGION", manifest["region"])
    for role in ("core", "adapter"):
        monkeypatch.setenv(
            f"ADP_TOOLS_ATLASSIAN_{role.upper()}_OBSERVER_ROLE_ARN",
            manifest[f"{role}_role_arn"],
        )
    for key, variable in SESSION_ENV.items():
        monkeypatch.setenv(variable, sessions[key])
    calls = []

    def aws(config, role, service, operation):
        calls.append((role, service, operation))
        if role == "adapter":
            raise ValueError("Selected AWS connection unavailable")

    monkeypatch.setattr("tests.e2e.tools.atlassian_setup_observer._aws", aws)
    with pytest.raises(ValueError, match="Selected AWS"):
        main()
    assert [role for role, _, _ in calls] == ["core", "adapter"]
    assert not output.exists() and not list(tmp_path.glob("atlassian-setup-*"))
    monkeypatch.setattr(
        "tests.e2e.tools.atlassian_setup_observer._aws",
        lambda config, role, service, operation: calls.append(
            (role, service, operation)
        ),
    )
    main()
    assert output.read_text().startswith("ADP_TOOLS_ATLASSIAN_SETUP_FILE=")
    assert "ADP_TOOLS_ATLASSIAN_AWS_CONFIG_FILE=" in output.read_text()
    assert [role for role, _, _ in calls][-2:] == ["core", "adapter"]


def test_live_workflow_binds_only_setup_scenario_and_cleans_private_inputs():
    workflow = Path(".github/workflows/tools-live-evaluation.yml").read_text()
    binding = workflow.split("- name: Bind protected Atlassian setup", 1)[1].split(
        "- name: Exercise deployed gateway", 1
    )[0]
    assert "if: steps.scenario-selection.outputs.atlassian_setup == 'true'" in binding
    assert "python3 -m tests.e2e.tools.atlassian_setup_binding" in binding
    assert "command -v adp-cred" not in binding
    assert "ADP_TOOLS_ATLASSIAN_CORE_OBSERVER_ROLE_ARN" in binding
    assert "ADP_TOOLS_ATLASSIAN_ADAPTER_OBSERVER_ROLE_ARN" in binding
    assert "${{ secrets.ADP_TOOLS_ATLASSIAN_SETUP_JSON }}" in binding
    assert "ADP_TOOLS_ATLASSIAN_SETUP_FILE%/config.json" in workflow
    assert "steps.scenario-selection.outputs.atlassian != 'true'" in workflow


def test_oidc_binding_rejects_roles_not_pinned_by_protected_environment(
    tmp_path, monkeypatch
):
    from tests.e2e.tools.atlassian_setup_binding import bind_oidc

    manifest, sessions = input_data(tmp_path)
    path = bind_setup(
        json.dumps(manifest),
        sessions,
        directory=tmp_path,
        gateway_url=manifest["gateway_url"],
    )
    _, config = load_setup(path, gateway_url=manifest["gateway_url"])
    monkeypatch.setenv("EXPECTED_ACCOUNT", config["account"])
    monkeypatch.setenv("AWS_REGION", config["region"])
    monkeypatch.setenv(
        "ADP_TOOLS_ATLASSIAN_CORE_OBSERVER_ROLE_ARN", config["core_role_arn"]
    )
    monkeypatch.setenv(
        "ADP_TOOLS_ATLASSIAN_ADAPTER_OBSERVER_ROLE_ARN", "different-role"
    )
    with pytest.raises(ValueError, match="protected role pins"):
        bind_oidc(config)
    monkeypatch.setenv(
        "ADP_TOOLS_ATLASSIAN_ADAPTER_OBSERVER_ROLE_ARN", config["adapter_role_arn"]
    )
    assert "credential_process" in Path(bind_oidc(config)).read_text()
