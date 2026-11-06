"""Protected input survives staging and exact publication binding, never inventing provider schemas."""

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from test_prepare_handoff import runtime_configuration

from tool_adapters.config import AdapterConfiguration, ObservationBindings

ROOT = Path(__file__).parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("prefix", ["", "/dev", "/release_2"])
def test_authorization_allows_root_and_one_api_stage(prefix, release):
    value = runtime_configuration(release)["observation"]
    value = {k: v for k, v in value.items() if k in ObservationBindings.model_fields}
    value["core_authorize_url"] = "https://core.example.test" + prefix + "/internal/v1/tool-adapters/authorize"
    assert ObservationBindings.model_validate(value).core_authorize_url == value["core_authorize_url"]


@pytest.mark.parametrize(
    "path",
    [
        "/dev/../internal/v1/tool-adapters/authorize",
        "/one/two/internal/v1/tool-adapters/authorize",
        "/dev/internal/v1/tool-adapters/authorize?x=1",
        "/dev/internal/v1/tool-adapters/authorize#fragment",
        "/dev/internal/v1/tool-adapters/dispatch",
    ],
)
def test_authorization_does_not_accept_other_paths(path, release):
    value = runtime_configuration(release)
    value["observation"]["core_authorize_url"] = "https://core.example.test" + path
    with pytest.raises(ValueError):
        AdapterConfiguration.model_validate(value)


@pytest.mark.parametrize("profile", [False, True])
def test_protected_observation_reaches_configure_with_exact_image_and_schemas(tmp_path, monkeypatch, release, profile):
    materialize = load("materialize_observation")
    stage = load("stage_configuration")
    monkeypatch.syspath_prepend(str(ROOT))
    parent = tmp_path / "adp-adapter-stage"
    parent.mkdir(mode=0o700)
    envfile = tmp_path / "env"
    value = runtime_configuration(release)
    value["release"]["group"] = "atlassian-foundation"
    expected = copy.deepcopy(value)
    if profile:
        value["source_revision"] = value.pop("release")["source_revision"]
    settings = {
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_ENV": str(envfile),
        "ADP_TOOLS_ADAPTER_OBSERVATION_JSON": json.dumps(value),
    }
    path = materialize.materialize(settings)
    assert path.stat().st_mode & 0o077 == 0
    assert envfile.read_text() == f"ADP_TOOLS_ADAPTER_OBSERVATION_FILE={path}\n"
    publication = tmp_path / "publication.json"
    publication.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "source_revision": release["source_revision"],
                "image_digest": release["image_digest"],
            }
        )
    )
    for key, value_ in {
        "ADP_SOURCE_REVISION": release["source_revision"],
        "ADP_TOOLS_ADAPTER_GROUP_GENERATION": str(release["group_generation"]),
        "ADP_TOOLS_ADAPTER_BACKEND_GENERATION": str(release["backend_generation"]),
        "ADP_TOOLS_ADAPTER_OBSERVATION_FILE": str(path),
    }.items():
        monkeypatch.setenv(key, value_)
    output = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv", ["stage", "--publication", str(publication), "--output", str(output)])
    stage.main()
    assert json.loads(output.read_text()) == AdapterConfiguration.model_validate(expected).model_dump(mode="json")
    changed = copy.deepcopy(value)
    if profile:
        changed["source_revision"] = "f" * 40
    else:
        changed["release"]["image_digest"] = "sha256:" + "f" * 64
    path.write_text(json.dumps(changed))
    output.unlink()
    with pytest.raises(ValueError, match="adapter_observation_release_mismatch"):
        stage.main()
    assert not output.exists()


@pytest.mark.parametrize(
    "fault", ["duplicate", "oversize", "foundation", "symlink", "public_directory", "existing_file"]
)
def test_untrusted_observation_input_never_exports_an_environment_binding(tmp_path, release, fault):
    subject = load("materialize_observation")
    parent = tmp_path / "adp-adapter-stage"
    parent.mkdir(mode=0o700)
    value = runtime_configuration(release)
    envfile = tmp_path / "env"
    settings = {
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_ENV": str(envfile),
        "ADP_TOOLS_ADAPTER_OBSERVATION_JSON": json.dumps(value),
    }
    if fault == "duplicate":
        settings["ADP_TOOLS_ADAPTER_OBSERVATION_JSON"] = '{"mode":"observation","mode":"foundation"}'
    elif fault == "oversize":
        settings["ADP_TOOLS_ADAPTER_OBSERVATION_JSON"] = "a" * (subject.MAX_BYTES + 1)
    elif fault == "foundation":
        value.update(mode="foundation", observation=None)
        settings["ADP_TOOLS_ADAPTER_OBSERVATION_JSON"] = json.dumps(value)
    elif fault == "public_directory":
        parent.chmod(0o755)
    elif fault == "symlink":
        other = tmp_path / "other"
        parent.rename(other)
        parent.symlink_to(other, target_is_directory=True)
    else:
        (parent / "observation.json").write_text("do not replace")
    with pytest.raises((ValueError, OSError)):
        subject.materialize(settings)
    assert not envfile.exists()


def test_absent_input_preserves_foundation_mode_without_files(tmp_path):
    assert load("materialize_observation").materialize({"RUNNER_TEMP": str(tmp_path)}) is None
    assert list(tmp_path.iterdir()) == []
